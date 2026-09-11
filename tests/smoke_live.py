"""对运行中的网关做端到端冒烟测试（会真实调用后端，消耗少量额度）。

用法：
    先启动服务，再执行
    python tests/smoke_live.py [--base http://127.0.0.1:8787] [--key YOUR_KEY]

与 test_gateway.py 的分工：那个测内部逻辑（离线、不烧额度），
这个测真实链路（在线、烧少量额度）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

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


def call(base: str, path: str, payload: dict | None = None, key: str = "", timeout: int = 180):
    url = base.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read().decode("utf-8", "replace")
        try:
            return r.status, json.loads(body)
        except Exception:
            return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--key", default="")
    ap.add_argument("--model", default="deepseek-v4.1-flash")
    args = ap.parse_args()
    base, key, model = args.base, args.key, args.model

    print(f"\n目标：{base}  鉴权：{'开' if key else '关'}  模型：{model}\n")

    # ── 健康检查 ─────────────────────────────────────────────────────────
    print("[健康检查]")
    st, body = call(base, "/health")
    check("GET /health → 200", st == 200, f"HTTP {st}")
    if isinstance(body, dict):
        check("返回 service 标识", body.get("service") == "gt-wb-gateway")
        acct = body.get("account") or {}
        check("已识别登录账号", bool(acct.get("loaded")), str(acct))
    st, _ = call(base, "/healthz")
    check("GET /healthz → 200", st == 200, f"HTTP {st}")

    # ── 鉴权 ─────────────────────────────────────────────────────────────
    if key:
        print("\n[鉴权]")
        st, _ = call(base, "/v1/models", key="")
        check("无 key 访问受保护端点 → 401", st == 401, f"HTTP {st}")
        st, _ = call(base, "/v1/models", key=key)
        check("带 key 访问受保护端点 → 200", st == 200, f"HTTP {st}")

    # ── 模型清单 ─────────────────────────────────────────────────────────
    print("\n[模型清单]")
    st, body = call(base, "/v1/models", key=key)
    ids = [m["id"] for m in body.get("data", [])] if isinstance(body, dict) else []
    check("GET /v1/models → 200", st == 200, f"HTTP {st}")
    check(f"模型数 > 0（实际 {len(ids)}）", len(ids) > 0)
    if ids:
        print(f"    前 8 个：{', '.join(ids[:8])}")

    # ── Chat Completions ─────────────────────────────────────────────────
    print("\n[Chat Completions]")
    st, body = call(base, "/v1/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "只回答两个字：收到"}],
    }, key=key)
    ok = st == 200 and isinstance(body, dict)
    check("非流式 → 200", ok, f"HTTP {st} {str(body)[:120]}")
    if ok:
        msg = body["choices"][0]["message"]
        check("返回文本内容", bool(msg.get("content")), str(msg)[:120])
        check("usage 有 token 数", (body.get("usage") or {}).get("total_tokens", 0) > 0)

    st, body = call(base, "/v1/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "北京天气如何？"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询城市天气",
                "parameters": {"type": "object",
                               "properties": {"city": {"type": "string"}},
                               "required": ["city"]},
            },
        }],
    }, key=key)
    ok = st == 200 and isinstance(body, dict)
    check("工具调用 → 200", ok, f"HTTP {st}")
    if ok:
        tcs = body["choices"][0]["message"].get("tool_calls") or []
        check("触发 tool_calls", len(tcs) > 0, str(body["choices"][0])[:160])
        if tcs:
            fn = tcs[0].get("function", {})
            check("工具名正确", fn.get("name") == "get_weather", str(fn))
            try:
                args_obj = json.loads(fn.get("arguments") or "{}")
                check("工具参数是合法 JSON", "city" in args_obj, str(args_obj))
            except Exception as e:
                check("工具参数是合法 JSON", False, str(e))

    # ── Responses（Codex）────────────────────────────────────────────────
    print("\n[Responses / Codex]")
    st, body = call(base, "/v1/responses", {
        "model": model,
        "input": [{"role": "user",
                   "content": [{"type": "input_text", "text": "只回答两个字：收到"}]}],
        "stream": False,
    }, key=key)
    ok = st == 200 and isinstance(body, dict)
    check("非流式 → 200", ok, f"HTTP {st} {str(body)[:120]}")
    if ok:
        check("object=response", body.get("object") == "response", str(body.get("object")))
        check("status=completed", body.get("status") == "completed", str(body.get("status")))
        texts = [c.get("text") for it in (body.get("output") or [])
                 for c in (it.get("content") or []) if c.get("type") == "output_text"]
        check("有 output_text 内容", any(texts), str(texts)[:120])

    # ── Anthropic Messages ───────────────────────────────────────────────
    print("\n[Anthropic Messages]")
    st, body = call(base, "/v1/messages", {
        "model": model, "max_tokens": 128,
        "messages": [{"role": "user", "content": "只回答两个字：收到"}],
    }, key=key)
    ok = st == 200 and isinstance(body, dict)
    check("非流式 → 200", ok, f"HTTP {st} {str(body)[:120]}")
    if ok:
        check("type=message", body.get("type") == "message", str(body.get("type")))
        check("stop_reason 有值", bool(body.get("stop_reason")))
        check("content 是数组", isinstance(body.get("content"), list))

    # ── 状态 ─────────────────────────────────────────────────────────────
    print("\n[状态]")
    st, body = call(base, "/status", key=key)
    check("GET /status → 200", st == 200, f"HTTP {st}")
    if isinstance(body, dict) and body.get("accounts"):
        print(f"    账号状态：{body['accounts'][0].get('state')}"
              f"｜轮转：{body.get('rotation_enabled')}")

    print("\n" + "═" * 56)
    print(f"冒烟测试：通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  失败：{f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
