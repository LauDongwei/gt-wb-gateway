"""
responses_projection — /v1/responses 的后端投影层。

目标
----
Codex CLI 会把大量运行时提示、完整工具 schema、长历史、以及工具输出一并塞进
/v1/responses 请求里。我们希望在保住 agent 能继续干活的前提下，把请求压到
后端稳定接受的规模。

三档策略
--------
1. **透明档**：消息字符 ≤ `TRANSPARENT_CHAR_LIMIT` → 完全原样透传，只补语言规则。
2. **弹性档**（压缩默认档）：超过阈值 → 按比例分配保留额度（`_message_budget`），
   最近的消息逐字保留，更早的压成摘要；**不动**系统提示词与工具描述。
3. **退化档**：`desensitize(force_compact=True)` 路径（仅命中审核后重试时使用），
   才会把系统提示词摘要化、丢 harness 消息。

历史教训（2026-09-19 实测）
-------------------------
早期是"二元开关"：>120k 字符就压到 ~6k 字符。Mac 端长会话实测 671k → 5.8k，
模型失去全部历史与操作手册，agent loop 反复重跑同一命令、不收敛。
同批证据还显示上游能接受 ≥877k 字符的载荷，"必须牺牲上下文"并不成立。
"""

from __future__ import annotations

import json
import os
from typing import Any


AGENTIC_TOOL_NAMES = {
    "exec_command",
    "write_stdin",
    "update_plan",
    "request_user_input",
    "view_image",
    "get_goal",
    "create_goal",
    "update_goal",
    "apply_patch",
    "tool_search_tool",
}

HARNESS_USER_MARKERS = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<system-reminder>",
    "# claudeMd",
)

HARNESS_SYSTEM_MARKERS = (
    "You are a coding agent running in the Codex CLI",
    "Within this context, Codex refers to",
    "# AGENTS.md spec",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "The following deferred tools are now available via ToolSearch.",
    "### Available skills",
    "## request_user_input availability",
    "You are Claude Code",
)

BASE_SYSTEM_PROMPT = (
    "You are a coding assistant serving an OpenAI-compatible coding client (desktop app or CLI). "
    "Be precise, concise, safe, and action-oriented. "
    "Always respond in the same language the user writes in. "
    "Use available tools when needed, follow repository instructions and durable user context, "
    "and continue from the preserved recent context. "
    "If earlier history was condensed, rely on the preserved recent messages and rerun tools when exact old details are required."
)

# 语言跟随：上下文经投影压缩后英文脚手架密度高，模型容易跟英文走，
# 这里根据最后一条用户消息显式指定回复语言（CJK 才注入，英文等默认不干预）
def _detect_user_language(messages: list[dict]) -> str:
    """从最后一条用户消息检测 CJK 语言；非 CJK 返回空串。"""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _content_to_text(msg.get("content", ""))
            if not text:
                continue
            zh = sum('\u4e00' <= c <= '\u9fff' for c in text)
            ja = sum('\u3040' <= c <= '\u30ff' for c in text)
            ko = sum('\uac00' <= c <= '\ud7af' for c in text)
            if zh == ja == ko == 0:
                return ""
            if ja > zh and ja >= ko:
                return "Japanese (日本語)"
            if ko > zh and ko > ja:
                return "Korean (한국어)"
            return "Chinese (简体中文)"
    return ""


def _language_rule(messages: list[dict]) -> str:
    lang = _detect_user_language(messages)
    if not lang:
        return ""
    return (
        "LANGUAGE RULE: Write ALL your output in " + lang + " — including any text "
        "before or between tool calls. Never use any other language."
    )


def _append_language_rule(system_content: str, rule: str) -> str:
    return system_content + "\n\n" + rule if rule else system_content


def _append_tail_language_note(messages: list[dict], lang: str) -> None:
    """在最后一条用户消息尾部追加就近语言提示。

    开头的 system 级语言规则对「要调工具」的回复约束力弱（模型常用英文写
    工具调用前的开场白），尾部就近提示实测约束力强得多。
    """
    if not lang:
        return
    note = f"\n\n[MANDATORY: Write all your text in {lang}, including text before tool calls.]"
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = c + note
            elif isinstance(c, list):
                for part in reversed(c):
                    if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                        part["text"] = part.get("text", "") + note
                        break
            return

HISTORY_PREFIX = "Earlier conversation summary (condensed):"

# ── 上下文预算（弹性，替代早期"二元开关"）──────────────────────────────────
#
# 早期实现：字符数 ≤120k 原样透传，一旦超过就压到 ~6k 字符（丢 99%）。
# 实测后果：Mac 端 Codex 的长会话（671k 字符）每轮被压成 5.8k 字符，
# 模型失去全部历史与操作手册 → 反复重跑同一命令、agent loop 不收敛。
#
# 实测上游 /v2/chat/completions 接受 ≥877k 字符（≈22 万 token）的载荷并返回 200，
# 说明"上下文过长"并不是必须牺牲上下文的理由。
#
# 新策略：
#   1) 消息字符 ≤ TRANSPARENT_CHAR_LIMIT → 完全原样透传（只加语言规则）
#   2) 超过 → 按比例保留（KEEP_RATIO），下限 MIN_MESSAGE_BUDGET，
#      且"消息 + 工具"总量不超过 TOTAL_CHAR_CEILING
#   3) 保留时优先保最近的消息（逐字完整），更早的才压缩成摘要
TRANSPARENT_CHAR_LIMIT = 600_000
TOTAL_CHAR_CEILING = 800_000
KEEP_RATIO = 0.6
MIN_MESSAGE_BUDGET = 100_000
SUMMARY_SHARE = 0.25          # 预算里分给"历史摘要"的比例，其余给最近的完整消息

# 单条消息/单块内容的截断上限。数值偏大是刻意的：Codex 的 instructions
# 有 2 万字符左右，工具描述是模型选对工具的唯一依据，都不能按"摘要"对待。
MAX_SYSTEM_GUIDANCE_CHARS = 24_000
MAX_USER_CHARS = 24_000
MAX_ASSISTANT_CHARS = 12_000
MAX_TOOL_OUTPUT_CHARS = 8_000
MAX_TOOL_ARGS_CHARS = 4_000
MAX_HISTORY_SUMMARY_CHARS = 40_000
MAX_HISTORY_ITEMS = 60
MAX_HISTORY_LINE_CHARS = 400
MAX_TAIL_MESSAGES = 400
MAX_TAIL_CHARS = 400_000

SCHEMA_KEEP_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "oneOf",
    "anyOf",
    "allOf",
    "additionalProperties",
    "format",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "nullable",
}


def project_responses_chat_body(body: dict) -> tuple[dict, dict]:
    """把 Responses 转出来的 Chat body 投影成更适合腾讯后端的最小上下文。"""
    projected = dict(body)
    messages = list(body.get("messages") or [])
    tools = list(body.get("tools") or [])

    projected_tools, tool_stats = _project_tools(tools)
    if projected_tools:
        projected["tools"] = projected_tools
    elif "tools" in projected:
        projected["tools"] = []

    lang_rule = _language_rule(messages)

    # ---- 透明模式：上下文在安全阈值内时原样透传，模型看到的就是 Codex 发的 ----
    # （学习 sub2api：网关不改写对话内容，模型才能发挥原有水准）
    if _messages_size(messages) <= TRANSPARENT_CHAR_LIMIT:
        transparent_msgs = messages
        if lang_rule:
            applied = False
            for m in transparent_msgs:
                if isinstance(m, dict) and m.get("role") == "system":
                    m["content"] = _append_language_rule(m.get("content", ""), lang_rule)
                    applied = True
                    break
            if not applied:
                transparent_msgs = [{"role": "system", "content": lang_rule}] + transparent_msgs
        _append_tail_language_note(transparent_msgs, _detect_user_language(messages))
        projected["messages"] = transparent_msgs
        projected["tools"] = tools  # 完整 schema + description，不做任何剪枝
        return projected, {
            "mode": "transparent",
            "aggressive": False,
            "original_messages": len(messages),
            "projected_messages": len(transparent_msgs),
            "original_message_chars": _messages_size(messages),
            "projected_message_chars": _messages_size(transparent_msgs),
            "original_tools": len(tools),
            "projected_tools": len(tools),
            "original_tool_chars": _tools_size(tools),
            "projected_tool_chars": _tools_size(tools),
        }

    aggressive = _looks_like_agentic_cli(messages, tools)
    if not aggressive:
        conservative_msgs = _project_messages_conservative(messages)
        if lang_rule:
            for m in conservative_msgs:
                if isinstance(m, dict) and m.get("role") == "system":
                    m["content"] = _append_language_rule(m.get("content", ""), lang_rule)
                    break
            else:
                conservative_msgs.insert(0, {"role": "system", "content": lang_rule})
        _append_tail_language_note(conservative_msgs, _detect_user_language(messages))
        projected["messages"] = conservative_msgs
        return projected, {
            "mode": "conservative",
            "aggressive": False,
            "original_messages": len(messages),
            "projected_messages": len(projected["messages"]),
            "original_message_chars": _messages_size(messages),
            "projected_message_chars": _messages_size(projected["messages"]),
            **tool_stats,
        }

    # ---- 压缩档：不再"一刀切砸成 6k"，而是按比例分配保留额度 ----
    budget = _message_budget(_messages_size(messages), tool_stats.get("projected_tool_chars", 0))
    tail_budget = max(int(budget * (1 - SUMMARY_SHARE)), 8_000)
    summary_budget = max(budget - tail_budget, 4_000)

    tool_name_by_call_id = _build_tool_call_name_map(messages)
    system_messages: list[dict] = []      # 真实系统提示词（Codex instructions / 仓库指令）
    conversation: list[dict] = []
    dropped_harness_messages = 0
    first_task_idx: int | None = None     # 第一条"真正的用户任务"

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = _content_to_text(msg.get("content", ""))

        if role == "system":
            # 只在"退化档"（force_compact）里才把系统提示词摘要化；
            # 常规压缩档必须原样保留，否则模型失去工具使用规范。
            if _looks_like_harness_system(text) and os.environ.get("GTWB_DROP_HARNESS") == "1":
                dropped_harness_messages += 1
                continue
            guidance = _truncate_text(text, MAX_SYSTEM_GUIDANCE_CHARS)
            if guidance:
                system_messages.append({"role": "system", "content": guidance})
            continue

        if role == "user" and _looks_like_harness_user(text):
            # harness 注入的 user 消息（AGENTS.md / environment_context）承载仓库规范，
            # 压缩档同样保留（截断），只有显式要求时才丢。
            truncated = _truncate_text(text, MAX_USER_CHARS)
            if truncated:
                conversation.append({"role": "user", "content": truncated})
            else:
                dropped_harness_messages += 1
            continue

        if role == "user" and first_task_idx is None:
            first_task_idx = len(conversation)

        projected_msg = _project_conversation_message(msg)
        if projected_msg is not None:
            conversation.append(projected_msg)

    if not conversation:
        conversation = _project_messages_conservative(messages)

    tail_start = _choose_tail_start(conversation, tail_budget)
    tail_start = _expand_tail_for_tool_context(conversation, tail_start)
    latest_user_idx = _latest_user_index(conversation)

    # 锚点一：原始任务陈述。agent 会话再长，用户的原始诉求都不能丢。
    anchor_task = None
    if first_task_idx is not None and first_task_idx < tail_start:
        anchor_task = dict(conversation[first_task_idx])

    # 锚点二：窗口之前的最后一条用户消息（最新意图）
    anchor_user = None
    if latest_user_idx is not None and latest_user_idx < tail_start:
        anchor_user = dict(conversation[latest_user_idx])

    omitted: list[dict] = []
    for idx, msg in enumerate(conversation):
        if idx >= tail_start:
            break
        if anchor_user is not None and latest_user_idx is not None and idx == latest_user_idx:
            continue
        if anchor_task is not None and idx == first_task_idx:
            continue
        omitted.append(msg)

    # 没有真实系统提示词时才补我们自己的中性基础提示词
    if system_messages:
        final_messages: list[dict] = list(system_messages)
        if lang_rule:
            first = final_messages[0]
            first["content"] = _append_language_rule(first.get("content", ""), lang_rule)
    else:
        final_messages = [{"role": "system", "content": _append_language_rule(BASE_SYSTEM_PROMPT, lang_rule)}]

    history_summary = _build_history_summary(omitted, tool_name_by_call_id, summary_budget)
    if history_summary:
        final_messages.append({"role": "system", "content": history_summary})

    if anchor_task is not None:
        final_messages.append(anchor_task)
    if anchor_user is not None:
        final_messages.append(anchor_user)

    final_messages.extend(conversation[tail_start:])
    final_messages = _ensure_tool_pairing(final_messages)
    _append_tail_language_note(final_messages, _detect_user_language(messages))
    projected["messages"] = final_messages

    return projected, {
        "mode": "elastic",
        "aggressive": True,
        "budget_chars": budget,
        "dropped_harness_messages": dropped_harness_messages,
        "preserved_system_messages": len(system_messages),
        "summarized_history_messages": len(omitted),
        "anchor_task_preserved": anchor_task is not None,
        "anchor_user_preserved": anchor_user is not None,
        "tail_messages": len(conversation[tail_start:]),
        "original_messages": len(messages),
        "projected_messages": len(final_messages),
        "original_message_chars": _messages_size(messages),
        "projected_message_chars": _messages_size(final_messages),
        **tool_stats,
    }


def _message_budget(message_chars: int, tool_chars: int) -> int:
    """弹性预算：按比例保留，同时给工具 schema 留出空间，总量不超天花板。"""
    room = max(TOTAL_CHAR_CEILING - max(tool_chars, 0), MIN_MESSAGE_BUDGET)
    scaled = int(message_chars * KEEP_RATIO)
    return max(MIN_MESSAGE_BUDGET, min(room, scaled))


def _looks_like_agentic_cli(messages: list[dict], tools: list[dict]) -> bool:
    tool_names = {
        _tool_name(tool)
        for tool in tools
        if _tool_name(tool)
    }
    if tool_names & AGENTIC_TOOL_NAMES:
        return True

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = _content_to_text(msg.get("content", ""))
        if _looks_like_harness_user(text) or _looks_like_harness_system(text):
            return True
    return False


def _project_messages_conservative(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for msg in messages:
        projected = _project_conversation_message(msg, conservative=True)
        if projected is not None:
            out.append(projected)
    return out


def _extract_images(content: Any) -> list[dict]:
    """收集 chat content 里的 image_url 图片块（投影时保真透传）。"""
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]


def _project_conversation_message(msg: dict, conservative: bool = False) -> dict | None:
    if not isinstance(msg, dict):
        return None

    role = msg.get("role")
    out = dict(msg)

    if role == "system":
        text = _content_to_text(msg.get("content", ""))
        out["content"] = _truncate_text(text, MAX_SYSTEM_GUIDANCE_CHARS)
        return out

    if role == "user":
        content = msg.get("content", "")
        images = _extract_images(content)
        text = _content_to_text(content)
        if images:
            # 带图消息：文本截断后与图片一起以多模态 parts 透传，绝不能丢图
            parts: list[dict] = []
            text = _truncate_text(text, MAX_USER_CHARS)
            if text:
                parts.append({"type": "text", "text": text})
            parts.extend(images)
            out["content"] = parts
        else:
            out["content"] = _truncate_text(text, MAX_USER_CHARS)
        return out

    if role == "assistant":
        text = _content_to_text(msg.get("content", ""))
        out["content"] = _summarize_free_text(text, MAX_ASSISTANT_CHARS)
        tool_calls = []
        for tool_call in msg.get("tool_calls") or []:
            projected_call = _project_tool_call(tool_call)
            if projected_call is not None:
                tool_calls.append(projected_call)
        if tool_calls:
            out["tool_calls"] = tool_calls
        elif "tool_calls" in out:
            out.pop("tool_calls", None)
        return out

    if role == "tool":
        out["content"] = _summarize_tool_output(_content_to_text(msg.get("content", "")))
        return out

    if conservative:
        text = _content_to_text(msg.get("content", ""))
        out["content"] = _truncate_text(text, MAX_ASSISTANT_CHARS)
        return out

    return None


def _project_tool_call(tool_call: dict) -> dict | None:
    if not isinstance(tool_call, dict):
        return None

    function = tool_call.get("function") or {}
    name = function.get("name", "")
    arguments = function.get("arguments", "")

    return {
        "id": tool_call.get("id") or "call_" + _rand_suffix(),
        "type": tool_call.get("type", "function"),
        "function": {
            "name": name,
            "arguments": _summarize_tool_arguments(name, arguments),
        },
    }


def _rand_suffix() -> str:
    import os
    return os.urandom(6).hex()


def _ensure_tool_pairing(messages: list[dict]) -> list[dict]:
    """保障 assistant(tool_calls) 与 tool 结果消息的配对完整性（Chat 协议硬约束）。

    - assistant 声明了 tool_calls 但结果被上下文裁掉 → 补占位 tool 消息，避免后端 400
    - tool 消息找不到声明对应 call_id 的 assistant → 丢弃孤儿，避免"无源之果"
    """
    out: list[dict] = []
    pending: list[str] = []   # 当前 assistant 声明、尚未应答的 call_id
    answered: set[str] = set()

    def _flush_missing():
        for cid in pending:
            if cid not in answered:
                out.append({"role": "tool", "tool_call_id": cid,
                            "content": "[tool output omitted from context]"})

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            if pending:
                _flush_missing()
            out.append(msg)
            pending = [tc.get("id") for tc in msg["tool_calls"]
                       if isinstance(tc, dict) and tc.get("id")]
            answered = set()
            continue
        if role == "tool":
            cid = msg.get("tool_call_id")
            if cid in pending and cid not in answered:
                out.append(msg)
                answered.add(cid)
            continue
        if pending:
            _flush_missing()
            pending, answered = [], set()
        out.append(msg)
    if pending:
        _flush_missing()
    return out


def _summarize_tool_arguments(name: str, arguments: Any) -> str:
    if not isinstance(arguments, str):
        try:
            return json.dumps(arguments, ensure_ascii=False)
        except Exception:
            return json.dumps({"summary": _truncate_text(str(arguments), 240)}, ensure_ascii=False)

    if len(arguments) <= MAX_TOOL_ARGS_CHARS:
        return arguments

    if name == "apply_patch":
        return json.dumps(
            {"summary": "Large apply_patch payload omitted; a patch was prepared or applied in a previous step."},
            ensure_ascii=False,
        )

    try:
        parsed = json.loads(arguments)
    except Exception:
        return json.dumps({"summary": _truncate_text(arguments, 320)}, ensure_ascii=False)

    return json.dumps(_shrink_json_value(parsed), ensure_ascii=False)


def _shrink_json_value(value: Any, depth: int = 0, key: str = "") -> Any:
    if depth >= 4:
        return "<omitted>"

    if isinstance(value, dict):
        out = {}
        items = list(value.items())
        for idx, (item_key, item_value) in enumerate(items):
            if idx >= 12:
                out["_omitted_keys"] = len(items) - idx
                break
            out[item_key] = _shrink_json_value(item_value, depth + 1, item_key)
        return out

    if isinstance(value, list):
        trimmed = [_shrink_json_value(item, depth + 1, key) for item in value[:6]]
        if len(value) > 6:
            trimmed.append(f"<omitted {len(value) - 6} items>")
        return trimmed

    if isinstance(value, str):
        limit = 240 if key in {"cmd", "chars", "patch", "content", "text", "question"} else 120
        return _truncate_text(value, limit)

    return value


def _project_tools(tools: list[dict]) -> tuple[list[dict], dict]:
    projected = []
    original_chars = _tools_size(tools)

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        if tool.get("type") != "function":
            continue

        function = tool.get("function") or tool
        name = function.get("name")
        if not name:
            continue

        projected_function: dict[str, Any] = {"name": name}
        # 保留工具描述（截断控 token）：模型需要 description 才知道工具用途，
        # 尤其是提升后的 MCP namespace 工具（如 computer use / node_repl），
        # 纯名字+裸 schema 模型不会调用
        desc = function.get("description")
        if desc:
            projected_function["description"] = desc[:500]
        if "parameters" in function:
            projected_function["parameters"] = _project_schema(function.get("parameters"))
        if "strict" in function:
            projected_function["strict"] = function.get("strict")

        projected.append({"type": "function", "function": projected_function})

    return projected, {
        "original_tools": len(tools),
        "projected_tools": len(projected),
        "original_tool_chars": original_chars,
        "projected_tool_chars": _tools_size(projected),
    }


def _project_schema(schema: Any, depth: int = 0) -> Any:
    if depth >= 6:
        return {"type": "object"}

    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key not in SCHEMA_KEEP_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                out["properties"] = {
                    prop: _project_schema(prop_schema, depth + 1)
                    for prop, prop_schema in value.items()
                }
            elif key == "items":
                out["items"] = _project_schema(value, depth + 1)
            elif key in {"oneOf", "anyOf", "allOf"} and isinstance(value, list):
                out[key] = [_project_schema(item, depth + 1) for item in value[:6]]
            elif key == "additionalProperties" and isinstance(value, dict):
                out[key] = _project_schema(value, depth + 1)
            else:
                out[key] = value
        return out or {"type": "object"}

    if isinstance(schema, list):
        return [_project_schema(item, depth + 1) for item in schema[:6]]

    return schema


def _choose_tail_start(messages: list[dict], budget_chars: int | None = None) -> int:
    """从尾部向前累计，直到用满预算 —— 返回仍可"逐字保留"的起始下标。

    早期实现用固定 7000 字符 / 8 条消息，长会话下等于把历史整体丢掉；
    现在额度由调用方按输入规模算出来（见 _message_budget）。
    """
    if not messages:
        return 0

    limit_chars = max(int(budget_chars if budget_chars is not None else MAX_TAIL_CHARS), 1_000)
    limit_msgs = max(MAX_TAIL_MESSAGES, 1)

    start = len(messages) - 1
    total_chars = 0
    kept = 0

    for idx in range(len(messages) - 1, -1, -1):
        cost = _message_cost(messages[idx])
        if kept > 0 and (kept >= limit_msgs or total_chars + cost > limit_chars):
            break
        start = idx
        total_chars += cost
        kept += 1
    return start


def _expand_tail_for_tool_context(messages: list[dict], start: int) -> int:
    if start <= 0 or not messages:
        return start

    needed_call_ids = {
        msg.get("tool_call_id")
        for msg in messages[start:]
        if isinstance(msg, dict) and msg.get("role") == "tool" and msg.get("tool_call_id")
    }
    if not needed_call_ids:
        return start

    expanded = start
    for idx in range(start - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") != "assistant":
            continue
        call_ids = {
            tool_call.get("id")
            for tool_call in msg.get("tool_calls") or []
            if isinstance(tool_call, dict)
        }
        if call_ids & needed_call_ids:
            expanded = idx
            needed_call_ids -= call_ids
            if not needed_call_ids:
                break
    return expanded


def _latest_user_index(messages: list[dict]) -> int | None:
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            return idx
    return None


def _build_history_summary(messages: list[dict], tool_name_by_call_id: dict[str, str],
                           budget_chars: int | None = None) -> str:
    """把被挤出窗口的早期消息压成摘要行。额度由调用方按输入规模分配。"""
    lines: list[str] = []
    total_chars = 0
    summarized = 0
    char_limit = max(int(budget_chars if budget_chars is not None else MAX_HISTORY_SUMMARY_CHARS), 2_000)

    for msg in messages:
        line = _history_line(msg, tool_name_by_call_id)
        if not line:
            continue
        if summarized >= MAX_HISTORY_ITEMS or total_chars + len(line) > char_limit:
            break
        lines.append(f"- {line}")
        total_chars += len(line)
        summarized += 1

    remaining = len(messages) - summarized
    if remaining > 0:
        lines.append(f"- {remaining} earlier messages or tool results were further condensed.")

    if not lines:
        return ""
    return HISTORY_PREFIX + "\n" + "\n".join(lines)


def _history_line(msg: dict, tool_name_by_call_id: dict[str, str]) -> str:
    role = msg.get("role")
    text = _content_to_text(msg.get("content", ""))
    cap = MAX_HISTORY_LINE_CHARS

    if role == "user":
        return f"User asked: {_truncate_text(text, cap)}"

    if role == "assistant":
        tool_names = [
            (tool_call.get("function") or {}).get("name")
            for tool_call in msg.get("tool_calls") or []
            if isinstance(tool_call, dict)
        ]
        tool_names = [name for name in tool_names if name]
        if text and tool_names:
            return f"Assistant replied: {_truncate_text(text, cap)} Then called tools: {', '.join(tool_names[:6])}."
        if tool_names:
            return f"Assistant called tools: {', '.join(tool_names[:6])}."
        if text:
            return f"Assistant replied: {_truncate_text(text, cap)}"
        return ""

    if role == "tool":
        tool_name = tool_name_by_call_id.get(msg.get("tool_call_id", ""), "tool")
        summary = _tool_output_inline_summary(text)
        return f"Tool {tool_name} returned: {summary}"

    if role == "system":
        return f"System guidance: {_truncate_text(text, cap)}"

    return ""


def _build_tool_call_name_map(messages: list[dict]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tool_call in msg.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            call_id = tool_call.get("id")
            name = (tool_call.get("function") or {}).get("name")
            if call_id and name:
                mapping[call_id] = name
    return mapping


def _summarize_tool_output(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= MAX_TOOL_OUTPUT_CHARS and text.count("\n") <= 24:
        return text

    lines = text.splitlines()
    exit_line = next((line.strip() for line in lines if "Process exited with code" in line), "")
    useful_lines = []
    saw_output = False
    for line in lines:
        stripped = line.rstrip()
        if stripped == "Output:":
            saw_output = True
            continue
        if (
            stripped.startswith("Chunk ID:")
            or stripped.startswith("Wall time:")
            or stripped.startswith("Original token count:")
            or stripped.startswith("Process exited with code")
        ):
            continue
        useful_lines.append(stripped)

    body_lines = useful_lines

    head = body_lines[:10]
    tail = body_lines[-6:] if len(body_lines) > 16 else []
    omitted = max(len(body_lines) - len(head) - len(tail), 0)

    parts: list[str] = []
    if exit_line:
        parts.append(exit_line)
    if head:
        parts.append("Key output:")
        parts.extend(head)
    if omitted:
        parts.append(f"... [omitted {omitted} lines] ...")
    if tail:
        parts.append("Recent tail:")
        parts.extend(tail)

    summary = "\n".join(part for part in parts if part).strip()
    return _truncate_text(summary or text, MAX_TOOL_OUTPUT_CHARS)


def _tool_output_inline_summary(text: str) -> str:
    summarized = _summarize_tool_output(text)
    summarized = summarized.replace("\n", " | ")
    return _truncate_text(summarized, MAX_HISTORY_LINE_CHARS)


def _summarize_free_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text

    head = text[: limit // 2].rstrip()
    tail = text[-(limit // 3):].lstrip()
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


def _truncate_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 24, 0)].rstrip() + f" ... [truncated {len(text) - max(limit - 24, 0)} chars]"


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block.get("text", "")))
                elif "output" in block:
                    parts.append(str(block.get("output", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def _looks_like_harness_user(text: str) -> bool:
    return any(marker in text for marker in HARNESS_USER_MARKERS)


def _looks_like_harness_system(text: str) -> bool:
    return any(marker in text for marker in HARNESS_SYSTEM_MARKERS)


def _message_cost(msg: dict) -> int:
    cost = len(_content_to_text(msg.get("content", "")))
    for tool_call in msg.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        cost += len(function.get("name", ""))
        cost += len(function.get("arguments", ""))
    return cost


def _messages_size(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        total += _message_cost(msg)
        total += len(msg.get("role", ""))
    return total


def _tool_name(tool: dict) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function") or tool
    return str(function.get("name", "") or "")


def _tools_size(tools: list[dict]) -> int:
    try:
        return len(json.dumps(tools, ensure_ascii=False))
    except Exception:
        return 0
