# -*- coding: utf-8 -*-
"""WorkBuddy 桌面客户端「自身」的 token 用量聚合器（只读）。

背景
----
网关账本 `usage-stats.jsonl` 只记录**经本网关转发**的调用（Codex / Claude Code /
curl 等第三方客户端）。而 WorkBuddy 桌面客户端**自己**调用同一批模型时并不经过网关，
这部分消耗此前完全不可见 —— 于是「某个模型的整体用量」永远算不全。

好在客户端把每次模型调用都落成了本地追踪文件：

    <用户目录>/.workbuddy/traces/<workerPid>/trace_*.json

顶层 `trace.startedAt` 给日期；`spans[]` 里 `type == "generation"` 的每个 span
的 `toolOutput` 是一段 JSON 字符串，解析后是标准 OpenAI 响应体，含
`model` 与 `usage{prompt_tokens, completion_tokens, total_tokens,
prompt_tokens_details.cached_tokens, completion_tokens_details.reasoning_tokens}`。

本模块把这些文件聚合为「按日 × 按模型」的 token 账，供状态看板与网关口径相加。

用法
----
    from wb_trace_stats import WbTraceIndex
    idx = WbTraceIndex()                 # 自动定位 traces 目录
    idx.daily(7)                         # 最近 7 天按日合计
    idx.models(days=7)                   # 最近 7 天按模型合计
    idx.day("2026-10-08")                # 指定整日明细（含按模型拆分）
    idx.summary()                        # 全量概览（有多少天 / 多少文件）

设计
----
- **不抛异常**：任何单文件解析失败只计数跳过，绝不影响看板。
- **增量缓存**：以 (路径, mtime, size) 为键缓存每个文件解析结果；
  trace 文件落盘后不再改写，因此重复扫描近乎零成本。
- **口径与网关一致**：prompt/completion/total/cached/reasoning 五字段同名同义，
  可直接相加得到「整体用量」。
"""
import json
import os
from datetime import datetime, timedelta

# 默认 traces 根目录（本机 WorkBuddy 用户目录）
DEFAULT_TRACES_DIR = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "traces"
)

# 单文件解析缓存： key -> {"mtime":..., "size":..., "calls":[...]}
_FILE_CACHE: dict = {}


def _extract_usages(path: str) -> list:
    """从一个 trace 文件里抽出全部 (day, hour, model, usage) 记录。

    返回 [{"day","hour","model","p","c","t","cached","reason"}...]。
    任何异常都吞掉并返回 []（看板不能因一个坏文件而挂）。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return []

    trace = data.get("trace") if isinstance(data, dict) else None
    if not isinstance(trace, dict):
        return []
    started = trace.get("startedAt") or ""
    day = started[:10]
    try:
        hour = int(started[11:13])
    except Exception:
        hour = -1

    out = []
    spans = data.get("spans")
    if not isinstance(spans, list):
        return []
    for sp in spans:
        if not isinstance(sp, dict) or sp.get("type") != "generation":
            continue
        raw = sp.get("toolOutput")
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if not isinstance(parsed, list) or not parsed:
            continue
        rec = parsed[0]
        if not isinstance(rec, dict):
            continue
        usage = rec.get("usage")
        if not isinstance(usage, dict):
            continue
        pdt = usage.get("prompt_tokens_details") or {}
        cdt = usage.get("completion_tokens_details") or {}
        if not isinstance(pdt, dict):
            pdt = {}
        if not isinstance(cdt, dict):
            cdt = {}
        out.append({
            "day": day,
            "hour": hour,
            "model": rec.get("model") or "(unknown)",
            "p": usage.get("prompt_tokens") or 0,
            "c": usage.get("completion_tokens") or 0,
            "t": usage.get("total_tokens") or 0,
            "cached": pdt.get("cached_tokens") or 0,
            "reason": cdt.get("reasoning_tokens") or 0,
        })
    return out


class WbTraceIndex:
    """扫描 traces 目录并聚合 WorkBuddy 客户端自身的 token 用量。"""

    def __init__(self, traces_dir: str | None = None):
        self.traces_dir = traces_dir or os.environ.get(
            "GTWB_TRACES_DIR", DEFAULT_TRACES_DIR
        )

    # ---- 文件枚举 -------------------------------------------------
    def _list_files(self) -> list:
        files = []
        try:
            for entry in os.scandir(self.traces_dir):
                if not entry.is_dir():
                    continue
                try:
                    for f in os.scandir(entry.path):
                        if f.is_file() and f.name.startswith("trace_") \
                                and f.name.endswith(".json"):
                            files.append(f.path)
                except Exception:
                    continue
        except FileNotFoundError:
            return []
        return files

    def _load_all(self) -> list:
        """返回全部调用记录（带文件级缓存，只重解析变化的文件）。"""
        calls = []
        seen = set()
        for path in self._list_files():
            try:
                st = os.stat(path)
            except Exception:
                continue
            key = (path, int(st.st_mtime), st.st_size)
            seen.add(key)
            hit = _FILE_CACHE.get(key)
            if hit is None:
                hit = _extract_usages(path)
                _FILE_CACHE[key] = hit
            calls.extend(hit)
        # 清掉已不存在/已变化的旧缓存项，避免长跑内存膨胀
        if len(_FILE_CACHE) > 4 * max(1, len(seen)):
            for k in list(_FILE_CACHE.keys()):
                if k not in seen:
                    _FILE_CACHE.pop(k, None)
        return calls

    # ---- 聚合 -----------------------------------------------------
    @staticmethod
    def _blank() -> dict:
        return {"p": 0, "c": 0, "t": 0, "cached": 0, "reason": 0, "n": 0}

    @staticmethod
    def _add(acc: dict, rec: dict):
        acc["p"] += rec["p"]
        acc["c"] += rec["c"]
        acc["t"] += rec["t"]
        acc["cached"] += rec["cached"]
        acc["reason"] += rec["reason"]
        acc["n"] += 1

    @staticmethod
    def _finalize(acc: dict) -> dict:
        out = dict(acc)
        out["cache_rate"] = round(acc["cached"] * 100 / acc["p"], 1) if acc["p"] else None
        return out

    def day(self, date: str) -> dict:
        """指定整日（YYYY-MM-DD）的明细：合计 + 按模型拆分 + 按小时分布。"""
        recs = [r for r in self._load_all() if r["day"] == date]
        total = self._blank()
        by_model = {}
        hours = [0] * 24
        for r in recs:
            self._add(total, r)
            m = by_model.setdefault(r["model"], self._blank())
            self._add(m, r)
            if 0 <= r["hour"] < 24:
                hours[r["hour"]] += 1
        return {
            "date": date,
            "total": self._finalize(total),
            "models": [
                {"model": m, **self._finalize(v)}
                for m, v in sorted(by_model.items(), key=lambda x: -x[1]["t"])
            ],
            "hours": hours,
        }

    def daily(self, days: int = 7, end: str | None = None) -> list:
        """最近 N 天按日合计（含 end 当天，默认今天）。"""
        base = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
        recs = self._load_all()
        buckets = {}
        for r in recs:
            b = buckets.setdefault(r["day"], self._blank())
            self._add(b, r)
        out = []
        for i in range(days - 1, -1, -1):
            d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            out.append({"date": d, **self._finalize(buckets.get(d, self._blank()))})
        return out

    def models(self, days: int | None = 7, end: str | None = None) -> list:
        """按模型合计。days=None 表示全量（不限天数）。"""
        recs = self._load_all()
        if days is not None:
            base = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
            lo = (base - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            hi = base.strftime("%Y-%m-%d")
            recs = [r for r in recs if lo <= r["day"] <= hi]
        by_model = {}
        for r in recs:
            m = by_model.setdefault(r["model"], self._blank())
            self._add(m, r)
        return [
            {"model": m, **self._finalize(v)}
            for m, v in sorted(by_model.items(), key=lambda x: -x[1]["t"])
        ]

    def available_days(self) -> list:
        """有数据的日期列表（升序），供前端日期选择器用。"""
        return sorted({r["day"] for r in self._load_all() if r["day"]})

    def summary(self) -> dict:
        recs = self._load_all()
        total = self._blank()
        for r in recs:
            self._add(total, r)
        days = sorted({r["day"] for r in recs if r["day"]})
        return {
            "traces_dir": self.traces_dir,
            "files": len(self._list_files()),
            "calls": total["n"],
            "total": self._finalize(total),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "active_days": len(days),
        }
