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

    chat = responses_request_to_chat(
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


def main() -> int:
    test_classify()
    test_next_hour()
    test_cooldown_machine()
    test_auth_parse()
    test_model_extraction()
    test_protocol_adapters()

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
