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

import json
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
}

_MODEL_CACHE: dict[str, Any] = {"ids": [], "at": 0.0}
MODEL_TTL_S = 3600


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


def _is_loopback(request: Request) -> bool:
    """请求是否来自本机回环。用于收敛健康端点的信息暴露面。"""
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


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
        if not cfg.desensitize:
            return body
        return desensitize_body(
            body,
            roles=("system", "developer"),
            desensitize_harness_user=True,
            desensitize_tools=True,
            compact_harness=(force_compact or cfg.compact_harness),
            strip_tool_metadata=cfg.strip_tool_metadata,
        )

    # ══════════════════════════════════════════════════════════════════════
    # 上游调用（含分类记账与 401 强刷）
    # ══════════════════════════════════════════════════════════════════════

    async def _note(uid: str, kind: ErrKind, stat: obs.RequestStat) -> None:
        gw.health.note_error(uid, kind)
        stat.status = stat.status or 0

    async def _open(acct: Any, body: dict[str, Any], rid: str):
        """打开上游流；401 且非会话死亡时强制刷新一次再试。"""
        uid = acct.uid or acct.source
        resp = await _open_raw(acct, body)
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
        body = _desensitize(body)

        rid = obs.new_rid()
        model = payload.get("model", "auto")
        stat = obs.RequestStat(model, "CHAT", rid)
        obs.log(
            f"▶ CHAT {model} | stream={wants_stream} | msgs={len(payload['messages'])}"
            f" | tools={[t.get('function', {}).get('name') for t in (payload.get('tools') or [])] or '-'}",
            rid,
        )
        obs.debug(rid, "REQUEST BODY", body)

        return await _execute(
            gw, rid, stat, body, wants_stream,
            stream_render=lambda r, s: _plain_stream(r, s),
            nonstream_render=lambda r, s: _aggregate_chat(r, s),
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

        try:
            body = responses_request_to_chat(payload)
        except Exception as e:
            raise _bad_request(f"request conversion error: {e}")

        body, proj = project_responses_chat_body(body)
        body = _finalize(body)
        body = _desensitize(body)

        rid = obs.new_rid()
        model = payload.get("model", "auto")
        stat = obs.RequestStat(model, "RESPONSES", rid)
        obs.log(
            f"▶ RESPONSES {model} | input_items={len(payload.get('input') or [])}"
            f" | projection msgs {proj.get('original_messages')}→{proj.get('projected_messages')}"
            f" chars {proj.get('original_message_chars')}→{proj.get('projected_message_chars')}"
            f" tools {proj.get('original_tools')}→{proj.get('projected_tools')}",
            rid,
        )
        obs.debug(rid, "RESPONSES → CHAT BODY", body)

        wants_stream = bool(payload.get("stream", True))
        # 仅在「非压缩模式」下才需要缓冲整段以支持审核重试；默认压缩模式下直通流式。
        needs_buffer = cfg.desensitize and not cfg.compact_harness and cfg.retry_on_filter

        conv_holder: dict[str, Any] = {}

        def _mk_conv() -> ResponsesStreamConverter:
            c = ResponsesStreamConverter(model=model)
            conv_holder["c"] = c
            return c

        return await _execute(
            gw, rid, stat, body, wants_stream,
            stream_render=lambda r, s: _responses_stream(r, s, _mk_conv(), rid),
            nonstream_render=lambda r, s: _responses_nonstream(r, s, _mk_conv(), model, rid),
            buffer_response=needs_buffer,
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
        body = _desensitize(body)

        rid = obs.new_rid()
        model = payload.get("model", "auto")
        stat = obs.RequestStat(model, "ANTHROPIC", rid)
        obs.log(f"▶ ANTHROPIC {model} | msgs={len(body.get('messages') or [])}", rid)
        obs.debug(rid, "ANTHROPIC → CHAT BODY", body)

        # Anthropic 协议默认非流式（与官方一致）；Claude Code 会显式带 stream=true
        wants_stream = bool(payload.get("stream", False))

        return await _execute(
            gw, rid, stat, body, wants_stream,
            stream_render=lambda r, s: _anthropic_stream(r, s, AnthropicStreamConverter(model=model), rid),
            nonstream_render=lambda r, s: _anthropic_nonstream(r, s, model, rid),
            buffer_response=cfg.desensitize and not cfg.compact_harness and cfg.retry_on_filter,
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
    ):
        acct, reject = await gw.pick()
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
                stat.status = 401
                stat.done()
                raise _upstream_error(401, err_raw)

            assert resp is not None
            if resp.status_code != 200:
                raw = await resp.aread()
                await resp.aclose()
                text = raw.decode("utf-8", "replace")
                kind = upstream.classify(resp.status_code, text)
                gw.health.note_error(uid, kind)
                stat.status = resp.status_code
                stat.done()
                obs.log(
                    f"✗ HTTP {resp.status_code} | {stat.model} | {kind.value} |"
                    f" {obs.truncate(text, 180)}",
                    rid,
                )
                raise _upstream_error(resp.status_code, raw, kind)

            # 非压缩模式：先收全量，命中审核则用紧凑模式重试一次
            if buffer_response:
                raw = await resp.aread()
                await resp.aclose()
                text = raw.decode("utf-8", "replace")
                if obs.looks_filtered(text):
                    obs.log("↻ 命中内容审核，改用紧凑模式重试", rid)
                    retry_body = _desensitize(body, force_compact=True)
                    acct2, resp2, _, _ = await _open(acct, retry_body, rid)
                    if resp2 is not None and resp2.status_code == 200:
                        raw = await resp2.aread()
                        await resp2.aclose()
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

    async def _responses_stream(resp, stat, conv, rid) -> AsyncIterator[bytes]:
        async for line in resp.aiter_lines():
            stat.first_byte()
            events = conv.feed_line(line)
            if events:
                yield events.encode("utf-8")
        tail = conv.finish()
        if tail:
            yield tail.encode("utf-8")

    async def _anthropic_stream(resp, stat, conv, rid) -> AsyncIterator[bytes]:
        async for line in resp.aiter_lines():
            stat.first_byte()
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
            conv.feed_line(line)
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
        stat.status = 502
        stat.done()
        raise
    finally:
        await resp.aclose()
    stat.status = 200
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


async def _json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as e:
        raise _bad_request(f"bad json: {e}")
    if not isinstance(payload, dict):
        raise _bad_request("request body must be a JSON object")
    return payload


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


def _sse_headers() -> dict[str, str]:
    return {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
