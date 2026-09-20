"""HTTP 服务层：把四种协议统一到同一条执行链上。

执行链（每个请求都走这条）：
    协议转换 → Codex 投影压缩 → 脱敏 → 取账号 → 健康检查/在途租约
    → 打后端 → 错误分类 → 状态机记账 → 协议回转 → 单行日志

统一收益（相对三家参考实现）：
  - 三种协议共享同一套冷却/熔断/日志，不再各写一遍
  - 内容审核重试扩展到 Chat 与 Anthropic 两条路径（原实现只有 Responses 有）
  - 模型清单改为动态拉取 + 1h 缓存（原实现是硬编码，已过时）
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import obs
from . import upstream
from .auth import AccountManager
from .config import Config
from .resilience import HealthRegistry
from .upstream import ErrKind

from .desensitize import desensitize_body
from .responses_adapter import ResponsesStreamConverter, responses_request_to_chat
from .responses_projection import project_responses_chat_body
from .anthropic_adapter import AnthropicStreamConverter, anthropic_request_to_chat

# 透传给后端的合法字段（其余客户端字段一律丢弃，避免触发后端校验）
PASSTHROUGH_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature", "max_tokens",
    "max_completion_tokens", "top_p", "stream", "stream_options", "stop",
    "presence_penalty", "frequency_penalty", "n", "response_format", "seed",
    "user", "reasoning_effort", "verbosity", "reasoning_summary",
    "parallel_tool_calls", "prompt_cache_key",
}

_MODEL_CACHE: dict[str, Any] = {"ids": [], "at": 0.0}
MODEL_TTL_S = 3600

# 抓包失败只提示一次，避免把日志刷爆
_CAPTURE_WARNED = False


class Gateway:
    """把配置、账号、健康状态、模型缓存打包，便于测试时替换。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.accounts = AccountManager(cfg)
        self.health = HealthRegistry(cfg)
        obs.setup(cfg.log_path)

    # ── 鉴权 ──────────────────────────────────────────────────────────────
    def check_auth(self, authorization: str | None, x_api_key: str | None) -> None:
        key = self.cfg.api_key
        if not key:
            return
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        token = token or (x_api_key or "")
        if token != key:
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": "invalid api key", "type": "auth_error"}},
            )

    # ── 模型清单 ──────────────────────────────────────────────────────────
    async def model_ids(self) -> list[str]:
        now = time.time()
        if _MODEL_CACHE["ids"] and now - _MODEL_CACHE["at"] < MODEL_TTL_S:
            return _MODEL_CACHE["ids"]
        try:
            acct = await self.accounts.current()
            ids = await upstream.fetch_models(self.cfg, acct)
        except Exception:
            ids = []
        if ids:
            _MODEL_CACHE["ids"] = ids
            _MODEL_CACHE["at"] = now
        return ids or list(_MODEL_CACHE["ids"])

    # ── 账号 + 健康度 ─────────────────────────────────────────────────────
    async def pick(self) -> tuple[Any, str]:
        """返回 (账号, 拒绝原因)。拒绝原因非空表示当前不可服务。"""
        acct = await self.accounts.current()
        uid = acct.uid or acct.source
        if self.health.available(uid):
            return acct, ""
        reason = self.health.get(uid).reason()
        # 未开启轮转时不换号，直接把原因透给客户端（便于定位）
        if await self.accounts.rotate():
            acct = await self.accounts.current()
            uid = acct.uid or acct.source
            if self.health.available(uid):
                return acct, ""
            reason = self.health.get(uid).reason()
        return acct, reason

    # ── 模型名解析 ────────────────────────────────────────────────────────
    async def resolve_model(self, requested: str) -> tuple[str, str]:
        """把客户端请求的模型名解析成上游真实可用的名字。

        为什么需要：客户端（cc-switch、Codex 桌面端）常常把模型名写成 Codex 原生名
        （gpt-5.6-luna / gpt-5.6-terra / gpt-6-astra 等），这些名字在上游不存在，
        直接透传会拿到 HTTP 400 —— 整条 agent 链路当场失败，客户端还以为是自己坏了。

        返回 (实际使用的模型名, 替换说明)。未配置别名/兜底时零开销直接放行。
        """
        name = (requested or "").strip()
        if not name:
            return requested, ""

        aliases = getattr(self.cfg, "model_aliases", None) or {}
        if name in aliases:
            return aliases[name], f"alias {name}→{aliases[name]}"

        fallback = (getattr(self.cfg, "model_fallback", "") or "").strip()
        if not aliases and not fallback:
            return requested, ""      # 未启用该能力：不查模型表，零额外开销

        ids = await self.model_ids()
        if not ids or name == "auto" or name in ids:
            return requested, ""
        if fallback:
            return fallback, f"unavailable {name}→{fallback}"
        return requested, ""


def _is_loopback(request: Request) -> bool:
    """请求是否来自本机回环。用于收敛健康端点的信息暴露面。"""
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


# 客户端 UA → 人看得懂的名字。多机共享时，日志里能直接看出
# "这条是家里 Mac 的 Codex"，而不是只看到一个版本号字符串。
_CLIENT_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("codex", "Codex CLI"),
    ("claude", "Claude Code"),
    ("claude-code", "Claude Code"),
    ("workbuddy", "WorkBuddy"),
    ("codebuddy", "CodeBuddy CLI"),
    ("anthropic", "Anthropic SDK"),
    ("openai", "OpenAI SDK"),
    ("httpx", "python-httpx"),
    ("requests", "python-requests"),
    ("axios", "axios"),
    ("node", "node"),
    ("curl", "curl"),
)


def _client_kind(ua: str) -> str:
    u = (ua or "").lower()
    if not u:
        return "未知UA"
    for key, name in _CLIENT_SIGNATURES:
        if key in u:
            return name
    return obs.truncate(ua, 24)


def _client_zone(ip: str) -> str:
    """把来源 IP 归类，便于一眼看出是哪台机器。"""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return "本机"
    if ip.startswith("192.168.191."):
        return "ZeroTier"
    if ip.startswith(("192.168.", "10.", "172.")):
        return "局域网"
    if not ip:
        return "未知"
    return "外部"


def _tag_client(stat: obs.RequestStat, request: Request) -> str:
    """给请求打上客户端来源，返回可打印摘要（供 ▶ 行）。"""
    ip = (request.client.host if request.client else "") or ""
    ua = request.headers.get("user-agent", "") or ""
    stat.client_ip, stat.client_ua = ip, ua
    stat.client_kind = _client_kind(ua)
    return f"{ip or '-'} {_client_zone(ip)} {stat.client_kind}"


def build_app(gw: Gateway) -> FastAPI:
    cfg = gw.cfg
    app = FastAPI(title="gt-wb-gateway", version="1.0")

    # ══════════════════════════════════════════════════════════════════════
    # 健康与观测
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/health")
    @app.get("/healthz")
    async def health(request: Request) -> dict[str, Any]:
        # 先确保账号已加载，否则健康检查会误报「未识别账号」
        account_error = ""
        try:
            await gw.accounts.current()
        except Exception as e:
            account_error = str(e)
        info: dict[str, Any] = {
            "status": "ok" if not account_error else "degraded",
            "service": "gt-wb-gateway",
        }
        # 详细诊断（路径、账号昵称、token 有效期）只对本机回环开放。
        # 一旦监听 0.0.0.0 供其它设备使用，健康端点必须保持「无信息量」，
        # 否则同网段任意设备不带 key 就能读到账号身份与本地路径。
        if _is_loopback(request):
            info.update(
                {
                    "backend": cfg.backend,
                    "desensitize": cfg.desensitize,
                    "compact": cfg.compact_harness,
                    "auth_candidates": gw.accounts.discover_summary(),
                    "account": gw.accounts.summary(),
                }
            )
            if account_error:
                info["account_error"] = account_error
        return info

    @app.get("/status")
    async def status(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    ) -> dict[str, Any]:
        gw.check_auth(authorization, x_api_key)
        accts = await gw.accounts.all_accounts()
        return {
            "accounts": [
                {
                    "uid": a.uid,
                    "nickname": a.nickname,
                    "source": a.source,
                    "expires_at": a.expires_at_ms,
                    "state": gw.health.get(a.uid or a.source).reason(),
                }
                for a in accts
            ],
            "health": gw.health.snapshot(),
            "rotation_enabled": cfg.allow_account_rotation,
        }

    @app.get("/v1/models")
    async def list_models(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    ):
        gw.check_auth(authorization, x_api_key)
        ids = await gw.model_ids()
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": 1700000000, "owned_by": "workbuddy"}
                for m in ids
            ],
        }

    # ══════════════════════════════════════════════════════════════════════
    # 请求体准备
    # ══════════════════════════════════════════════════════════════════════

    def _finalize(body: dict[str, Any]) -> dict[str, Any]:
        """后端只支持流式：统一强制 stream=True，并补齐 usage。"""
        body.setdefault("model", "auto")
        body["stream"] = True
        body.setdefault("stream_options", {"include_usage": True})
        # 后端不认 developer 角色，会触发风控 11128；统一降级为 system
        if isinstance(body.get("messages"), list):
            body["messages"] = [
                {**m, "role": "system"}
                if isinstance(m, dict) and m.get("role") == "developer"
                else m
                for m in body["messages"]
            ]
        return body

    def _desensitize(body: dict[str, Any], force_compact: bool = False) -> dict[str, Any]:
        """脱敏两档：

        - 首轮（默认）：**无损**——只插零宽空格，完整保留真实提示词与工具描述。
          模型必须知道工具怎么用，agent 才可能干成活。
        - 降级（force_compact，仅命中审核后重试）：把 harness 提示词摘要化、
          去掉工具描述，用能力换通过率。
        """
        if not cfg.desensitize:
            return body
        hard = bool(force_compact)
        return desensitize_body(
            body,
            roles=("system", "developer"),
            desensitize_harness_user=True,
            desensitize_tools=True,
            compact_harness=hard and cfg.compact_harness,
            strip_tool_metadata=hard and cfg.strip_tool_metadata,
            prune_runtime=hard,
        )

    # ══════════════════════════════════════════════════════════════════════
    # 上游调用（含分类记账与 401 强刷）
    # ══════════════════════════════════════════════════════════════════════

    async def _note(uid: str, kind: ErrKind, stat: obs.RequestStat) -> None:
        gw.health.note_error(uid, kind)
        stat.status = stat.status or 0

    async def _open(acct: Any, body: dict[str, Any], rid: str):
        """打开上游流；401 且非会话死亡时强制刷新一次再试。

        失败自动重试（学习 new-api）：传输层错误 / 429 / 5xx 在首字节前可安全重试，
        默认重试 2 次、退避 1.5s×attempt（可配 upstream_retry / retry_backoff_s）。
        """
        uid = acct.uid or acct.source
        max_retry = int(getattr(cfg, "upstream_retry", 2) or 0)
        backoff = float(getattr(cfg, "retry_backoff_s", 1.5) or 1.5)
        attempt = 0
        while True:
            try:
                resp = await _open_raw(acct, body)
            except Exception as e:
                attempt += 1
                if attempt <= max_retry:
                    obs.log(f"⚠ 网络错误 {type(e).__name__}: {obs.truncate(str(e), 120)}"
                            f" → 重试 {attempt}/{max_retry}", rid)
                    await asyncio.sleep(backoff * attempt)
                    continue
                obs.log(f"✗ 网络错误重试耗尽：{obs.truncate(str(e), 160)}", rid)
                return acct, None, str(e).encode(), ErrKind.NETWORK

            if resp.status_code == 401:
                raw = await resp.aread()
                await resp.aclose()
                if upstream.classify(401, raw.decode("utf-8", "replace")) is ErrKind.AUTH:
                    obs.log("401 → 强制刷新登录态后重试", rid)
                    try:
                        acct = await gw.accounts.force_refresh()
                        resp = await _open_raw(acct, body)
                    except Exception as e:
                        obs.log(f"强制刷新失败：{e}", rid)
                        return acct, None, raw, ErrKind.SESSION_DEAD
                    raw = b""
                else:
                    return acct, None, raw, ErrKind.SESSION_DEAD
                # 刷新后可能拿到新 resp，继续走 429/5xx 检查
                if resp.status_code not in (429,) and resp.status_code < 500:
                    return acct, resp, b"", ErrKind.NONE

            if resp.status_code == 429 or resp.status_code >= 500:
                raw = await resp.aread()
                await resp.aclose()
                attempt += 1
                if attempt <= max_retry:
                    obs.log(f"⚠ HTTP {resp.status_code} → 重试 {attempt}/{max_retry}", rid)
                    await asyncio.sleep(backoff * attempt)
                    continue
                return acct, None, raw, upstream.classify(resp.status_code, raw.decode("utf-8", "replace"))

            return acct, resp, b"", ErrKind.NONE

    async def _open_raw(acct: Any, body: dict[str, Any]):
        # 直接用 httpx 的 stream 上下文，手动持有 resp（在 _execute 里统一关闭）
        client = httpx.AsyncClient(timeout=_timeout())
        try:
            req = client.build_request(
                "POST",
                cfg.backend.rstrip("/") + "/v2/chat/completions",
                headers=upstream.chat_headers(cfg, acct),
                json=body,
            )
            resp = await client.send(req, stream=True)
            return _Attached(resp, client)
        except Exception:
            await client.aclose()
            raise

    def _timeout() -> httpx.Timeout:
        return httpx.Timeout(
            connect=cfg.connect_timeout_s, read=cfg.timeout_s, write=60.0, pool=60.0
        )

    # ══════════════════════════════════════════════════════════════════════
    # Chat Completions
    # ══════════════════════════════════════════════════════════════════════

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    ):
        gw.check_auth(authorization, x_api_key)
        payload = await _json(request)
        if not payload.get("messages"):
            raise _bad_request("messages is required")

        wants_stream = bool(payload.get("stream"))
        body = {k: payload[k] for k in PASSTHROUGH_KEYS if k in payload}
        body = _finalize(body)

        rid = obs.new_rid()
        model, model_note = await gw.resolve_model(payload.get("model", "auto"))
        if model_note:
            obs.log(f"↪ 模型名替换：{model_note}", rid)
        if model != payload.get("model"):
            body["model"] = model

        original_body = body
        body = _desensitize(body)
        stat = obs.RequestStat(model, "CHAT", rid)
        obs.log(
            f"▶ CHAT {model} | client={_tag_client(stat, request)}"
            f" | stream={wants_stream} | msgs={len(payload['messages'])}"
            f" | tools={[t.get('function', {}).get('name') for t in (payload.get('tools') or [])] or '-'}",
            rid,
        )
        obs.debug(rid, "REQUEST BODY", body)

        return await _execute(
            gw, rid, stat, body, wants_stream,
            stream_render=lambda r, s: _plain_stream(r, s),
            nonstream_render=lambda r, s: _aggregate_chat(r, s),
            escalate=lambda: _desensitize(original_body, force_compact=True),
        )

    # ══════════════════════════════════════════════════════════════════════
    # Responses（Codex CLI）
    # ══════════════════════════════════════════════════════════════════════

    @app.post("/v1/responses")
    async def create_response(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    ):
        gw.check_auth(authorization, x_api_key)
        payload = await _json(request)

        _capture(cfg, "responses", payload)

        try:
            body, name_route, internal_tools = responses_request_to_chat(
                payload, enable_web_search=bool(cfg.web_search)
            )
        except Exception as e:
            raise _bad_request(f"request conversion error: {e}")
        if internal_tools:
            obs.log(f"⇄ 网关代跑工具：{sorted(internal_tools)}")

        body, proj = project_responses_chat_body(body)
        body = _finalize(body)
        body = _desensitize(body)

        rid = obs.new_rid()
        model, model_note = await gw.resolve_model(payload.get("model", "auto"))
        if model_note:
            obs.log(f"↪ 模型名替换：{model_note}", rid)
        if model != payload.get("model"):
            body["model"] = model
        stat = obs.RequestStat(model, "RESPONSES", rid)
        obs.log(
            f"▶ RESPONSES {model} | client={_tag_client(stat, request)}"
            f" | input_items={len(payload.get('input') or [])}"
            f" | projection[{proj.get('mode')}] msgs {proj.get('original_messages')}→{proj.get('projected_messages')}"
            f" chars {proj.get('original_message_chars')}→{proj.get('projected_message_chars')}"
            f" tools {proj.get('original_tools')}→{proj.get('projected_tools')}",
            rid,
        )
        obs.debug(rid, "RESPONSES → CHAT BODY", body)

        wants_stream = bool(payload.get("stream", True))

        conv_holder: dict[str, Any] = {}

        def _mk_conv() -> ResponsesStreamConverter:
            c = ResponsesStreamConverter(model=model, name_route=name_route,
                                         internal_tools=internal_tools)
            conv_holder["c"] = c
            return c

        def _escalate() -> dict[str, Any]:
            """首轮被上游拒绝时的降级体：紧凑提示词 + 去工具描述。"""
            return _desensitize(body, force_compact=True)

        # 内部工具（web_search）循环需要账号与原始 body 才能重开上游，
        # 这两样在 _execute 里才拿得到，用一个 dict 过桥。
        ctx: dict[str, Any] = {"body": body, "rid": rid,
                               "internal_tools": internal_tools}

        def _render_stream(r, s):
            return _responses_stream(r, s, _mk_conv(), rid, ctx)

        return await _execute(
            gw, rid, stat, body, wants_stream,
            stream_render=_render_stream,
            nonstream_render=lambda r, s: _responses_nonstream(r, s, _mk_conv(), model, rid),
            escalate=_escalate,
            ctx=ctx,
        )

    # ══════════════════════════════════════════════════════════════════════
    # Anthropic Messages（Claude Code / CC Switch）
    # ══════════════════════════════════════════════════════════════════════

    @app.post("/v1/messages")
    async def create_message(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    ):
        gw.check_auth(authorization, x_api_key)
        payload = await _json(request)
        if not payload.get("messages"):
            raise _bad_request("messages is required")

        try:
            body = anthropic_request_to_chat(payload)
        except Exception as e:
            raise _bad_request(f"request conversion error: {e}")
        body = _finalize(body)

        rid = obs.new_rid()
        model, model_note = await gw.resolve_model(payload.get("model", "auto"))
        if model_note:
            obs.log(f"↪ 模型名替换：{model_note}", rid)
        if model != payload.get("model"):
            body["model"] = model

        original_body = body
        body = _desensitize(body)
        stat = obs.RequestStat(model, "ANTHROPIC", rid)
        obs.log(
            f"▶ ANTHROPIC {model} | client={_tag_client(stat, request)}"
            f" | msgs={len(body.get('messages') or [])}",
            rid,
        )
        obs.debug(rid, "ANTHROPIC → CHAT BODY", body)

        # Anthropic 协议默认非流式（与官方一致）；Claude Code 会显式带 stream=true
        wants_stream = bool(payload.get("stream", False))

        return await _execute(
            gw, rid, stat, body, wants_stream,
            stream_render=lambda r, s: _anthropic_stream(r, s, AnthropicStreamConverter(model=model), rid),
            nonstream_render=lambda r, s: _anthropic_nonstream(r, s, model, rid),
            escalate=lambda: _desensitize(original_body, force_compact=True),
        )

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    ):
        gw.check_auth(authorization, x_api_key)
        payload = await _json(request)
        text = json.dumps(payload.get("messages") or [], ensure_ascii=False)
        return {"input_tokens": max(1, len(text) // 4)}  # 粗估，仅用于满足客户端前置调用

    # ══════════════════════════════════════════════════════════════════════
    # 统一执行链
    # ══════════════════════════════════════════════════════════════════════

    async def _execute(
        gw: Gateway,
        rid: str,
        stat: obs.RequestStat,
        body: dict[str, Any],
        wants_stream: bool,
        *,
        stream_render,
        nonstream_render,
        buffer_response: bool = False,
        escalate=None,
        ctx: dict[str, Any] | None = None,
    ):
        acct, reject = await gw.pick()
        if ctx is not None:
            # 供渲染器在需要时重开上游（网关侧内部工具循环）
            ctx["acct"] = acct
        if reject:
            obs.log(f"✗ 账号不可用：{reject}", rid)
            raise HTTPException(
                status_code=503,
                detail={"error": {"message": f"account unavailable: {reject}", "type": "upstream_error"}},
            )
        uid = acct.uid or acct.source
        stat.uid8 = acct.uid[:8]

        if not gw.health.acquire(uid):
            raise HTTPException(
                status_code=503,
                detail={"error": {"message": "in-flight limit reached", "type": "upstream_error"}},
            )

        try:
            acct, resp, err_raw, err_kind = await _open(acct, body, rid)

            if err_kind is not ErrKind.NONE:
                gw.health.note_error(uid, err_kind)
                status = _status_for_kind(err_kind)
                stat.status = status
                stat.done()
                obs.log(f"✗ 上游不可用 | {stat.model} | {err_kind.value} → {status}", rid)
                raise _upstream_error(status, err_raw, err_kind)

            assert resp is not None
            if resp.status_code != 200:
                raw = await resp.aread()
                await resp.aclose()
                text = raw.decode("utf-8", "replace")
                kind = upstream.classify(resp.status_code, text)

                # ── 审核/风控拦截：唯一能在"流开始之前"挽救的机会 ──────────────
                # 此时还没向客户端发出任何字节，可以换成紧凑模式重开一次。
                if escalate is not None and (obs.looks_filtered(text) or kind is ErrKind.CLIENT):
                    obs.log(f"↻ 上游 {resp.status_code}，改用紧凑模式重试一次", rid)
                    try:
                        _, resp2, _, _ = await _open(acct, escalate(), rid)
                    except Exception as e:
                        resp2 = None
                        obs.log(f"紧凑重试异常：{obs.truncate(str(e), 120)}", rid)
                    if resp2 is not None and resp2.status_code == 200:
                        resp = resp2
                        stat.escalated = True
                        obs.log("✓ 紧凑模式重试成功", rid)
                    else:
                        if resp2 is not None:
                            await resp2.aclose()
                        gw.health.note_error(uid, kind)
                        stat.status = resp.status_code
                        stat.done()
                        obs.log(
                            f"✗ HTTP {resp.status_code} | {stat.model} | {kind.value} |"
                            f" {obs.truncate(text, 180)}",
                            rid,
                        )
                        raise _upstream_error(resp.status_code, raw, kind)
                else:
                    gw.health.note_error(uid, kind)
                    stat.status = resp.status_code
                    stat.done()
                    obs.log(
                        f"✗ HTTP {resp.status_code} | {stat.model} | {kind.value} |"
                        f" {obs.truncate(text, 180)}",
                        rid,
                    )
                    raise _upstream_error(resp.status_code, raw, kind)

                # 走到这里说明紧凑重试成功，落到下面的正常成功路径
                gw.health.note_success(uid)
                stat.status = 200
                if not wants_stream:
                    out = await nonstream_render(resp, stat)
                    await resp.aclose()
                    stat.done()
                    return JSONResponse(content=out)
                return StreamingResponse(
                    _guarded_stream(resp, stat, stream_render, uid, gw, rid),
                    media_type="text/event-stream",
                    headers=_sse_headers(),
                )

            # 非压缩模式：先收全量，命中审核则用紧凑模式重试一次
            if buffer_response:
                raw = await resp.aread()
                await resp.aclose()
                text = raw.decode("utf-8", "replace")
                if obs.looks_filtered(text):
                    obs.log("↻ 命中内容审核，改用紧凑模式重试", rid)
                    retry_body = escalate() if escalate is not None else _desensitize(body, force_compact=True)
                    acct2, resp2, _, _ = await _open(acct, retry_body, rid)
                    if resp2 is not None and resp2.status_code == 200:
                        raw = await resp2.aread()
                        await resp2.aclose()
                        stat.escalated = True
                if wants_stream:
                    gw.health.note_success(uid)
                    return StreamingResponse(
                        _replay(raw, stat, stream_render, resp, rid),
                        media_type="text/event-stream",
                        headers=_sse_headers(),
                    )
                gw.health.note_success(uid)
                out = await nonstream_render(_Lines(raw.decode("utf-8", "replace")), stat)
                stat.done()
                return JSONResponse(content=out)

            gw.health.note_success(uid)
            stat.status = 200
            if not wants_stream:
                out = await nonstream_render(resp, stat)
                await resp.aclose()
                stat.done()
                return JSONResponse(content=out)

            return StreamingResponse(
                _guarded_stream(resp, stat, stream_render, uid, gw, rid),
                media_type="text/event-stream",
                headers=_sse_headers(),
            )
        finally:
            gw.health.release(uid)

    # ══════════════════════════════════════════════════════════════════════
    # 流渲染器
    # ══════════════════════════════════════════════════════════════════════

    async def _plain_stream(resp, stat: obs.RequestStat) -> AsyncIterator[bytes]:
        """Chat：后端已是标准 OpenAI SSE，原样转发。"""
        async for chunk in resp.aiter_bytes():
            if chunk:
                stat.first_byte()
                stat.feed_sse(chunk.decode("utf-8", "replace"))
                yield chunk

    async def _run_internal_search(conv, calls: list[dict], rid: str,
                                   cache: dict[str, list],
                                   client_calls: list[dict] | None = None) -> list[dict] | None:
        """执行模型发起的内部工具（当前只有 web_search），返回要追加的 messages。

        形状按 Chat 协议：assistant(tool_calls=[…]) + 每个调用的 tool 结果。
        assistant 的 content 用**本轮**文本，不含更早轮次，避免上下文重复膨胀。

        `cache` 按 query 记住已搜过的结果。模型在结果互相矛盾时会反复重搜同一个词
        （实测第 2、3 轮把前几轮的 query 原样重发），既浪费时间又容易触发上游限流，
        所以重复 query 直接复用并明确告知模型「这条已经搜过」。

        `client_calls` 是本轮已外发给客户端的调用。混合轮次（模型同时调了
        web_search 和客户端工具）时必须把它们以占位结果补进续跑对话 —— 否则
        续跑轮的上游看不到模型调过它们，会把同一个客户端工具再调一遍，
        客户端就收到重复的 function_call。
        """
        from . import websearch as _ws

        tool_calls: list[dict] = []
        outputs: list[tuple[str, str]] = []

        for tc in client_calls or []:
            cid = tc.get("id") or f"call_{obs.new_rid()}"
            tool_calls.append({
                "id": cid, "type": "function",
                "function": {"name": tc.get("name") or "",
                             "arguments": tc.get("args") or "{}"},
            })
            outputs.append((cid,
                            "[This tool call was already delivered to the client; the gateway "
                            "does not have its output. Do not call it again this turn — its "
                            "result will arrive with the client's next message.]"))

        for tc in calls:
            cid = tc.get("id") or f"call_{obs.new_rid()}"
            args_raw = tc.get("args") or "{}"
            try:
                args = json.loads(args_raw) if args_raw.strip() else {}
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            name = tc.get("name") or _ws.TOOL_NAME
            tool_calls.append({
                "id": cid, "type": "function",
                "function": {"name": name, "arguments": args_raw},
            })
            q = str(args.get("query") or "").strip()
            try:
                cnt = int(args.get("count")) if args.get("count") is not None else 8
            except Exception:
                cnt = 8
            if not q:
                outputs.append((cid, "Error: web_search requires a non-empty 'query'."))
                continue
            key = q.lower()
            if key in cache:
                got = cache[key]
                obs.log(f"↺ web_search 复用已有结果 q={q[:52]!r}（模型重复请求）", rid)
                body_text = ("NOTE: this exact query was already searched earlier in this "
                             "conversation; the same results are repeated below. Do not "
                             "search this query again — refine the keywords instead.\n\n")
            else:
                got = await _ws.web_search(cfg, q, cnt)
                cache[key] = got
                body_text = ""
            outputs.append((cid, body_text + _ws.format_results(q, got)))

        msgs: list[dict] = [{
            "role": "assistant",
            "content": conv.round_content(),
            "tool_calls": tool_calls,
        }]
        for cid, text in outputs:
            msgs.append({"role": "tool", "tool_call_id": cid, "content": text})
        return msgs

    async def _responses_stream(resp, stat, conv, rid, ctx=None) -> AsyncIterator[bytes]:
        """Chat SSE → Responses 事件流；并保证一定给出终止事件。

        早期实现直接 `async for` 到流结束就收工：上游被掐断时客户端既收不到
        response.completed 也收不到 response.failed，只能一直挂着等。

        另支持「网关侧内部工具」循环：模型调用 web_search（托管工具在上游不可用，
        已降级成普通 function）时，该 function_call **不转发**给客户端，由网关自己
        检索并把结果作为 tool 消息回灌，再重开上游继续同一轮对话。客户端只看到
        最终答案 —— 中间轮不发终止事件，否则客户端会认为整轮已结束。
        """
        max_rounds = max(0, int(getattr(cfg, "web_search_max_rounds", 3) or 0))
        current = resp
        rounds = 0
        cache: dict[str, list] = {}
        finalized = False
        internal_set = set((ctx or {}).get("internal_tools") or ())
        body_base = (ctx or {}).get("body") or {}
        # 累积续跑消息。**必须逐轮累积**：早期实现每轮都从原始 body 重建，
        # 结果上一轮的搜索结果被丢掉，模型看不到自己搜过什么 —— 于是反复重搜
        # 同一个 query，收尾轮甚至回一句「我无法联网」。
        msgs_acc: list[dict] = list(body_base.get("messages") or [])

        while True:
            try:
                async for line in current.aiter_lines():
                    stat.first_byte()
                    stat.feed_sse(line)  # 上游原始 SSE 里的 usage 也要记账（不影响转发）
                    events = conv.feed_line(line)
                    if events:
                        yield events.encode("utf-8")
            except httpx.HTTPError as e:
                obs.log(f"✗ 上游流中断：{obs.truncate(str(e), 160)}", rid)
                stat.status = 502
                tail = conv.finish(error={"code": "upstream_stream_error",
                                          "message": str(e)[:400],
                                          "type": "upstream_error"})
                if tail:
                    yield tail.encode("utf-8")
                if current is not resp:
                    await current.aclose()
                return

            # ── 是否需要代跑内部工具并续流 ─────────────────────────────────
            calls = conv.internal_calls()
            if not (calls and ctx is not None and ctx.get("acct") is not None
                    and conv.saw_terminal_evidence()):
                break

            if rounds >= max_rounds or (rounds >= 1 and _all_queries_cached(calls, cache)):
                # 搜索预算已用尽：必须再跑一轮「收尾轮」并禁用 web_search，
                # 否则模型会把「我再查证一下」当作终稿交给客户端，答案永远是半截。
                # 另一种情况是「无进展轮」：本轮调用全部命中缓存，模型在原地重发
                # 上一轮的 query，再跑一轮上游不会有任何新信息，同样直接收尾。
                if finalized:
                    break
                finalized = True
                no_progress = rounds < max_rounds
                if no_progress:
                    obs.log(f"⏹ 本轮 {len(calls)} 次调用全部是已搜索过的 query"
                            f"（无进展）→ 直接收尾轮", rid)
                else:
                    obs.log(f"⏹ 搜索已达上限 {max_rounds} 轮 → 收尾轮（停用 web_search）", rid)
                msgs_acc.append({
                    "role": "user",
                    "content": ("[The search phase for this turn is over. You already have the "
                                "search results earlier in this conversation — they are part of "
                                "your context. Give the user your final answer now, citing those "
                                "results. Do not claim you cannot access the internet.]"),
                })
                next_body = _strip_internal_tools(
                    {**body_base, "messages": msgs_acc}, internal_set)
            else:
                rounds += 1
                seeds = " ".join(f"q={_brief(c.get('args'))}" for c in calls[:4])
                obs.log(f"⌕ 网关侧搜索｜第 {rounds} 轮｜{len(calls)} 次调用｜{seeds}", rid)
                try:
                    extra = await _run_internal_search(conv, calls, rid, cache,
                                                       client_calls=conv.client_calls())
                except Exception as e:
                    obs.log(f"⚠ 内部工具执行异常：{type(e).__name__}: {str(e)[:140]}", rid)
                    extra = None
                if not extra:
                    break
                msgs_acc.extend(extra)
                next_body = {**body_base, "messages": msgs_acc}

            try:
                nxt = await _open_raw(ctx["acct"], next_body)
            except Exception as e:
                obs.log(f"⚠ 续流重开上游失败：{type(e).__name__}: {str(e)[:140]}", rid)
                break
            if nxt.status_code != 200:
                raw = await nxt.aread()
                await nxt.aclose()
                obs.log(f"⚠ 续流上游 HTTP {nxt.status_code}："
                        f"{obs.truncate(raw.decode('utf-8', 'replace'), 150)}", rid)
                break

            if current is not resp:
                await current.aclose()
            current = nxt
            conv.begin_next_round()
            stat.first_byte()

        if current is not resp:
            await current.aclose()

        if conv.saw_terminal_evidence():
            yield conv.finish().encode("utf-8")
        else:
            obs.log("⚠ 上游流未见 finish_reason/[DONE]，判定为截断", rid)
            stat.status = 502
            yield conv.finish(incomplete={"reason": "upstream_truncated"}).encode("utf-8")

    async def _anthropic_stream(resp, stat, conv, rid) -> AsyncIterator[bytes]:
        async for line in resp.aiter_lines():
            stat.first_byte()
            stat.feed_sse(line)  # 同上：原始 usage 记账
            events = conv.feed_line(line)
            if events:
                yield events.encode("utf-8")
        tail = conv.finish()
        if tail:
            yield tail.encode("utf-8")

    # ══════════════════════════════════════════════════════════════════════
    # 非流渲染器
    # ══════════════════════════════════════════════════════════════════════

    async def _aggregate_chat(resp, stat: obs.RequestStat) -> dict[str, Any]:
        """把后端 SSE 聚合成单个 chat.completion。"""
        text = (await resp.aread()).decode("utf-8", "replace")
        stat.first_byte()
        stat.feed_sse(text)
        return _aggregate_chat_text(text, stat)

    async def _responses_nonstream(resp, stat, conv, model, rid) -> dict[str, Any]:
        text = (await resp.aread()).decode("utf-8", "replace")
        stat.first_byte()
        for line in text.splitlines():
            stat.feed_sse(line)  # 非流式同样补记账
            conv.feed_line(line)
        if not conv.saw_terminal_evidence():
            obs.log("⚠ 上游流未见 finish_reason/[DONE]，判定为截断", rid)
            stat.status = 502
            return conv.get_nonstream_response(incomplete={"reason": "upstream_truncated"})
        return conv.get_nonstream_response()

    async def _anthropic_nonstream(resp, stat, model, rid) -> dict[str, Any]:
        text = (await resp.aread()).decode("utf-8", "replace")
        stat.first_byte()
        stat.feed_sse(text)
        agg = _aggregate_chat_text(text, stat)
        # 以 chat 聚合结果包装成 Anthropic 非流式响应
        msg = agg["choices"][0]["message"]
        content: list[dict[str, Any]] = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        for tc in msg.get("tool_calls") or []:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": (tc.get("function") or {}).get("name"),
                    "input": _safe_json((tc.get("function") or {}).get("arguments")),
                }
            )
        usage = agg.get("usage") or {}
        return {
            "id": "msg_" + obs.new_rid(),
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content or [{"type": "text", "text": ""}],
            "stop_reason": _map_stop(agg["choices"][0].get("finish_reason")),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

    return app


# ══════════════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════════════


class _Attached:
    """把 httpx.Response 和它的 client 绑在一起，保证最终能一起关闭。"""

    def __init__(self, resp: httpx.Response, client: httpx.AsyncClient) -> None:
        object.__setattr__(self, "_resp", resp)
        object.__setattr__(self, "_client", client)

    def __getattr__(self, item: str) -> Any:
        # 注意用 object.__getattribute__，避免 _resp 缺失时无限递归
        return getattr(object.__getattribute__(self, "_resp"), item)

    async def aclose(self) -> None:
        try:
            await object.__getattribute__(self, "_resp").aclose()
        finally:
            await object.__getattribute__(self, "_client").aclose()


class _Lines:
    """把已收下的整段文本伪装成响应对象，让同一套渲染器既能处理真流、
    也能处理「已缓冲完的字节」（审核重试路径需要先看全量）。"""

    def __init__(self, text: str) -> None:
        self._text = text

    async def aread(self) -> bytes:
        return self._text.encode("utf-8")

    async def aiter_bytes(self):
        yield self._text.encode("utf-8")

    async def aiter_lines(self):
        for line in self._text.splitlines():
            yield line


async def _replay(raw: bytes, stat: obs.RequestStat, render, resp, rid) -> AsyncIterator[bytes]:
    """把缓冲好的内容按流式渲染器输出（用于需要先看全量再决定的路径）。"""
    stat.first_byte()
    stat.feed_sse(raw.decode("utf-8", "replace"))
    async for chunk in render(_Lines(raw.decode("utf-8", "replace")), stat):
        yield chunk


async def _guarded_stream(resp, stat, render, uid, gw, rid) -> AsyncIterator[bytes]:
    """流式转发；传输中断时记账并终止（客户端已收到部分字节，无法重试）。"""
    try:
        async for chunk in render(resp, stat):
            yield chunk
    except httpx.HTTPError as e:
        gw.health.note_error(uid, ErrKind.NETWORK)
        obs.log(f"✗ 流中断：{e}", rid)
        stat.status = stat.status or 502
        stat.done()
        raise
    finally:
        await resp.aclose()
    # 渲染器可能已经判定失败/截断并写了状态，不要覆盖成 200
    stat.status = stat.status or 200
    stat.done()


def _aggregate_chat_text(text: str, stat: obs.RequestStat) -> dict[str, Any]:
    """OpenAI SSE 文本 → 单个非流式 chat.completion。"""
    content: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    model: str | None = None
    finish: str | None = None
    usage: dict[str, Any] | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {
                "id": v["id"],
                "type": "function",
                "function": {"name": v["name"], "arguments": v["arguments"]},
            }
            for _, v in sorted(tool_calls.items())
        ]
        finish = finish or "tool_calls"

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + obs.new_rid(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or stat.model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _map_stop(finish: str | None) -> str:
    return {
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "stop": "end_turn",
    }.get(finish or "stop", "end_turn")


def _safe_json(s: Any) -> Any:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


# 请求体大小上限（学习 new-api MAX_REQUEST_BODY_MB，防超大 base64 载荷撑爆内存）
MAX_BODY_BYTES = 100 * 1024 * 1024  # 100MB，Codex 带图请求正常远低于此


async def _json(request: Request) -> dict[str, Any]:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        raise _bad_request(f"request body too large ({int(cl)} bytes, limit {MAX_BODY_BYTES})")
    try:
        payload = await request.json()
    except Exception as e:
        raise _bad_request(f"bad json: {e}")
    if not isinstance(payload, dict):
        raise _bad_request("request body must be a JSON object")
    return payload


def _capture(cfg: Config, kind: str, payload: dict[str, Any]) -> None:
    """可选诊断抓包：把客户端原始请求体落盘（默认关闭，失败绝不影响主链路）。

    排查"客户端到底发了什么"最快的手段。设 `capture_dir` 或 GTWB_CAPTURE_DIR 即生效。

    注意：抓包失败不能拖垮主链路，但**也不能静默**——早前这里 `except: pass`
    掩盖了 `NameError: os` 的缺失，导致抓包长期"开着却没文件"。首次失败记一条
    告警，之后不再重复刷屏。
    """
    try:
        d = getattr(cfg, "capture_dir", None)
        if not d:
            return
        os.makedirs(d, exist_ok=True)
        max_bytes = 8 * 1024 * 1024
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{obs.new_rid()}.json"
        text = json.dumps(payload, ensure_ascii=False)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text[:max_bytes])
    except Exception as e:  # noqa: BLE001 — 抓包是旁路，绝不能影响主链路
        global _CAPTURE_WARNED
        if not _CAPTURE_WARNED:
            _CAPTURE_WARNED = True
            obs.log(f"⚠ 抓包失败（已忽略，不影响请求）：{type(e).__name__}: {e}")


def _bad_request(msg: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"message": msg, "type": "invalid_request_error"}},
    )


def _upstream_error(status: int, raw: bytes, kind: Any = None) -> HTTPException:
    text = raw.decode("utf-8", "replace") if raw else ""
    detail: Any
    try:
        detail = json.loads(text) if text else {}
    except Exception:
        detail = {"error": {"message": text[:500], "type": "upstream_error"}}
    if kind is not None and not detail:
        detail = {"error": {"message": f"upstream error ({kind.value})", "type": "upstream_error"}}
    return HTTPException(status_code=status or 502, detail=detail)


# 错误类型 → 返回给客户端的 HTTP 状态。
# 早期实现把所有上游异常一律返回 401，客户端（Codex/Claude Code）会把网络抖动
# 误解为"鉴权失效"从而重新登录或直接放弃，掩盖真实原因。
_STATUS_BY_KIND: dict[ErrKind, int] = {
    ErrKind.AUTH: 401,
    ErrKind.SESSION_DEAD: 401,
    ErrKind.HARD_CREDIT: 402,
    ErrKind.SOFT_RATE: 429,
    ErrKind.NOT_FOUND: 404,
    ErrKind.SERVER: 502,
    ErrKind.NETWORK: 502,
    ErrKind.CLIENT: 400,
}


def _status_for_kind(kind: ErrKind) -> int:
    return _STATUS_BY_KIND.get(kind, 502)


def _sse_headers() -> dict[str, str]:
    return {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def _strip_internal_tools(body: dict, internal: set[str]) -> dict:
    """复制 body 并移除由网关代跑的工具，用于「收尾轮」逼模型直接作答。"""
    if not internal:
        return dict(body)
    kept: list[Any] = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        nm = (t.get("function") or {}).get("name") or t.get("name")
        if nm in internal:
            continue
        kept.append(t)
    out = dict(body)
    if kept:
        out["tools"] = kept
    else:
        out.pop("tools", None)
        out.pop("tool_choice", None)
    return out


def _brief(raw: Any, limit: int = 56) -> str:
    """把 tool_call 的 arguments 压成一行短摘要，仅用于日志（便于归因是哪次搜索）。"""
    try:
        obj = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except Exception:
        return f"<{str(raw)[:limit]}>"
    if isinstance(obj, dict) and obj.get("query"):
        return '"' + str(obj["query"])[:limit] + '"'
    return f"<{str(raw)[:limit]}>"


def _all_queries_cached(calls: list[dict], cache: dict[str, list]) -> bool:
    """本轮调用是否**全部**命中已经搜过的 query（即不可能带来任何新信息）。

    动机（2026-09-20 实测）：模型拿不准时会原样重发上一轮的 query ——
    日志里连续出现 `↺ web_search 复用已有结果（模型重复请求）`，
    两个会话因此各烧到 82.5s / 72.1s 才被收尾轮强行结束。
    既然缓存命中不可能产生新结果，这一轮就该直接判为「无进展」交给收尾轮，
    省掉一次完整的上游往返。只要有一个新 query 就返回 False ——
    正常的补充检索（换个关键词再查）绝不会被这道判断打断。
    """
    if not calls:
        return False
    for c in calls:
        try:
            obj = json.loads(c.get("args") or "{}")
        except Exception:
            return False
        if not isinstance(obj, dict):
            return False
        q = str(obj.get("query") or "").strip().lower()
        if not q or q not in cache:
            return False
    return True
