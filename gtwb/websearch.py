"""网关侧联网搜索（服务端工具执行）。

背景（2026-09-19 实测，脚本见 `_t/probe_search.py` / `_t/test_engines*.py`）：
上游 `copilot.tencent.com/v2/chat/completions` **不具备任何原生搜索能力**。
把 `enable_search` / `search_enable` / `web_search_options` 或 OpenAI 托管型
`{"type":"web_search"}` 工具透传过去，一律 200 但被静默忽略，模型会直接回答
「无法联网」。唯一能让模型发起检索的办法，是给它一个**普通 function 工具**。

因此 Codex 的托管工具 `{"type":"web_search","external_web_access":true}` 改由
本模块在网关侧执行：`_convert_tools_for_chat` 把它降级成普通 function，模型调用后
网关不发往客户端，而是自己检索，把标题/链接/摘要作为 tool 消息回灌，继续同一轮
对话。对 Codex 客户端完全透明。

引擎选型也是实测出来的（同一 query，用「结果是否含关键词」自动判相关性）：

| 引擎 | 结果 | 说明 |
|---|---|---|
| `bing` HTML/RSS | ✗ | 对本机出口 IP 返回噪声（标题对、条目全是无关内容） |
| `ddg` | ✗ | 无 cookie 时只回首页；SearXNG 公共实例全部 antibot |
| `gnews` | ✓ 52/55 | Google News RSS，走本机 Clash 或 TUN，覆盖面意外地广 |
| `wiki` | ✓ | Wikipedia API，百科/概念类最准 |
| `hn` | ✓ | HN Algolia，开发者讨论 |
| `stack` | ✓ | StackExchange API，报错/用法类 |
| `gh` | ✓ | GitHub 仓库检索 |
| `cn_bing` / `so360` | △ | 国内可得，但结果是泛化导航，作兜底 |

默认并发跑 `gnews + cn_bing + so360 + wiki` 并交错合并；配了官方 key 的
`wsa` / `brave` / `serper` / `tavily` 会优先并替代兜底引擎。

注意：`gnews` 返回的 feed 带 Google 的非商业个人使用声明，个人自用无碍；
若用于商业分发，请改用 `wsa`（腾讯云联网搜索，国内合规且面向 AI）。
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from html import unescape
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from . import obs

TOOL_NAME = "web_search"

TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Search the live web and get ranked results (title, URL, snippet). "
            "Call it whenever the answer depends on information that may have changed "
            "after your training cutoff: news, releases, version numbers, prices, "
            "schedules, error messages, or the current API of any library. "
            "Use short keyword queries; call it again with refined keywords when the "
            "first page is not enough. Never claim you cannot access the internet "
            "without calling this tool first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords, e.g. 'openai codex latest release'.",
                },
                "count": {
                    "type": "integer",
                    "description": "How many results to return (default 8, max 15).",
                },
            },
            "required": ["query"],
        },
    },
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_HREF_RE = re.compile(r'href="([^"]+)"')
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _clean(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", unescape(text)).strip()


def _norm_url(url: str) -> str:
    """去 fragment / 常见追踪参数，用于跨引擎去重。"""
    try:
        p = urlparse(url)
        return f"{p.netloc.lower().lstrip('www.')}{p.path.rstrip('/')}"
    except Exception:
        return url


# ---------------------------------------------------------------------------
# 引擎：统一签名 (client, query, count) -> [{title,url,snippet}]
# ---------------------------------------------------------------------------

_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_FIELD = {
    "title": re.compile(r"<title>(.*?)</title>", re.S),
    "link": re.compile(r"<link>(.*?)</link>", re.S),
    "desc": re.compile(r"<description>(.*?)</description>", re.S),
    "date": re.compile(r"<pubDate>(.*?)</pubDate>", re.S),
}


async def _gnews(client, query, count):
    """Google News RSS —— 覆盖面最广的免 key 源。"""
    cjk = bool(_CJK_RE.search(query))
    params = {
        "q": query,
        "hl": "zh-CN" if cjk else "en-US",
        "gl": "CN" if cjk else "US",
        "ceid": "CN:zh" if cjk else "US:en",
    }
    r = await client.get("https://news.google.com/rss/search", params=params,
                         headers={"User-Agent": _UA})
    r.raise_for_status()
    out = []
    for block in _ITEM_RE.findall(r.text)[: max(count * 2, 10)]:
        title = _clean((_FIELD["title"].search(block) or [None, ""])[1])
        link = (_FIELD["link"].search(block) or [None, ""])[1].strip()
        desc_raw = (_FIELD["desc"].search(block) or [None, ""])[1]
        date = (_FIELD["date"].search(block) or [None, ""])[1].strip()
        # description 里带原始链接；没有就用 Google 跳转链
        m = _HREF_RE.search(desc_raw)
        url = unescape(m.group(1)) if m else link
        snippet = _clean(desc_raw)
        # 去掉标题尾部的「 - 来源」
        if " - " in title:
            title = title.rsplit(" - ", 1)[0].strip()
        if not title or not url.startswith("http"):
            continue
        if date:
            snippet = f"({date}) {snippet}"
        out.append({"title": title, "url": url, "snippet": snippet[:500]})
        if len(out) >= count:
            break
    return out


async def _wiki(client, query, count):
    r = await client.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query,
                "format": "json", "srlimit": count},
        headers={"User-Agent": "gt-wb-gateway/1.0 (personal self-hosted)"},
    )
    r.raise_for_status()
    out = []
    for h in (r.json().get("query") or {}).get("search", []):
        out.append({
            "title": h.get("title", ""),
            "url": "https://en.wikipedia.org/wiki/" + quote_plus(h.get("title", "").replace(" ", "_")),
            "snippet": _clean(h.get("snippet", ""))[:400],
        })
    return out


async def _hn(client, query, count):
    r = await client.get("https://hn.algolia.com/api/v1/search",
                         params={"query": query, "hitsPerPage": count})
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        title = h.get("title") or h.get("story_title") or ""
        if not title:
            continue
        oid = h.get("objectID")
        url = h.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        out.append({"title": title, "url": url,
                    "snippet": (h.get("story_text") or "")[:300] or "Hacker News discussion"})
    return out


async def _stack(client, query, count):
    r = await client.get(
        "https://api.stackexchange.com/2.3/search/advanced",
        params={"order": "desc", "sort": "relevance", "q": query,
                "site": "stackoverflow", "pagesize": count, "filter": "default"},
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("items", []):
        out.append({
            "title": _clean(it.get("title", "")),
            "url": it.get("link", ""),
            "snippet": _clean(it.get("body", ""))[:300] or "Stack Overflow question",
        })
    return out


async def _gh(client, query, count):
    r = await client.get("https://api.github.com/search/repositories",
                         params={"q": query, "sort": "stars", "per_page": count},
                         headers={"Accept": "application/vnd.github+json",
                                  "User-Agent": "gt-wb-gateway"})
    r.raise_for_status()
    out = []
    for it in r.json().get("items", []):
        out.append({
            "title": f"{it.get('full_name')} ★{it.get('stargazers_count')}",
            "url": it.get("html_url", ""),
            "snippet": (it.get("description") or "")[:300],
        })
    return out


_BING_ITEM_RE = re.compile(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', re.S)
_BING_TITLE_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_BING_JUNK = ("进一步探索", "相关搜索", "翻译", "下一步", "广告")


def _bing_parse(html: str, count: int) -> list[dict]:
    out = []
    for block in _BING_ITEM_RE.findall(html):
        m = _BING_TITLE_RE.search(block)
        if not m:
            continue
        url, title = unescape(m.group(1)), _clean(m.group(2))
        if not url.startswith("http") or not title:
            continue
        if any(j in title for j in _BING_JUNK):
            continue
        snippet = ""
        for cand in _BING_SNIPPET_RE.findall(block):
            t = _clean(cand)
            if len(t) >= 40:
                snippet = t
                break
        out.append({"title": title, "url": url, "snippet": snippet[:500]})
        if len(out) >= count:
            break
    return out


async def _cn_bing(client, query, count):
    r = await client.get("https://cn.bing.com/search", params={"q": query},
                         headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    r.raise_for_status()
    return _bing_parse(r.text, count)


async def _so360(client, query, count):
    r = await client.get("https://www.so.com/s", params={"q": query},
                         headers={"User-Agent": _UA})
    r.raise_for_status()
    out = []
    for block in re.findall(r"<li[^>]*class=\"res-list[^\"]*\".*?(?=<li|</ul>)", r.text, re.S):
        m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        title = _clean(m.group(2))
        if not title or any(j in title for j in _BING_JUNK):
            continue
        sm = re.search(r'<p[^>]*class="[^"]*(?:res-desc|res-comm-con)[^"]*"[^>]*>(.*?)</p>', block, re.S)
        out.append({
            "title": title,
            "url": m.group(1) if m.group(1).startswith("http") else "https://www.so.com",
            "snippet": _clean(sm.group(1))[:400] if sm else "",
        })
        if len(out) >= count:
            break
    return out


async def _wsa(client, query, count, key):
    """腾讯云联网搜索（国内合规、面向 AI）。配 key 后优先使用。"""
    r = await client.get("https://api.bing.tencentcloudapi.com/",
                         params={"query": query}, headers={"Authorization": "Bearer " + key})
    r.raise_for_status()
    data = r.json()
    pages = (data.get("Response") or {}).get("Pages") or data.get("data") or []
    out = []
    for p in pages if isinstance(pages, list) else []:
        if isinstance(p, dict) and str(p.get("url", "")).startswith("http"):
            out.append({"title": _clean(str(p.get("title") or "")), "url": p["url"],
                        "snippet": _clean(str(p.get("passage") or p.get("snippet") or ""))[:500]})
    return out[:count]


async def _brave(client, query, count, key):
    r = await client.get("https://api.search.brave.com/res/v1/web/search",
                         params={"q": query, "count": count},
                         headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    out = []
    for w in ((r.json().get("web") or {}).get("results") or []):
        out.append({"title": _clean(w.get("title", "")), "url": w.get("url", ""),
                    "snippet": _clean(w.get("description", ""))[:500]})
    return out


async def _serper(client, query, count, key):
    r = await client.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": key, "Content-Type": "application/json"},
                          json={"q": query, "num": count})
    r.raise_for_status()
    out = []
    for o in (r.json().get("organic") or []):
        out.append({"title": o.get("title", ""), "url": o.get("link", ""),
                    "snippet": (o.get("snippet") or "")[:500]})
    return out


async def _tavily(client, query, count, key):
    r = await client.post("https://api.tavily.com/search",
                          json={"api_key": key, "query": query, "max_results": count})
    r.raise_for_status()
    out = []
    for o in (r.json().get("results") or []):
        out.append({"title": o.get("title", ""), "url": o.get("url", ""),
                    "snippet": (o.get("content") or "")[:500]})
    return out


# 名称 → (实现, 是否需要 key)
_KEYED = {"wsa", "brave", "serper", "tavily"}
_PLAIN: dict[str, Callable] = {
    "gnews": _gnews, "wiki": _wiki, "hn": _hn, "stack": _stack, "gh": _gh,
    "cn_bing": _cn_bing, "so360": _so360,
}
_KEYED_IMPL = {"wsa": _wsa, "brave": _brave, "serper": _serper, "tavily": _tavily}

DEFAULT_ENGINES = ["gnews", "cn_bing", "so360", "wiki"]
DEFAULT_KEYED_ENGINES = ["wsa", "brave", "serper", "tavily"]


def _api_key_for(cfg, name: str) -> str:
    if name == "wsa":
        return getattr(cfg, "web_search_api_key", "") or ""
    return getattr(cfg, f"web_search_{name}_key", "") or ""


async def _run_engine(cfg, name: str, query: str, count: int) -> list[dict]:
    timeout = float(getattr(cfg, "web_search_timeout_s", 15.0) or 15.0)
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=min(timeout, 8.0)),
        "follow_redirects": True,
    }
    proxy = getattr(cfg, "web_search_proxy", "") or ""
    if proxy:
        kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**kwargs) as client:
        if name in _PLAIN:
            return await _PLAIN[name](client, query, count)
        if name in _KEYED_IMPL:
            key = _api_key_for(cfg, name)
            if not key:
                raise RuntimeError(f"{name}: no api key configured")
            return await _KEYED_IMPL[name](client, query, count, key)
    raise RuntimeError(f"unknown engine {name}")


def resolve_engines(cfg) -> list[str]:
    """决定本次用哪些引擎：配了 key 的官方源优先，其余用免 key 兜底。"""
    raw = getattr(cfg, "web_search_engines", None) or DEFAULT_ENGINES
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",") if s.strip()]
    engines = [e for e in raw if e in _PLAIN or e in _KEYED_IMPL]
    if not engines:
        engines = list(DEFAULT_ENGINES)

    keyed = [e for e in DEFAULT_KEYED_ENGINES if _api_key_for(cfg, e)]
    if keyed:
        # 有官方源就把它放最前，并去掉兜底里质量最差的国内泛搜
        engines = keyed + [e for e in engines if e not in keyed and e not in ("cn_bing", "so360")]
    return engines


async def web_search(cfg, query: str, count: int = 8) -> list[dict]:
    """并发跑各引擎、交错合并去重。任一引擎挂掉只降级，绝不打断主链路。"""
    query = (query or "").strip()
    if not query:
        return []
    count = max(1, min(int(count or 8), 15))
    engines = resolve_engines(cfg)
    per = max(count, 6)

    async def safe(name: str):
        try:
            got = await asyncio.wait_for(_run_engine(cfg, name, query, per), timeout=30.0)
            return name, [g for g in got if g.get("title") and g.get("url")]
        except Exception as e:
            obs.log(f"⚠ web_search[{name}] 失败：{type(e).__name__}: {str(e)[:110]}")
            return name, []

    groups = await asyncio.gather(*(safe(n) for n in engines))

    merged: list[dict] = []
    seen: set[str] = set()
    seen_title: set[str] = set()
    depth = max((len(g) for _, g in groups), default=0)
    for i in range(depth):
        for name, group in groups:
            if i >= len(group):
                continue
            item = group[i]
            nu = _norm_url(item["url"])
            nt = item["title"].lower()[:45]
            if nu in seen or nt in seen_title:
                continue
            seen.add(nu)
            seen_title.add(nt)
            merged.append({**item, "engine": name})
            if len(merged) >= count:
                break
        if len(merged) >= count:
            break

    if merged:
        used = ", ".join(f"{n}:{len(g)}" for n, g in groups if g)
        obs.log(f"✓ web_search q={query[:56]!r} → {len(merged)} 条 ({used})")
    else:
        obs.log(f"⚠ web_search q={query[:56]!r} → 全部引擎无结果")
    return merged


def format_results(query: str, results: list[dict], max_chars: int = 3600) -> str:
    """把结果排版成给模型读的 tool 输出。

    有长度上限：一轮里模型可能并发搜 6 个词、每次 8 条，不限长会把上下文撑爆
    （实测第 2 轮 input 就冲到 23k tokens，大部分是搜索结果）。
    """
    if not results:
        return (
            f'Web search for "{query}" returned no usable results. '
            "Do not repeat the same query; either refine the keywords, or tell the user "
            "plainly that the search came back empty."
        )
    head = [f'Web search results for "{query}":', ""]
    tail_note = ("\nUse these results to answer the user, and cite the URLs you rely on. "
                 "If they do not answer the question, call web_search again with "
                 "different keywords.")
    lines: list[str] = []
    used = len("\n".join(head)) + len(tail_note)
    for i, r in enumerate(results, 1):
        block = [f"[{i}] {r['title']}", f"    URL: {r['url']}"]
        if r.get("snippet"):
            block.append(f"    {r['snippet']}")
        block.append("")
        size = len("\n".join(block))
        if used + size > max_chars:
            lines.append("… (more results omitted for length)")
            break
        lines.extend(block)
        used += size
    return "\n".join(head + lines) + tail_note
