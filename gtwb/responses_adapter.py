"""
responses_adapter.py — OpenAI Responses API ↔ Chat Completions API 适配层。

Codex CLI 使用 Responses API（POST /v1/responses），而 CodeBuddy 后端只支持
Chat Completions 协议。本模块做双向转换：
  请求：Responses input/instructions/tools → Chat messages/tools
  响应：Chat SSE delta → Responses 语义事件流（response.created / output_text.delta / …）

事件类型参考：https://developers.openai.com/api/docs/guides/streaming-responses
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from typing import Any

from . import websearch

# ---------------------------------------------------------------------------
# ID 生成
# ---------------------------------------------------------------------------

def _rand_id(prefix: str = "resp_") -> str:
    return prefix + os.urandom(12).hex()

# ---------------------------------------------------------------------------
# 请求转换：Responses → Chat
# ---------------------------------------------------------------------------

def responses_request_to_chat(body: dict, enable_web_search: bool = True
                              ) -> tuple[dict, dict[str, tuple[str, str]], set[str]]:
    """将 Responses API 请求体转换为 Chat Completions 请求体。

    关键映射：
      input → messages
      instructions → system message（置顶）
      max_output_tokens → max_tokens
      tools 格式微调（Responses 用 name，Chat 用 function.name）
      namespace 工具容器 → 提升为顶层 function，返回 bare→namespace 映射
      托管型 web_search → 降级为普通 function，由网关自己执行检索

    返回 (chat_body, bare_to_ns, internal_tools)。
    internal_tools 是「由网关代跑、不转发给客户端」的工具名集合。
    """
    name_route: dict[str, tuple[str, str]] = {}
    internal_tools: set[str] = set()
    messages: list[dict] = []

    # instructions → system message
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # input → messages
    inp = body.get("input", [])
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        messages.extend(_convert_input_items(inp))

    # 构造 Chat body
    chat: dict[str, Any] = {"messages": messages, "stream": True}

    # model
    if "model" in body:
        chat["model"] = body["model"]

    # tools — Responses 和 Chat 的 function tool 格式略有不同
    tools = body.get("tools")
    if tools:
        chat_tools, name_route, internal_tools = _convert_tools_for_chat(
            tools, enable_web_search=enable_web_search
        )
        if chat_tools:
            chat["tools"] = chat_tools
    if "tool_choice" in body:
        chat["tool_choice"] = _convert_tool_choice(body["tool_choice"])

    # 透传常见参数
    for key in ("temperature", "top_p", "stop", "seed",
                "presence_penalty", "frequency_penalty",
                "response_format", "parallel_tool_calls"):
        if key in body:
            chat[key] = body[key]

    # 推理强度：Codex 发的是嵌套对象 {"effort": "high", "summary": "auto"}，
    # 早期实现只找顶层 reasoning_effort，导致客户端的强度选择被静默忽略。
    effort = body.get("reasoning_effort")
    if not effort and isinstance(body.get("reasoning"), dict):
        effort = body["reasoning"].get("effort")
    if effort:
        chat["reasoning_effort"] = effort

    # 提示词缓存键：Codex 每轮都带，透传可显著提高上游 prefix cache 命中率。
    if body.get("prompt_cache_key"):
        chat["prompt_cache_key"] = body["prompt_cache_key"]

    # max_output_tokens → max_tokens
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    elif "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]

    return chat, name_route, internal_tools


def _convert_input_items(items: list) -> list[dict]:
    """将 Responses API 的 input 数组转换为 Chat messages。

    input 里可能包含：
      - {"role": "user/developer", "content": ...}   → 直接映射
      - {"type": "message", ...}                      → 助手消息
      - {"type": "function_call", ...}                → 需合并到前面的助手消息
      - {"type": "function_call_output", ...}         → tool 角色
    """
    messages: list[dict] = []
    # 临时缓存：合并相邻的 assistant message 和 function_call
    pending_assistant_content: str | None = None
    pending_tool_calls: list[dict] = []
    # 临时缓存：一组连续的 tool 结果 → [(tool 消息, 该输出的图片 URL 列表)]
    pending_tool_results: list[tuple[dict, list[str]]] = []

    def _flush_assistant():
        nonlocal pending_assistant_content, pending_tool_calls
        if pending_assistant_content is not None or pending_tool_calls:
            msg: dict[str, Any] = {"role": "assistant",
                                   "content": pending_assistant_content or ""}
            if pending_tool_calls:
                msg["tool_calls"] = pending_tool_calls[:]
            messages.append(msg)
            pending_assistant_content = None
            pending_tool_calls.clear()

    def _flush_tool_results():
        """输出缓存的 tool 结果；图片消息统一挂到整组之后。

        Chat 协议要求 `assistant.tool_calls` 之后**连续**跟上同等数量的 tool
        消息。工具输出里的图片只能借道 user 消息承载，若在每条 tool 后立即插入，
        并行调用就会变成 [tool A, user(图), tool B, user(图)] —— B 被 user 隔开，
        上游直接 400 `11148 tool calls and tool results do not match`。

        实测（2026-09-19，Mac Codex Desktop）：`view_image`×2 触发 11148，
        而 `exec_command`×2（纯文本输出）正常 —— 差别就在有无图片消息插入。
        """
        nonlocal pending_tool_results
        if not pending_tool_results:
            return
        images: list[str] = []
        for msg, imgs in pending_tool_results:
            messages.append(msg)
            images.extend(imgs)
        if images:
            messages.append({
                "role": "user",
                "content": [{"type": "text",
                             "text": "[Image output from the previous tool call(s)]"}]
                           + [{"type": "image_url", "image_url": {"url": u}}
                              for u in images],
            })
        pending_tool_results = []

    for item in items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")

        # 任何非工具结果的项，都意味着上一组工具结果已结束 → 先落盘它们。
        # `*_call_output` 涵盖 function_call_output 与 local_shell_call_output 等。
        if not (isinstance(item_type, str) and item_type.endswith("_call_output")):
            _flush_tool_results()
        role = item.get("role", "")

        # 简单消息 {"role": "user", "content": "..."}
        if item_type is None and role in ("user", "system", "developer"):
            _flush_assistant()
            mapped_role = "system" if role == "developer" else role
            content = _extract_content_parts(item.get("content", ""))
            messages.append({"role": mapped_role, "content": content})
            continue

        # typed message（Responses 里常见）
        if item_type == "message" and role in ("user", "system", "developer"):
            _flush_assistant()
            mapped_role = "system" if role == "developer" else role
            content = _extract_content_parts(item.get("content", ""))
            messages.append({"role": mapped_role, "content": content})
            continue

        # assistant 消息（来自前一轮输出）
        if item_type == "message" and role == "assistant":
            _flush_assistant()
            content_parts = item.get("content", [])
            text = _extract_output_text(content_parts) if isinstance(content_parts, list) else str(content_parts)
            pending_assistant_content = text
            continue

        # 简单 role=assistant（无 type 标记）
        if item_type is None and role == "assistant":
            _flush_assistant()
            content = _extract_content(item.get("content", ""))
            pending_assistant_content = content
            # chat 风格的 tool_calls 直接随消息携带，必须承接（否则多轮工具历史断裂）
            for tc in item.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("function"):
                    pending_tool_calls.append({
                        "id": tc.get("id", _rand_id("call_")),
                        "type": "function",
                        "function": tc["function"],
                    })
            continue

        # function_call — 合并到前面的 assistant 消息
        if item_type == "function_call":
            if pending_assistant_content is None:
                pending_assistant_content = ""
            pending_tool_calls.append({
                "id": item.get("call_id", item.get("id", _rand_id("call_"))),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            })
            continue

        # function_call_output → tool 消息
        if item_type == "function_call_output":
            _flush_assistant()
            output = item.get("output", "")
            text_out, images = _split_tool_output_images(output)
            pending_tool_results.append(({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": text_out,
            }, images))
            # Chat 协议的 tool 消息只能放文本；工具输出里的图片改为紧随其后的
            # 一条 user 消息承载（否则模型"看不见图"，且 base64 会被静默丢弃）。
            continue

        # reasoning 项（Codex 开启 reasoning summary 时带回）：
        # Chat 后端没有对应容器，跳过但显式计数，避免"静默丢失"无从察觉。
        if item_type == "reasoning":
            continue

        # 其它 *_call / *_call_output（local_shell_call 等未来类型）
        if isinstance(item_type, str) and item_type.endswith("_call_output"):
            _flush_assistant()
            pending_tool_results.append(({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": _normalize_tool_output(item.get("output", "")),
            }, []))
            continue
        if isinstance(item_type, str) and item_type.endswith("_call"):
            if pending_assistant_content is None:
                pending_assistant_content = ""
            pending_tool_calls.append({
                "id": item.get("call_id", item.get("id", _rand_id("call_"))),
                "type": "function",
                "function": {
                    "name": item.get("name", item_type[:-5]),
                    "arguments": _normalize_call_arguments(item),
                },
            })
            continue

        # Chat 风格 tool 结果消息（role=tool，无 type 标记）— 必须保留 tool_call_id
        if item_type is None and role == "tool":
            _flush_assistant()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("tool_call_id", item.get("call_id", "")),
                "content": _normalize_tool_output(item.get("output", item.get("content", ""))),
            })
            continue

        # 顶层图片输入项（图片也可能直接放在 input 数组顶层）
        if item_type == "input_image":
            _flush_assistant()
            url = item.get("image_url", item.get("url", ""))
            if isinstance(url, dict):
                url = url.get("url", "")
            if url:
                messages.append({"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": url}}
                ]})
            continue

        # 其他未知类型 — 尝试当作普通消息
        if role:
            _flush_assistant()
            content = _extract_content(item.get("content", ""))
            messages.append({"role": role, "content": content})

    _flush_tool_results()
    _flush_assistant()
    return messages


def _normalize_tool_output(output) -> str:
    """Chat 协议的 tool 消息 content 必须是字符串。

    Codex/MCP 工具的 output 可能是结构化对象（dict/list），统一序列化为 JSON 字符串。
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    try:
        return json.dumps(output, ensure_ascii=False)
    except Exception:
        return str(output)


def _split_tool_output_images(output) -> tuple[str, list[str]]:
    """从工具输出里分离图片，返回 (文本, 图片 URL 列表)。

    Codex / MCP 工具的 output 可能是 parts 数组，其中含 input_image。
    Chat 协议的 tool 消息只接字符串，图片若直接 json.dumps 会让 base64 白占 token
    且模型根本看不到；这里把图片摘出来单独承载。
    """
    urls: list[str] = []
    rest: list = []

    def _pull(node) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t in ("input_image", "image_url", "computer_screenshot"):
                url = node.get("image_url") or node.get("url") or node.get("source")
                if isinstance(url, dict):
                    url = url.get("url") or url.get("data")
                if isinstance(url, str) and url:
                    urls.append(url)
                    return
            if node.get("image_url"):
                url = node["image_url"]
                if isinstance(url, dict):
                    url = url.get("url", "")
                if isinstance(url, str) and url:
                    urls.append(url)
                    return
            rest.append({k: v for k, v in node.items() if k != "image_url"})
            return
        rest.append(node)

    if isinstance(output, list):
        for item in output:
            _pull(item)
        text = _normalize_tool_output(rest) if rest else ""
    else:
        text = _normalize_tool_output(output)

    return text, urls


def _normalize_call_arguments(item: dict) -> str:
    """把非 function_call 类工具的调用参数归一成 JSON 字符串。"""
    for key in ("arguments", "action", "input"):
        val = item.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            # 已经是 JSON 字符串就直接用，否则包一层
            try:
                json.loads(val)
                return val
            except Exception:
                return json.dumps({"input": val}, ensure_ascii=False)
        try:
            return json.dumps(val, ensure_ascii=False)
        except Exception:
            return json.dumps({"input": str(val)}, ensure_ascii=False)
    return "{}"


def _convert_tool_choice(tool_choice) -> Any:
    """Responses 的 tool_choice → Chat 格式。

    字符串（auto/none/required/none）两边通用；
    命名选择 Responses 是 {"type":"function","name":"shell"}，
    Chat 是 {"type":"function","function":{"name":"shell"}}。
    """
    if isinstance(tool_choice, dict) and "function" not in tool_choice:
        name = tool_choice.get("name", "")
        if name:
            return {"type": "function", "function": {"name": name}}
    return tool_choice


def _extract_content(content) -> str:
    """提取 content（可能是 str / list[{type,text}]）为纯文本（不含图片）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("input_text", "text"):
                    parts.append(p.get("text", ""))
                elif p.get("type") == "output_text":
                    parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts) or str(content)
    return str(content)


def _extract_content_parts(content) -> str | list:
    """提取 content；含图片（input_image）时返回 Chat 多模态 parts，否则返回纯文本。

    Responses 的 input_image 形如 {"type":"input_image","image_url":"data:..."}，
    image_url 可能是字符串，也可能是 {"url": ...}；统一转成 Chat 的 image_url part。
    """
    if not isinstance(content, list):
        return _extract_content(content)
    text_parts: list[str] = []
    image_parts: list[dict] = []
    for p in content:
        if isinstance(p, dict):
            t = p.get("type")
            if t in ("input_text", "text", "output_text"):
                text_parts.append(p.get("text", ""))
            elif t == "input_image":
                url = p.get("image_url", p.get("url", ""))
                if isinstance(url, dict):
                    url = url.get("url", "")
                if url:
                    image_parts.append({"type": "image_url", "image_url": {"url": url}})
        elif isinstance(p, str):
            text_parts.append(p)
    if not image_parts:
        text = "".join(text_parts)
        return text or _extract_content(content)
    parts: list[dict] = []
    text = "".join(text_parts).strip()
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(image_parts)
    return parts


def _extract_output_text(content_parts: list) -> str:
    """从 Responses output content parts 提取纯文本。"""
    texts = []
    for part in content_parts:
        if isinstance(part, dict) and part.get("type") == "output_text":
            texts.append(part.get("text", ""))
    return "".join(texts)


def _convert_tools_for_chat(tools: list, enable_web_search: bool = True
                            ) -> tuple[list, dict[str, tuple[str, str]], set[str]]:
    """将 Responses 格式的 tools 转为 Chat 格式。

    Responses:  {"type": "function", "name": "shell", "description": ..., "parameters": ...}
    Chat:       {"type": "function", "function": {"name": "shell", "description": ..., "parameters": ...}}

    Codex 0.117+ 会把 MCP 工具打包成 namespace 容器：
      {"type": "namespace", "name": "mcp__cua_repl", "description": ...,
       "tools": [{"type": "function", "name": "js", ...}, ...]}
    Chat 协议不认识 namespace，这里把子工具"提升"为顶层 function 工具，
    并返回 上游名→(namespace, 原始裸名) 的路由表；模型回传调用时由转换器把
    name 还原、namespace 字段补回，Codex 客户端据此路由到对应 MCP 服务器。

    **同名工具必须改名而不是丢弃**：Codex 实测会同时下发
    `mcp__cua_repl.js` 与 `mcp__node_repl.js`。早期实现按「先到先得」丢弃后者，
    模型仍会调用 `js`，结果被路由到 **错误** 的 MCP 服务器（比缺工具更危险）。
    现在重名的统一加 `<ns>__` 前缀保证唯一，路由时再还原。

    托管型工具 `{"type":"web_search"}` 上游承接不了（实测静默忽略），
    改为降级成一个普通 function 交给网关自己执行检索（见 `websearch.py`）。
    """
    from . import obs  # 延迟导入避免循环依赖

    result: list[dict] = []
    name_route: dict[str, tuple[str, str]] = {}
    internal_tools: set[str] = set()

    # ── 第一遍：统计裸名出现次数，用来判定哪些必须加前缀 ──────────────────
    counts: Counter[str] = Counter()
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "namespace":
            for sub in t.get("tools") or []:
                if isinstance(sub, dict) and sub.get("type") == "function":
                    counts[str(sub.get("name") or "")] += 1
        elif t.get("type") == "function" and t.get("name"):
            counts[str(t["name"])] += 1

    def _unique(candidate: str, ns_hint: str) -> str:
        """冲突时加 namespace 前缀，仍冲突则追加序号。"""
        name = candidate
        if counts.get(candidate, 0) > 1:
            name = f"{ns_hint}__{candidate}"
        i = 2
        while name in name_route:
            name = f"{ns_hint}__{candidate}_{i}"
            i += 1
        return name

    for t in tools:
        if not isinstance(t, dict):
            continue

        # ---- namespace 容器：提升内部 function 工具 ----
        if t.get("type") == "namespace":
            ns_name = str(t.get("name") or "")
            ns_hint = ns_name.replace("mcp__", "").replace("__", "_") or "ns"
            for sub in t.get("tools") or []:
                if not isinstance(sub, dict) or sub.get("type") != "function":
                    continue
                bare = str(sub.get("name") or "")
                if not bare:
                    obs.log(f"⚠ namespace {ns_name}: skip tool (empty name)")
                    continue
                up = _unique(bare, ns_hint)
                fn: dict[str, Any] = {"name": up}
                if sub.get("description"):
                    fn["description"] = sub["description"]
                if "parameters" in sub:
                    fn["parameters"] = sub["parameters"]
                if "strict" in sub:
                    fn["strict"] = sub["strict"]
                result.append({"type": "function", "function": fn})
                name_route[up] = (ns_name, bare)
            continue

        # ---- 托管型 web_search → 网关代跑的普通 function ----
        if t.get("type") == "web_search":
            if not enable_web_search:
                obs.log("⚠ 丢弃 web_search（网关侧搜索已关闭）")
                continue
            if websearch.TOOL_NAME in name_route:
                continue
            result.append(websearch.TOOL_SPEC)
            internal_tools.add(websearch.TOOL_NAME)
            name_route[websearch.TOOL_NAME] = ("", websearch.TOOL_NAME)
            continue

        if t.get("type") != "function":
            # 其余托管型工具（file_search / computer_use 等）无人承接，丢弃并记录
            obs.log("⚠ drop non-function tool: type=" + str(t.get("type"))
                    + " name=" + str(t.get("name")))
            continue

        # 已经是 Chat 格式（有 "function" key）
        if "function" in t:
            nm = str((t.get("function") or {}).get("name") or "")
            if nm in name_route:
                continue
            result.append(t)
            name_route[nm] = ("", nm)
            continue

        # Responses 扁平格式 → Chat 嵌套格式
        nm = str(t.get("name") or "")
        if nm in name_route:
            continue
        fn = {"name": nm}
        if "description" in t:
            fn["description"] = t["description"]
        if "parameters" in t:
            fn["parameters"] = t["parameters"]
        if "strict" in t:
            fn["strict"] = t["strict"]
        result.append({"type": "function", "function": fn})
        name_route[nm] = ("", nm)

    return result, name_route, internal_tools


# ---------------------------------------------------------------------------
# 响应转换：Chat → Responses
# ---------------------------------------------------------------------------

class ResponsesStreamConverter:
    """将 Chat SSE 流实时转换为 Responses API 语义事件流。

    用法：
      converter = ResponsesStreamConverter(model="glm-5.2")
      # 对后端返回的每个 SSE 行调 feed_line()
      # feed_line 返回要发送给客户端的 Responses 事件字符串（可能多行）
      for line in backend_sse:
          events = converter.feed_line(line)
          if events:
              yield events.encode()
      # 流结束后调 finish() 获取收尾事件
      yield converter.finish().encode()
    """

    def __init__(self, model: str = "unknown",
                 name_route: dict[str, tuple[str, str]] | None = None,
                 internal_tools: set[str] | None = None,
                 bare_to_ns: dict[str, str] | None = None):
        self.resp_id = _rand_id("resp_")
        self.msg_id = _rand_id("msg_")
        self.model = model
        self.created_at = int(time.time())
        # 路由表：上游可见名 → (namespace, 原始裸名)。模型回传调用时据此
        # 还原 name 并补 namespace 字段，Codex 客户端才能路由到正确的 MCP。
        self.name_route: dict[str, tuple[str, str]] = dict(name_route or {})
        if bare_to_ns:  # 兼容旧签名（纯字符串映射）
            for k, v in bare_to_ns.items():
                self.name_route.setdefault(k, (v, k))
        # 由网关代跑、不转发给客户端的工具名（如 web_search）
        self.internal_tools: set[str] = set(internal_tools or ())

        # 状态标记
        self._emitted_created = False
        self._emitted_msg_item = False
        self._emitted_content_part = False
        # message item 的 output_index：动态计算，避免与先到的 function_call 冲突
        self._msg_oi = 0

        # 累积内容
        self._content = ""
        self._tool_calls: dict[int, dict] = {}  # index → {id, name, args, fc_id, output_idx, emitted}
        # 多轮续流时的 index 偏移：内部工具循环会重开上游，各轮 tool_call 的
        # index 都从 0 开始，不偏移就会互相覆盖。
        self._idx_base = 0
        self._round = 0
        self._round_start = 0  # 本轮文本在 self._content 里的起点
        self._finish_reason: str | None = None
        self._usage: dict | None = None
        self._saw_done = False   # 是否见过上游的 [DONE]

    # ---- 内部工具（网关代跑） ----

    def internal_calls(self) -> list[dict]:
        """本轮被拦截、需要网关真正执行的工具调用。"""
        return [tc for _, tc in sorted(self._tool_calls.items()) if tc.get("internal")]

    def client_calls(self) -> list[dict]:
        """本轮已（或将）外发给客户端的工具调用（与 internal_calls 互补）。"""
        return [tc for _, tc in sorted(self._tool_calls.items()) if not tc.get("internal")]

    def round_content(self) -> str:
        """本轮（当前上游请求）模型产出的文本，不含更早的轮次。"""
        return self._content[self._round_start:]

    def all_content(self) -> str:
        return self._content

    def begin_next_round(self) -> None:
        """开一轮新的上游请求（内部工具循环用）。

        保留已向客户端声明的 output item，只把 tool_call 的 index 基线后移，
        避免新一轮的 index 0/1 覆盖上一轮记录。
        """
        self._round += 1
        self._round_start = len(self._content)
        if self._tool_calls:
            self._idx_base += max(self._tool_calls) + 1
        self._finish_reason = None

    # ---- 公开接口 ----

    def feed_line(self, line: str) -> str:
        """处理一行 SSE（如 'data: {...}'），返回转换后的 Responses 事件字符串。"""
        line = line.strip()
        if not line or not line.startswith("data:"):
            return ""
        data = line[5:].strip()
        if data == "[DONE]":
            self._saw_done = True
            return ""
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return ""
        return self._process_chunk(chunk)

    def _accumulate_usage(self, u: dict) -> None:
        """合并多次上游请求的 usage（内部工具循环每轮都会报一次）。"""
        if not self._usage:
            self._usage = dict(u)
            return
        for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                  "input_tokens", "output_tokens"):
            if isinstance(u.get(k), int):
                self._usage[k] = (self._usage.get(k) or 0) + u[k]
        for k in ("prompt_tokens_details", "completion_tokens_details"):
            if isinstance(u.get(k), dict):
                tgt = self._usage.setdefault(k, {})
                for kk, vv in u[k].items():
                    if isinstance(vv, int):
                        tgt[kk] = (tgt.get(kk) or 0) + vv

    def accumulated_usage(self) -> dict | None:
        """跨轮累加后的 usage（Chat Completions 形态，与发给客户端的一致）。

        obs 记账要用这个：内部工具循环会开多次上游请求，客户端拿到的是**累加值**，
        账本若只留最后一轮就会系统性少记（实测同一请求差 4~5 倍）。
        """
        return self._usage

    def finish(self, error: dict | None = None, incomplete: dict | None = None,
               terminate: bool = True) -> str:
        """流结束后发出终止事件。

        - 正常：output_text.done / content_part.done / output_item.done ×N → response.completed
        - 上游中断或出错：统一走 response.failed（带 error），客户端才能立刻给出反馈，
          而不是一直等一个永远不来的 response.completed。
        - 流被上游悄悄截断（没收到 finish_reason）：走 response.incomplete。
        - `terminate=False`：**中间轮**。网关要接着跑内部工具并重开上游，
          此时绝不能发 output_item.done / response.completed，否则客户端会认为
          整轮已结束、后续事件被丢弃。中间轮只返回空串，让客户端继续等。
        """
        if not terminate and error is None and incomplete is None:
            return ""
        if error is not None:
            return self._evt("response.failed", {
                "response": self._response_obj("failed", error=error)
            })
        if incomplete is not None:
            return self._evt("response.incomplete", {
                "response": self._response_obj("incomplete", incomplete_details=incomplete)
            })

        events: list[str] = []

        # 关闭 text content
        if self._emitted_content_part:
            events.append(self._evt("response.output_text.done", {
                "output_index": self._msg_oi, "content_index": 0, "text": self._content
            }))
            events.append(self._evt("response.content_part.done", {
                "output_index": self._msg_oi, "content_index": 0,
                "part": {"type": "output_text", "text": self._content, "annotations": []}
            }))

        if self._emitted_msg_item:
            events.append(self._evt("response.output_item.done", {
                "output_index": self._msg_oi,
                "item": self._msg_item("completed")
            }))

        # 关闭 function calls
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            if tc.get("emitted"):
                oi = tc["output_idx"]
                events.append(self._evt("response.function_call_arguments.done", {
                    "item_id": tc["fc_id"], "output_index": oi,
                    "call_id": tc["id"], "arguments": tc["args"]
                }))
                events.append(self._evt("response.output_item.done", {
                    "output_index": oi, "item": self._fc_item(tc, "completed")
                }))

        # response.completed
        events.append(self._evt("response.completed", {
            "response": self._response_obj("completed")
        }))
        return "".join(events)

    # ---- 流完整性判定 ----
    def saw_terminal_evidence(self) -> bool:
        """上游流是否留下了"正常结束"的证据。

        Chat 后端正常收尾会给出 finish_reason 或 [DONE]；两者都没有说明流被截断
        （连接被掐、网关超时等），此时不能把请求当成功记账。
        """
        return bool(self._finish_reason) or self._saw_done

    def note_done(self) -> None:
        self._saw_done = True

    def has_output(self) -> bool:
        return bool(self._content or self._tool_calls)

    def get_nonstream_response(self, error: dict | None = None,
                               incomplete: dict | None = None) -> dict:
        """流结束后获取完整的非流式 Response 对象。"""
        if error is not None:
            return self._response_obj("failed", error=error)
        if incomplete is not None:
            return self._response_obj("incomplete", incomplete_details=incomplete)
        return self._response_obj("completed")

    # ---- 内部 ----

    def _process_chunk(self, chunk: dict) -> str:
        events: list[str] = []

        # 模型名
        if chunk.get("model"):
            self.model = chunk["model"]

        # 首次 → 发 created + in_progress
        if not self._emitted_created:
            resp = self._response_obj("in_progress")
            events.append(self._evt("response.created", {"response": resp}))
            events.append(self._evt("response.in_progress", {"response": resp}))
            self._emitted_created = True

        # usage（累加：内部工具循环会开多次上游请求，用量应合并记账）
        if chunk.get("usage"):
            self._accumulate_usage(chunk["usage"])

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")

            # ---- content delta ----
            content = delta.get("content")
            if content:
                if not self._emitted_msg_item:
                    # 若 function_call 已先发出，message 的 output_index 要排在其后
                    if self._tool_calls:
                        self._msg_oi = max(tc["output_idx"] for tc in self._tool_calls.values()) + 1
                    events.append(self._evt("response.output_item.added", {
                        "output_index": self._msg_oi,
                        "item": self._msg_item("in_progress", empty=True)
                    }))
                    self._emitted_msg_item = True

                if not self._emitted_content_part:
                    events.append(self._evt("response.content_part.added", {
                        "output_index": self._msg_oi, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}
                    }))
                    self._emitted_content_part = True

                self._content += content
                events.append(self._evt("response.output_text.delta", {
                    "output_index": self._msg_oi, "content_index": 0, "delta": content
                }))

            # ---- tool_calls delta ----
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0) + self._idx_base
                if idx not in self._tool_calls:
                    # 计算 output_index：msg 占 0，function_call 从 1 开始（如果有 msg）
                    base = 1 if (self._emitted_msg_item or self._content) else 0
                    oi = base + len(self._tool_calls)
                    self._tool_calls[idx] = {
                        "id": tc.get("id", ""),
                        "name": "",
                        "args": "",
                        "fc_id": _rand_id("fc_"),
                        "output_idx": oi,
                        "emitted": False,
                    }
                slot = self._tool_calls[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    # 兼容三种上游行为：一次给全 / 重复给全 / 分片拼接
                    if not slot["name"]:
                        slot["name"] = fn["name"]
                    elif fn["name"].startswith(slot["name"]):
                        slot["name"] = fn["name"]
                    elif not slot["name"].endswith(fn["name"]):
                        slot["name"] += fn["name"]

                name = slot["name"]
                # name 还没拼完（仍是某内部工具名的真前缀）→ 先观望，
                # 别急着向客户端声明一个可能是 web_search 的工具调用
                if not slot["emitted"] and any(
                    t != name and t.startswith(name) for t in self.internal_tools
                ):
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                    continue

                # 内部工具（如 web_search）：完全不外发，只把参数攒起来
                # 交给网关自己执行，客户端全程无感
                if name in self.internal_tools:
                    slot["internal"] = True
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                    continue

                if not slot["emitted"]:
                    events.append(self._evt("response.output_item.added", {
                        "output_index": slot["output_idx"],
                        "item": self._fc_item(slot, "in_progress")
                    }))
                    slot["emitted"] = True

                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    events.append(self._evt("response.function_call_arguments.delta", {
                        "item_id": slot["fc_id"],
                        "output_index": slot["output_idx"],
                        "call_id": slot["id"],
                        "delta": fn["arguments"]
                    }))

            if finish:
                self._finish_reason = finish

        return "".join(events)

    def _evt(self, event_type: str, data: dict) -> str:
        """格式化一个 SSE 事件。"""
        payload = {"type": event_type, **data}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _msg_item(self, status: str = "in_progress", empty: bool = False) -> dict:
        content = [] if empty else [
            {"type": "output_text", "text": self._content, "annotations": []}
        ]
        return {
            "type": "message",
            "id": self.msg_id,
            "status": status,
            "role": "assistant",
            "content": content,
        }

    def _fc_item(self, tc: dict, status: str) -> dict:
        # call_id 兜底：部分后端的 delta 不带 id，缺了 Codex 无法回传工具结果。
        # 直接写回 slot，保证 added/done 事件与最终 response 里一致。
        if not tc.get("id"):
            tc["id"] = _rand_id("call_")
        item = {
            "type": "function_call",
            "id": tc["fc_id"],
            "call_id": tc["id"],
            "name": tc["name"],
            "arguments": tc["args"],
            "status": status,
        }
        # 路由还原：Codex 0.117+ 靠 namespace + name 把调用分派到对应 MCP 服务器。
        # 重名工具在上游侧被加了 `<ns>__` 前缀，这里必须把 name 改回原始裸名，
        # 否则客户端会找不到该工具。
        route = self.name_route.get(tc["name"])
        if route:
            ns, orig = route
            if ns:
                item["namespace"] = ns
            if orig and orig != tc["name"]:
                item["name"] = orig
        return item

    def _response_obj(self, status: str, error: dict | None = None,
                      incomplete_details: dict | None = None) -> dict:
        """按 output_index 顺序拼最终 output 数组。

        早期实现按"先 message 再 function_call"固定顺序拼装，当模型先出工具调用、
        后出结论文本时，数组顺序会与事件里声明的 output_index 不一致，
        客户端按 output_index 取项就会错位。
        """
        indexed: list[tuple[int, dict]] = []
        if self._emitted_msg_item or self._content:
            indexed.append((self._msg_oi, self._msg_item(status)))
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            if tc.get("emitted"):
                indexed.append((tc["output_idx"], self._fc_item(tc, status)))
        indexed.sort(key=lambda pair: pair[0])
        output = [item for _, item in indexed]

        usage = None
        if self._usage:
            u = self._usage
            pt = u.get("prompt_tokens_details") or {}
            ct = u.get("completion_tokens_details") or {}
            usage = {
                "input_tokens": u.get("prompt_tokens", u.get("input_tokens", 0)) or 0,
                "input_tokens_details": {"cached_tokens": pt.get("cached_tokens", 0) or 0},
                "output_tokens": u.get("completion_tokens", u.get("output_tokens", 0)) or 0,
                "output_tokens_details": {"reasoning_tokens": ct.get("reasoning_tokens", 0) or 0},
                "total_tokens": u.get("total_tokens", 0) or 0,
            }

        return {
            "id": self.resp_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "error": error,
            "incomplete_details": incomplete_details,
            "model": self.model,
            "output": output,
            "parallel_tool_calls": True,
            "store": False,
            "metadata": {},
            "truncation": "disabled",
            "usage": usage,
        }
