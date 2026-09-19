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


def test_protocol_adapters() -> None:
    print("\n[协议适配]")
    from gtwb.responses_adapter import responses_request_to_chat
    from gtwb.responses_projection import project_responses_chat_body
    from gtwb.anthropic_adapter import anthropic_request_to_chat

    chat, _bare_to_ns = responses_request_to_chat(
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
    chat, _ = RA.responses_request_to_chat({
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

    cfg2 = Config(api_key="")   # 未配置别名/兜底 → 零开销直通
    gw2 = SV.Gateway.__new__(SV.Gateway)
    gw2.cfg = cfg2

    async def _boom():
        raise AssertionError("不应查模型表")

    gw2.model_ids = _boom
    check("未启用时不查模型表", asyncio.run(gw2.resolve_model("whatever")) == ("whatever", ""))

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


def main() -> int:
    test_classify()
    test_next_hour()
    test_cooldown_machine()
    test_auth_parse()
    test_model_extraction()
    test_protocol_adapters()
    test_hardening()
    test_client_identity()

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
