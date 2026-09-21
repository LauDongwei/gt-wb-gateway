"""gt-wb-gateway 自测。零第三方依赖（除被测包本身），直接运行：

    python tests/test_gateway.py

覆盖那些「靠真实请求无法稳定触发」的逻辑：错误分类、冷却/熔断状态机、
登录态解析的各种畸形输入、模型清单抽取。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtwb import upstream  # noqa: E402
from gtwb.auth import parse_account  # noqa: E402
from gtwb.config import Config  # noqa: E402
from gtwb.resilience import HealthRegistry, _next_hour  # noqa: E402
from gtwb.upstream import ErrKind, _extract_model_ids  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}  {detail}")


# ══════════════════════════════════════════════════════════════════════════
def test_classify() -> None:
    print("\n[错误分类]")
    cases = [
        (402, "余额不足", ErrKind.HARD_CREDIT),
        (402, "", ErrKind.HARD_CREDIT),
        (429, "too many requests", ErrKind.SOFT_RATE),
        (401, '{"code":12153,"msg":"Offline user session not found"}', ErrKind.SESSION_DEAD),
        (401, "plain unauthorized", ErrKind.AUTH),
        (404, "page not found", ErrKind.NOT_FOUND),
        (503, "bad gateway", ErrKind.SERVER),
        (400, "invalid request", ErrKind.CLIENT),
        (200, '{"code":0,"msg":"OK"}', ErrKind.NONE),
        (200, '{"code":11140}', ErrKind.SOFT_RATE),
    ]
    for status, body, want in cases:
        got = upstream.classify(status, body)
        check(f"{status} + {body[:32]!r} → {want.value}", got is want, f"得到 {got.value}")


def test_cooldown_machine() -> None:
    print("\n[冷却 / 熔断状态机]")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(state_file=str(Path(td) / "state.json"))
        cfg.soft_cooldown_s = 10
        cfg.breaker_threshold = 2
        cfg.breaker_cooldown_s = 10
        h = HealthRegistry(cfg)
        uid = "u1"

        check("初始可用", h.available(uid))

        h.note_error(uid, ErrKind.SOFT_RATE)
        st = h.get(uid)
        check("429 后进入冷却", not h.available(uid))
        first = st.cool_until - time.time()
        check(f"冷却时长≈基数({first:.1f}s)", 8 < first <= 11, f"{first:.2f}s")
        check("软冷却未禁用账号", not st.disabled)

        # 第二次 429 应指数退避翻倍
        h.note_error(uid, ErrKind.SOFT_RATE)
        second = h.get(uid).cool_until - time.time()
        check(f"第2次 429 指数退避({second:.1f}s)", second > first * 1.8, f"{second:.2f}s")

        h.note_success(uid)
        check("成功后冷却清空", h.available(uid) and h.get(uid).softs == 0)

        # 5xx 达到阈值触发熔断
        h.note_error(uid, ErrKind.SERVER)
        check("连续 5xx 未达阈值仍可用", h.available(uid))
        h.note_error(uid, ErrKind.SERVER)
        check("达到阈值触发熔断", not h.available(uid) and h.get(uid).breaker_until > 0)

        h.note_success(uid)
        check("成功后熔断解除", h.available(uid) and h.get(uid).breaker_until == 0)

        # 会话失效 → 永久禁用，且成功也不能自动恢复
        h.note_error(uid, ErrKind.SESSION_DEAD)
        check("会话失效 → 禁用", not h.available(uid) and h.get(uid).disabled)
        h.note_success(uid)
        check("禁用不会因单次成功解除", not h.available(uid))
        h.enable(uid)
        check("人工 enable 可恢复", h.available(uid))

        # 4xx 业务错误不应惩罚
        h2 = HealthRegistry(cfg)
        h2.note_error("u2", ErrKind.CLIENT)
        check("CLIENT 不触发冷却", h2.available("u2") and h2.get("u2").cool_until == 0)
        h2.note_error("u2", ErrKind.NETWORK)
        check("NETWORK 不触发冷却", h2.available("u2"))

        # 在途限流
        cfg.max_in_flight = 1
        h3 = HealthRegistry(cfg)
        check("首次 acquire 成功", h3.acquire("u3"))
        check("超限 acquire 失败", not h3.acquire("u3"))
        h3.release("u3")
        check("release 后可再 acquire", h3.acquire("u3"))

        # 状态落盘 + 重载
        h.note_error(uid, ErrKind.SESSION_DEAD)  # 重新置为禁用再落盘
        h.persist_now()
        state_path = Path(cfg.state_file)
        check("状态文件已生成", state_path.is_file())
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        check("禁用状态写入文件", raw["accounts"][uid]["disabled"] is True)
        check("在途计数不落盘", "in_flight" not in raw["accounts"][uid])
        h4 = HealthRegistry(cfg)
        check("状态可跨进程重载", h4.get(uid).disabled is True)
        check("重载后仍不可用", not h4.available(uid))


def test_next_hour() -> None:
    print("\n[硬冷却时刻计算]")
    now = time.mktime((2026, 9, 11, 10, 0, 0, 0, 0, -1))
    t = _next_hour(4, now)
    lt = time.localtime(t)
    check("今天已过 4 点 → 顺延到明天", (lt.tm_mday, lt.tm_hour) == (12, 4), str(lt[:4]))
    now2 = time.mktime((2026, 9, 11, 2, 0, 0, 0, 0, -1))
    lt2 = time.localtime(_next_hour(4, now2))
    check("今天未到 4 点 → 当天", (lt2.tm_mday, lt2.tm_hour) == (11, 4), str(lt2[:4]))


def test_auth_parse() -> None:
    print("\n[登录态解析]")
    with tempfile.TemporaryDirectory() as td:
        # 正常形状（含 expiresAt + domain + enterpriseId）
        p1 = Path(td) / "a.info"
        p1.write_text(
            json.dumps(
                {
                    "account": {"uid": "uid-1", "nickname": "张三", "enterpriseId": "ent-9"},
                    "auth": {
                        "accessToken": "tok",
                        "refreshToken": "ref",
                        "expiresAt": 4102444800000,
                        "domain": "www.codebuddy.cn",
                    },
                }
            ),
            encoding="utf-8",
        )
        a = parse_account(p1)
        check("uid 解析", a.uid == "uid-1")
        check("enterpriseId 解析", a.enterprise_id == "ent-9")
        check("domain 解析", a.domain == "www.codebuddy.cn")
        check("未过期判定", not a.needs_refresh(0))

        # 只有 expiresIn（本机真实形状的变体）→ 必须推算，不能当成 0
        p2 = Path(td) / "b.info"
        p2.write_text(
            json.dumps(
                {
                    "account": {"uid": "uid-2"},
                    "auth": {
                        "accessToken": "tok2",
                        "expiresIn": 3600,
                        "lastRefreshTime": int(time.time() * 1000),
                    },
                }
            ),
            encoding="utf-8",
        )
        b = parse_account(p2)
        check("expiresIn 推算 expiresAt", b.expires_at_ms > time.time() * 1000)
        check("推算后未过期", not b.needs_refresh(0))

        # 完全缺有效期 → 视为未知，绝不预判过期（否则每请求都白刷一次）
        p3 = Path(td) / "c.info"
        p3.write_text(
            json.dumps({"account": {"uid": "u3"}, "auth": {"accessToken": "t3"}}),
            encoding="utf-8",
        )
        c = parse_account(p3)
        check("缺有效期 → 不预判过期", c.expires_at_ms == 0 and not c.needs_refresh(300))

        # 只有 accounts[0]、没有 account 字段
        p4 = Path(td) / "d.info"
        p4.write_text(
            json.dumps(
                {"accounts": [{"uid": "u4", "nickname": "李四"}], "auth": {"accessToken": "t4"}}
            ),
            encoding="utf-8",
        )
        d = parse_account(p4)
        check("回退到 accounts[0]", d.uid == "u4" and d.nickname == "李四")

        # 无 token → 明确报错而不是静默通过
        p5 = Path(td) / "e.info"
        p5.write_text(json.dumps({"auth": {}}), encoding="utf-8")
        try:
            parse_account(p5)
            check("无 accessToken 应报错", False, "未抛异常")
        except RuntimeError:
            check("无 accessToken 应报错", True)


def test_model_extraction() -> None:
    print("\n[模型清单抽取]")
    real = {
        "code": 0,
        "data": {
            "agents": [
                {"name": "cli", "tags": ["cli", "default"], "models": ["auto", "glm-5.3", "hy3"]},
                {"name": "other", "tags": ["x"], "models": ["hidden-model"]},
            ]
        },
    }
    ids = _extract_model_ids(real)
    check("优先取 cli agent 的模型", ids[:3] == ["auto", "glm-5.3", "hy3"], str(ids))
    check("非 cli agent 不混入", "hidden-model" not in ids)
    check("去重", len(ids) == len(set(ids)))

    check("空结构返回空", _extract_model_ids({}) == [])

    flat = {"data": {"models": [{"id": "a"}, {"id": "b"}]}}
    check("兼容 models[].id 形状", _extract_model_ids(flat) == ["a", "b"])

    # ── 2026-09-21 实测结构：上游三层数据只该取第一层 ──────────────────────
    # 症状：/v1/models 报了 31 个模型，Mac 照着逐个探测，9 个撞上游 code 11102
    # "model service info not found"（400），agent 被当场打断。
    live = {
        "code": 0,
        "data": {
            "mergeStrategy": "merge",
            "agents": [
                {"name": "cli", "tags": ["cli", "default"],
                 "models": ["auto", "hy4-preview", "hy3", "deepseek-v4.1-flash", "glm-5.3",
                            "kimi-k3-1", "minimax-m3", "deepseek-v4-pro"]},
                {"name": "general-purpose", "tags": ["cli", "general-purpose"], "models": []},
                # 子 agent 各自挂着内部轻量模型 —— 外部客户端调它会 400
                {"name": "promptHookEvaluator", "tags": ["cli", "prompt-hook-evaluator"],
                 "models": ["lite"]},
                {"name": "Explore", "tags": ["cli", "sub-agent"], "models": ["lite"]},
                {"name": "Bash", "tags": ["cli", "sub-agent"], "models": ["lite"]},
            ],
            # 账号级目录：含已下线 / 未开通条目，实测全为 11102
            "models": [{"id": "auto"}, {"id": "glm-5.0"}, {"id": "glm-4.6"},
                       {"id": "glm-4.7"}, {"id": "glm-4.6v"}, {"id": "minimax-m2.5"},
                       {"id": "kimi-k2-thinking"}, {"id": "hy4-preview-x"},
                       {"id": "hunyuan-image-v3.0"}, {"id": "default"}],
        },
    }
    live_ids = _extract_model_ids(live)
    check("主 CLI agent 的模型全保留",
          all(m in live_ids for m in ("auto", "glm-5.3", "kimi-k3-1", "deepseek-v4-pro")),
          str(live_ids))
    check("子 agent 的内部模型 lite 不混入", "lite" not in live_ids, str(live_ids))
    check("账号目录里的已下线模型不混入",
          not ({"glm-5.0", "glm-4.6", "glm-4.7", "glm-4.6v", "minimax-m2.5",
                "kimi-k2-thinking", "hy4-preview-x", "hunyuan-image-v3.0"} & set(live_ids)),
          str(live_ids))
    check("清单不再超发（主 agent 8 个 + default）", len(live_ids) == 9, str(live_ids))


def test_protocol_adapters() -> None:
    print("\n[协议适配]")
    from gtwb.responses_adapter import responses_request_to_chat
    from gtwb.responses_projection import project_responses_chat_body
    from gtwb.anthropic_adapter import anthropic_request_to_chat

    chat, _name_route, _internal = responses_request_to_chat(
        {
            "model": "glm-5.3",
            "instructions": "你是助手",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "你好"}]}],
        }
    )
    check("Responses → Chat 产出 messages", bool(chat.get("messages")))
    has_sys = any(m.get("role") == "system" for m in chat["messages"])
    check("instructions 落到 system", has_sys or bool(chat.get("instructions")))

    proj, stats = project_responses_chat_body(chat)
    check("投影返回统计", isinstance(stats, dict) and "mode" in stats)
    check("投影后仍有消息", bool(proj.get("messages")))

    a = anthropic_request_to_chat(
        {"model": "glm-5.3", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    )
    check("Anthropic → Chat 产出 messages", bool(a.get("messages")))


def test_hardening():
    """2026-09-19 硬化批次：无损脱敏、弹性上下文预算、流完整性、模型名兜底。"""
    import asyncio

    from gtwb import responses_adapter as RA
    from gtwb import responses_projection as RP
    from gtwb import desensitize as DS
    from gtwb import server as SV

    print("\n[无损脱敏]")
    codex_instructions = (
        "You are a coding agent running in the Codex CLI. "
        + "Follow the repository conventions carefully. " * 400
    )
    tools = [{
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "Run a shell command in the workspace.",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        },
    }]
    body = {"messages": [{"role": "system", "content": codex_instructions},
                         {"role": "user", "content": "hello"}],
            "tools": tools}

    lossless = DS.desensitize_body(body, roles=("system", "developer"),
                                   desensitize_harness_user=True, desensitize_tools=True,
                                   compact_harness=False, strip_tool_metadata=False,
                                   prune_runtime=False)
    sys_out = lossless["messages"][0]["content"]
    check("无损档保留系统提示词全文",
          len(sys_out) >= len(codex_instructions),
          f"{len(codex_instructions)} → {len(sys_out)}")
    check("无损档保留工具描述",
          lossless["tools"][0]["function"].get("description") ==
          tools[0]["function"]["description"])

    # 身份中和：无损档必须只动"我是谁"，不动"做什么"。
    check("无损档中和 Codex CLI 身份声明",
          "Codex CLI" not in sys_out,
          "身份句已改写为中性表述")
    check("无损档保留行为指令正文",
          sys_out.count("Follow the repository conventions carefully.") == 400,
          "400 条行为指令逐字保留")
    check("身份中和后可再次调用（参数不遮蔽函数）",
          callable(getattr(DS, "neutralize_identity_text", None)),
          "neutralize_identity_text 可调用")

    hard = DS.desensitize_body(body, roles=("system", "developer"),
                               desensitize_harness_user=True, desensitize_tools=True,
                               compact_harness=True, strip_tool_metadata=True,
                               prune_runtime=True)
    check("降级档才压缩系统提示词",
          len(hard["messages"][0]["content"]) < len(codex_instructions) * 0.05)
    check("降级档才清空工具描述",
          not hard["tools"][0]["function"].get("description"))

    print("\n[弹性上下文预算]")
    check("透明阈值已上调到 ≥400k 字符", RP.TRANSPARENT_CHAR_LIMIT >= 400_000)

    small = [{"role": "user", "content": "x" * 5_000}]
    _, meta_small = RP.project_responses_chat_body({"messages": small})
    check("阈值内走透明档", meta_small.get("mode") == "transparent", str(meta_small.get("mode")))

    big_msgs = [{"role": "system", "content": codex_instructions}]
    for i in range(300):
        big_msgs.append({"role": "assistant", "content": f"step {i} " + "y" * 2_000})
        big_msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                         "content": f"result {i} " + "z" * 2_000})
    big_msgs.append({"role": "user", "content": "继续"})
    _, meta_big = RP.project_responses_chat_body({"messages": big_msgs, "tools": []})

    orig = meta_big.get("original_message_chars") or 0
    kept = meta_big.get("projected_message_chars") or 0
    check("超阈值进入弹性档", meta_big.get("mode") == "elastic", str(meta_big.get("mode")))
    check("不再把长会话压成个位数百分比",
          kept >= 100_000 and kept >= orig * 0.15,
          f"{orig} → {kept}")
    check("压缩档保留真实系统提示词",
          any(m.get("role") == "system" and len(str(m.get("content", ""))) > 5_000
              for m in RP.project_responses_chat_body({"messages": big_msgs, "tools": []})[0]["messages"]))

    print("\n[请求字段透传]")
    chat, _, _ = RA.responses_request_to_chat({
        "model": "m", "instructions": "i",
        "input": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "prompt_cache_key": "abc123",
    })
    check("嵌套 reasoning.effort 映射到 reasoning_effort", chat.get("reasoning_effort") == "high")
    check("prompt_cache_key 透传", chat.get("prompt_cache_key") == "abc123")
    check("prompt_cache_key 在 Chat 直通白名单里", "prompt_cache_key" in SV.PASSTHROUGH_KEYS)

    print("\n[工具输出图片]")
    text_out, urls = RA._split_tool_output_images([
        {"type": "input_text", "text": "screenshot taken"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ])
    check("图片被摘出为 URL 列表", urls == ["data:image/png;base64,AAAA"])
    check("文本部分不含 base64", "AAAA" not in text_out)

    print("\n[并行工具调用配对]")
    # 回归动机：2026-09-19 Mac Codex Desktop 实测 —— 某轮返回两个 view_image，
    # 下一轮上游 400 `11148 tool calls and tool results do not match`。
    # 根因：工具输出里的图片消息被插在两条 tool 结果**之间**，把第二条 tool 与
    # assistant.tool_calls 隔开。Chat 协议要求 tool_calls 之后**连续**跟上同等
    # 数量的 tool 消息。修法：图片消息缓存到整组 tool 结果之后再统一发出。
    _IMG = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}

    def _pair_ok(msgs):
        """校验每条带 tool_calls 的 assistant 后紧跟同数同序的 tool 消息。"""
        i = 0
        while i < len(msgs):
            tcs = msgs[i].get("tool_calls")
            if msgs[i].get("role") == "assistant" and tcs:
                follow = msgs[i + 1:i + 1 + len(tcs)]
                if len(follow) != len(tcs) or any(f.get("role") != "tool" for f in follow):
                    return False
                if [f.get("tool_call_id") for f in follow] != [t["id"] for t in tcs]:
                    return False
            i += 1
        return True

    def _parallel(calls):
        items = [{"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": "go"}]}]
        for cid, name, _out in calls:
            items.append({"type": "function_call", "call_id": cid, "name": name,
                          "arguments": "{}"})
        for cid, _name, out in calls:
            items.append({"type": "function_call_output", "call_id": cid, "output": out})
        return RA._convert_input_items(items)

    _m = _parallel([("A", "view_image", [_IMG]), ("B", "view_image", [_IMG])])
    check("并行 view_image ×2：配对合法", _pair_ok(_m),
          str([(x["role"], x.get("tool_call_id")) for x in _m]))
    check("并行 view_image ×2：图片消息在所有 tool 之后",
          [x["role"] for x in _m] == ["user", "assistant", "tool", "tool", "user"],
          str([x["role"] for x in _m]))
    _m2 = _parallel([("A", "exec_command", "ok"), ("B", "exec_command", "ok")])
    check("并行纯文本 ×2：配对合法且不产生多余消息",
          _pair_ok(_m2) and [x["role"] for x in _m2] == ["user", "assistant", "tool", "tool"],
          str([x["role"] for x in _m2]))
    _m3 = _parallel([("A", "view_image", [_IMG]), ("B", "view_image", "no img")])
    check("并行混合（仅一个带图）：配对合法",
          _pair_ok(_m3)
          and [x["role"] for x in _m3] == ["user", "assistant", "tool", "tool", "user"],
          str([x["role"] for x in _m3]))

    print("\n[流完整性]")
    conv = RA.ResponsesStreamConverter(model="m")
    conv.feed_line('data: {"choices":[{"delta":{"content":"hi"}}]}')
    check("未见 finish_reason 时不算正常收尾", conv.saw_terminal_evidence() is False)
    conv.note_done()
    check("见到 [DONE] 后算正常收尾", conv.saw_terminal_evidence() is True)
    failed = conv.finish(error={"code": "upstream_stream_error", "message": "boom"})
    check("可发出 response.failed", "response.failed" in failed)
    inc = conv.finish(incomplete={"reason": "upstream_truncated"})
    check("可发出 response.incomplete", "response.incomplete" in inc)

    print("\n[output_index 一致性]")
    conv2 = RA.ResponsesStreamConverter(model="m")
    conv2.feed_line('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"exec_command","arguments":"{}"}}]}}]}')
    conv2.feed_line('data: {"choices":[{"delta":{"content":"done"}}]}')
    obj = conv2.get_nonstream_response()
    indexes = [it.get("id") for it in obj["output"]]
    check("先工具后文本时 output 顺序不乱",
          [it["type"] for it in obj["output"]] == ["function_call", "message"],
          str([it["type"] for it in obj["output"]]))
    check("output 非空", bool(indexes))

    print("\n[内部工具拦截]")
    conv3 = RA.ResponsesStreamConverter(
        model="m", internal_tools={"web_search"})
    conv3.feed_line('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"web_search","arguments":"{\\"query\\":\\"codex\\"}"}}]}}]}')
    conv3.feed_line('data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"c2","function":{"name":"exec_command","arguments":"{\\"cmd\\":\\"ls\\"}"}}]}}]}')
    conv3.note_done()
    icalls = conv3.internal_calls()
    ccalls = conv3.client_calls()
    check("internal_calls 只收内部工具",
          [t["name"] for t in icalls] == ["web_search"], str(icalls))
    check("client_calls 与 internal_calls 互补",
          [t["name"] for t in ccalls] == ["exec_command"], str(ccalls))
    check("拦截的调用带完整参数",
          icalls and icalls[0]["args"] == '{"query":"codex"}', str(icalls))

    print("\n[搜索无进展短路]")
    # 动机：2026-09-20 实测模型会把上一轮的 query 原样重发，两次会话各烧到
    # 82.5s / 72.1s。凡是「本轮全部命中缓存」就判无进展、直接进收尾轮。
    _cache = {"codex cli version": [1, 2]}
    check("全部命中缓存 → 判为无进展",
          SV._all_queries_cached(
              [{"name": "web_search", "args": '{"query":"Codex CLI Version"}'}], _cache) is True)
    check("含一个新 query → 不算无进展（不打断补充检索）",
          SV._all_queries_cached(
              [{"name": "web_search", "args": '{"query":"Codex CLI Version"}'},
               {"name": "web_search", "args": '{"query":"rust release notes"}'}], _cache) is False)
    check("空调用列表 → 不算无进展", SV._all_queries_cached([], _cache) is False)
    check("参数不是合法 JSON → 不算无进展（宁可多搜一轮也不误判）",
          SV._all_queries_cached([{"name": "web_search", "args": "{bad"}], _cache) is False)
    check("query 为空 → 不算无进展",
          SV._all_queries_cached([{"name": "web_search", "args": '{"query":"  "}'}], _cache) is False)

    print("\n[错误码归正]")
    check("NETWORK → 502", SV._status_for_kind(SV.ErrKind.NETWORK) == 502)
    check("SERVER → 502", SV._status_for_kind(SV.ErrKind.SERVER) == 502)
    check("SOFT_RATE → 429", SV._status_for_kind(SV.ErrKind.SOFT_RATE) == 429)
    check("AUTH → 401", SV._status_for_kind(SV.ErrKind.AUTH) == 401)

    print("\n[记账自洽]")
    # 回归动机：2026-09-19 新增 escalated 字段时漏了 __init__ 赋值，
    # 结果是每个请求在收尾记账时 500，而日志里只看到 ▶ 没有 ◀。这里把
    # 「fresh RequestStat 走完 done()」固化下来，任何字段漏定义都会当场失败。
    from gtwb import obs as OBS
    st = OBS.RequestStat("m", "RESPONSES", "rid0")
    st.uid8 = "abcdef12"
    st.status = 200
    st.elapsed = 1.0
    st.tokens = 12
    st.usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0}}
    st.tool_calls = ["exec_command"]
    st.finish = "tool_calls"
    st.first_byte()
    st.done()
    check("RequestStat.done() 全字段自洽（不再 AttributeError）", True)

    print("\n[模型名兜底]")
    cfg = Config(api_key="", model_fallback="deepseek-v4.1-flash",
                 model_aliases={"gpt-5.6-luna": "glm-5.3"})
    gw = SV.Gateway.__new__(SV.Gateway)
    gw.cfg = cfg

    async def _ids():
        return ["deepseek-v4.1-flash", "glm-5.3"]

    gw.model_ids = _ids

    async def _probe():
        alias = await gw.resolve_model("gpt-5.6-luna")
        unknown = await gw.resolve_model("gpt-6-astra")
        known = await gw.resolve_model("deepseek-v4.1-flash")
        empty = await gw.resolve_model("")
        return alias, unknown, known, empty

    alias, unknown, known, empty = asyncio.run(_probe())
    check("别名命中", alias[0] == "glm-5.3", str(alias))
    check("未知模型落到兜底", unknown[0] == "deepseek-v4.1-flash", str(unknown))
    check("已知模型原样放行", known == ("deepseek-v4.1-flash", ""), str(known))
    check("空模型名不处理", empty == ("", ""), str(empty))

    # 关掉守卫 → 不查模型表，零开销直通
    cfg2 = Config(api_key="", model_guard=False)
    gw2 = SV.Gateway.__new__(SV.Gateway)
    gw2.cfg = cfg2

    async def _boom():
        raise AssertionError("不应查模型表")

    gw2.model_ids = _boom
    check("守卫关闭时不查模型表", asyncio.run(gw2.resolve_model("whatever")) == ("whatever", ""))

    # ── 守卫默认开：清单外 / 已下线的名字不发给上游，直接换掉 ──────────────
    # 起因：Mac 的 Codex 缓存了旧清单，照着选 glm-4.6 → 上游 code 11102 → 400 断链。
    cfg3 = Config(api_key="")
    gw3 = SV.Gateway.__new__(SV.Gateway)
    gw3.cfg = cfg3

    async def _ids3():
        return ["auto", "glm-5.3", "deepseek-v4.1-flash", "default"]

    gw3.model_ids = _ids3

    async def _probe3():
        return (await gw3.resolve_model("glm-4.6"),      # 上游已下线
                await gw3.resolve_model("lite"),          # 子 agent 内部模型
                await gw3.resolve_model("glm-5.3"),
                await gw3.resolve_model("default"),
                await gw3.resolve_model("auto"))

    ghost, internal, good, dflt, auto = asyncio.run(_probe3())
    check("守卫拦下已下线模型 → auto", ghost[0] == "auto" and "unavailable" in ghost[1], str(ghost))
    check("守卫拦下内部模型 lite", internal[0] == "auto", str(internal))
    check("清单内模型原样放行", good == ("glm-5.3", ""), str(good))
    check("default 保留在清单里不被换", dflt == ("default", ""), str(dflt))
    check("auto 永远放行", auto == ("auto", ""), str(auto))

    # 配了兜底时优先落兜底，而不是 auto
    cfg4 = Config(api_key="", model_fallback="deepseek-v4.1-flash")
    gw4 = SV.Gateway.__new__(SV.Gateway)
    gw4.cfg = cfg4
    gw4.model_ids = _ids3
    check("配了兜底则落兜底",
          asyncio.run(gw4.resolve_model("glm-4.6"))[0] == "deepseek-v4.1-flash")

    print("\n[诊断抓包]")
    import tempfile

    with tempfile.TemporaryDirectory() as td:

        class _Cfg:
            capture_dir = td

        SV._capture(_Cfg(), "responses", {"model": "m", "input": []})
        got = os.listdir(td)
        check("抓包确实写出文件（不再被静默吞掉）", len(got) == 1, str(got))
        if got:
            import json as _json
            with open(os.path.join(td, got[0]), encoding="utf-8") as f:
                written = _json.load(f)
            check("抓包内容与请求体一致", written.get("model") == "m")
        check("未配置 capture_dir 时不写盘", SV._capture(Config(api_key=""), "responses", {}) is None)


def test_client_identity() -> None:
    """客户端身份头：后端据此归因用量明细的「客户端」列。

    官方客户端（桌面端 + 随包 CLI）会发 X-IDE-Type / X-IDE-Name / X-IDE-Version，
    UA 形如 "<product>/<ver> <platform>/<ver> CLI/<cliVer>"。漏发这组头会让
    用量明细的「客户端」列空着 —— 既不便核对消耗，也更像来源不明的流量。
    """
    from gtwb import config as CF
    from gtwb.auth import Account
    from gtwb.upstream import chat_headers

    acct = Account(access_token="t", refresh_token="r", uid="1", enterprise_id="", domain="")

    cfg = Config(api_key="", client_version="9.9.9", cli_version="1.2.3")
    h = chat_headers(cfg, acct)
    check("上报 X-IDE-Type", h.get("X-IDE-Type") == "WorkBuddy")
    check("上报 X-IDE-Name", h.get("X-IDE-Name") == "WorkBuddy")
    check("上报 X-IDE-Version", h.get("X-IDE-Version") == "9.9.9")
    check("X-Product 仍为 SaaS", h.get("X-Product") == "SaaS")
    check(
        "UA 为官方格式",
        h.get("User-Agent") == "WorkBuddy/9.9.9 WorkBuddy/9.9.9 CLI/1.2.3",
    )
    check("UA 不再自称 CodeBuddy", "CodeBuddy" not in h.get("User-Agent", ""))

    # 关掉开关则退回旧身份，且不再发明文身份头（便于 A/B 排查）。
    cfg_off = Config(api_key="", client_identity=False)
    h_off = chat_headers(cfg_off, acct)
    check("关闭后不发 X-IDE-Name", "X-IDE-Name" not in h_off)
    check("关闭后 UA 回退为旧值", h_off.get("User-Agent") == CF.LEGACY_USER_AGENT)

    # 显式 UA 优先于一切。
    cfg_ua = Config(api_key="", user_agent="MyUA/1")
    check("显式 UA 优先", chat_headers(cfg_ua, acct).get("User-Agent") == "MyUA/1")

    # 探测不到安装版本时必须兜底成非空，不能拼出 "WorkBuddy//"。
    cfg_probe = Config(api_key="")
    check(
        "版本兜底非空",
        bool(cfg_probe.resolved_client_version()) and "//" not in cfg_probe.official_user_agent(),
    )


def test_client_attribution() -> None:
    """客户端来源识别与诊断快照。

    多机共享时，"哪条请求来自家里 Mac、用的什么客户端" 是排障的第一现场。
    没有它，日志里只能靠时间戳猜，出问题基本靠运气。
    """
    import contextlib
    import io
    import json
    import tempfile

    from gtwb import obs
    from gtwb.diag import _aggregate, _is_bad
    from gtwb.server import _client_kind, _client_zone, _tag_client

    class _Req:
        """最小 Request 替身：_tag_client 只用 client.host 与 headers.get。"""

        def __init__(self, host: str, ua: str) -> None:
            self.client = type("C", (), {"host": host})()
            self.headers = {"user-agent": ua}

    # ── UA → 客户端名 ──
    check("识别 Codex CLI", _client_kind("codex_cli_rs/0.44.0 (Mac OS 15; arm64)") == "Codex CLI")
    check("识别 Claude Code", _client_kind("claude-cli/2.0.1 (external, cli)") == "Claude Code")
    check("识别 WorkBuddy", _client_kind("WorkBuddy/5.5.6") == "WorkBuddy")
    check("识别 httpx", _client_kind("python-httpx/0.27") == "python-httpx")
    check("空 UA 不崩且有标记", _client_kind("") == "未知UA")
    check("陌名 UA 截断保留", len(_client_kind("x" * 200)) <= 25)

    # ── IP → 区域 ──
    check("本机区域", _client_zone("127.0.0.1") == "本机")
    check("ZeroTier 区域", _client_zone("192.168.191.10") == "ZeroTier")
    check("局域网区域", _client_zone("192.168.1.5") == "局域网")
    check("10 段算局域网", _client_zone("10.0.0.7") == "局域网")
    check("外部区域", _client_zone("8.8.8.8") == "外部")
    check("空 IP 不崩", _client_zone("") == "未知")

    # ── 打标会填进 stat，并给出可打印摘要 ──
    st = obs.RequestStat("m", "CHAT", "r1")
    desc = _tag_client(st, _Req("192.168.191.10", "codex_cli_rs/0.44.0"))
    check("填充 client_ip", st.client_ip == "192.168.191.10")
    check("填充 client_ua", "codex_cli_rs" in st.client_ua)
    check("填充 client_kind", st.client_kind == "Codex CLI")
    check("摘要含区域与客户端", "ZeroTier" in desc and "Codex CLI" in desc)

    # ── 记账与日志都要带上来源（否则事后无法归因）──
    tmpd = tempfile.mkdtemp()
    obs.setup(os.path.join(tmpd, "t.log"), verbose=False)
    try:
        st2 = obs.RequestStat("m", "RESPONSES", "r9")
        st2.client_ip, st2.client_ua, st2.client_kind = "192.168.191.10", "codex_cli_rs/0.44.0", "Codex CLI"
        st2.status = 200
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            st2.done()
        check("日志行含 client=", "client=192.168.191.10 Codex CLI" in buf.getvalue())
        check("日志行含 rid", "[r9]" in buf.getvalue())

        stat_path = os.path.join(tmpd, "usage-stats.jsonl")
        recs = [json.loads(x) for x in open(stat_path, encoding="utf-8")]
        check("记账含 client_ip", recs[0]["client_ip"] == "192.168.191.10")
        check("记账含 client_kind", recs[0]["client_kind"] == "Codex CLI")
        check("记账含 rid（可与日志对上）", recs[0]["rid"] == "r9")
    finally:
        obs.setup(None)

    # ── 诊断聚合 ──
    sample = [
        {"client_ip": "192.168.191.10", "client_kind": "Codex CLI", "status": 200, "ts": "2026-09-19 20:00:00"},
        {"client_ip": "192.168.191.10", "client_kind": "Codex CLI", "status": 400, "ts": "2026-09-19 20:01:00"},
        {"client_ip": "192.168.191.10", "client_kind": "Codex CLI", "status": 200, "ts": "2026-09-19 20:02:00",
         "escalated": True},
        {"client_ip": "127.0.0.1", "client_kind": "curl", "status": 200, "ts": "2026-09-19 20:03:00"},
    ]
    g = _aggregate(sample)
    check("聚合按来源+客户端分组", len(g) == 2)
    check("请求量多的排前面", g[0]["ip"] == "192.168.191.10")
    check("统计请求数", g[0]["n"] == 3)
    check("统计成功数", g[0]["ok"] == 2)
    check("统计失败数", g[0]["bad"] == 1)
    check("统计降级数", g[0]["esc"] == 1)
    check("最近时间取最后一条", g[0]["last"] == "2026-09-19 20:02:00")

    check("异常判定：status>=400", _is_bad({"status": 400}) is True)
    check("异常判定：200 不算异常", _is_bad({"status": 200}) is False)
    check("异常判定：降级算异常", _is_bad({"status": 200, "escalated": True}) is True)
    check("异常判定：审核命中算异常", _is_bad({"status": 200, "filtered": True}) is True)


def test_usage_accounting() -> None:
    """用量记账口径：客户端拿到的是跨轮**累加**值，账本必须一致。

    内部工具循环（web_search）会开多次上游请求，每轮各报一次 usage。
    客户端读到累加值，而 obs 侧若每轮覆盖、只留最后一轮，看板就会系统性少记
    （2026-09-21 实测同一请求差 4~5 倍，用户据此认为"两边数据不一致"）。
    """
    print("\n[用量记账口径]")
    from gtwb import responses_adapter as RA

    conv = RA.ResponsesStreamConverter(model="m")

    conv.feed_line(
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":1000,'
        '"completion_tokens":20,"total_tokens":1020,'
        '"prompt_tokens_details":{"cached_tokens":800}}}'
    )
    u1 = conv.accumulated_usage()
    check("第 1 轮：累加值 = 该轮本身",
          bool(u1) and u1.get("total_tokens") == 1020, str(u1))
    check("第 1 轮不被重复计入", u1.get("prompt_tokens") == 1000, str(u1))

    conv.begin_next_round()
    conv.feed_line(
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":1500,'
        '"completion_tokens":30,"total_tokens":1530,'
        '"prompt_tokens_details":{"cached_tokens":1200}}}'
    )
    u2 = conv.accumulated_usage()
    check("第 2 轮：prompt 累加而非覆盖",
          u2.get("prompt_tokens") == 2500, str(u2))
    check("第 2 轮：total 累加", u2.get("total_tokens") == 2550, str(u2))
    check("第 2 轮：cached 明细一并累加",
          (u2.get("prompt_tokens_details") or {}).get("cached_tokens") == 2000, str(u2))
    check("累加值 ≠ 最后一轮（旧口径确实会少记）",
          u2.get("total_tokens") != 1530, str(u2))
    check("accumulated_usage 可被 stat 直接引用",
          conv.accumulated_usage() is u2)


def test_upstream_config() -> None:
    """上游域名 / Origin 可按部署环境切换（国内账号 vs 国际账号、第二上游实例）。"""
    from gtwb.config import load_config

    default = Config()
    check("默认上游域名", default.backend == "https://copilot.tencent.com", default.backend)
    check("默认 Origin", default.web_origin == "https://www.codebuddy.cn", default.web_origin)

    cfg = load_config(
        config_path=None,
        env={
            "GTWB_BACKEND": "https://intl.backend.example",
            "GTWB_WEB_ORIGIN": "https://intl.origin.example",
        },
    )
    check("GTWB_BACKEND 覆盖上游域名", cfg.backend == "https://intl.backend.example", cfg.backend)
    check("GTWB_WEB_ORIGIN 覆盖 Origin/Referer",
          cfg.web_origin == "https://intl.origin.example", cfg.web_origin)


def main() -> int:
    test_classify()
    test_next_hour()
    test_cooldown_machine()
    test_auth_parse()
    test_model_extraction()
    test_protocol_adapters()
    test_hardening()
    test_client_identity()
    test_client_attribution()
    test_usage_accounting()
    test_upstream_config()

    print("\n" + "═" * 56)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  失败：{f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
