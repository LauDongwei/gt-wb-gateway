"""上游调用与错误分类。

错误分类（ErrKind）语义取自 Sliverkiss/workbuddy2api 的 upstream.Classify：
把「HTTP 状态码 + 响应体关键词」归一到有限的几类，供状态机决定冷却策略。
这是三家实现里对腾讯风控行为刻画最准的一层，直接沿用其判据。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, AsyncIterator

import httpx

from . import config as C
from .auth import Account

# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


class ErrKind(str, Enum):
    NONE = "none"  # 成功
    HARD_CREDIT = "hard_credit"  # 余额/积分不足（402 或关键词）→ 长冷却
    SOFT_RATE = "soft_rate"  # 429 限流 → 短冷却
    SESSION_DEAD = "session_dead"  # 会话失效，需人工重登 → 禁用
    NOT_FOUND = "not_found"  # 上游偶发 404 → 短冷却，不计入熔断
    SERVER = "server"  # 5xx 上游故障 → 计入熔断
    CLIENT = "client"  # 其他 4xx / 业务错误 → 只换号不惩罚
    AUTH = "auth"  # 401 但非 session 死亡 → 触发一次强制刷新
    NETWORK = "network"  # 传输层错误 → 不惩罚


# 判定为「会话彻底失效、必须重新登录」的精确标记。
_SESSION_DEAD_MARKERS = ("Offline user session not found", "12153")
# 判定为「额度耗尽」的关键词（402 之外的软信号）。
_CREDIT_MARKERS = ("余额不足", "积分不足", "quota", "insufficient")
# 非 429 状态码下也可能出现的限流语义（例如 200 + code 11140）。
_RATE_MARKERS = ("11140", "too many requests", "rate limit", "限流")


def classify(status: int, body: str) -> ErrKind:
    """把一次上游响应归一成 ErrKind。"""
    text = body or ""
    low = text.lower()

    # 先判终态：session 死亡优先级最高，避免被误判成限流后反复重试死号。
    if any(m in text for m in _SESSION_DEAD_MARKERS):
        return ErrKind.SESSION_DEAD

    if status == 402:
        return ErrKind.HARD_CREDIT
    if status == 401:
        if any(m.lower() in low for m in _CREDIT_MARKERS):
            return ErrKind.HARD_CREDIT
        return ErrKind.AUTH
    if status == 429:
        return ErrKind.SOFT_RATE
    if status == 404:
        return ErrKind.NOT_FOUND
    if status >= 500:
        return ErrKind.SERVER
    if status >= 400:
        return ErrKind.CLIENT

    # 2xx 但体内含限流 / 额度语义
    if any(m.lower() in low for m in _CREDIT_MARKERS):
        return ErrKind.HARD_CREDIT
    if any(m.lower() in low for m in _RATE_MARKERS):
        return ErrKind.SOFT_RATE

    # 200 + 业务 code 非 0
    try:
        obj = json.loads(text)
        code = obj.get("code") if isinstance(obj, dict) else None
        if code == 11140:
            return ErrKind.SOFT_RATE
        if isinstance(code, int) and code not in (0,):
            return ErrKind.CLIENT
    except Exception:
        pass
    return ErrKind.NONE


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------


def chat_headers(cfg: C.Config, acct: Account) -> dict[str, str]:
    """聊天请求头。

    安全红线：**绝不携带 X-Refresh-Token**（该头只允许出现在刷新端点，
    详见 Sliverkiss headers.go 的同名约束）。
    """
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": cfg.web_origin,
        "Referer": cfg.web_origin + "/",
        "User-Agent": cfg.user_agent,
        "X-Product": "SaaS",
        "Authorization": f"Bearer {acct.access_token}",
    }
    if acct.uid:
        h["X-User-Id"] = acct.uid
    else:
        h["X-No-User-Id"] = "1"
    if acct.enterprise_id:
        h["X-Enterprise-Id"] = acct.enterprise_id
        h["X-Tenant-Id"] = acct.enterprise_id
    else:
        h["X-No-Enterprise-Id"] = "1"
    if acct.domain:
        h["X-Domain"] = acct.domain
    else:
        h["X-No-Department-Info"] = "1"
    return h


def refresh_headers(cfg: C.Config, acct: Account) -> dict[str, str]:
    """刷新端点专属头：唯一允许出现 X-Refresh-Token 的地方。"""
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": cfg.web_origin,
        "Referer": cfg.web_origin + "/",
        "User-Agent": cfg.user_agent,
        "Authorization": f"Bearer {acct.access_token}",
        "X-Refresh-Token": acct.refresh_token,
        "X-Auth-Refresh-Source": "workbuddy",
    }
    if acct.uid:
        h["X-User-Id"] = acct.uid
    if acct.enterprise_id:
        h["X-Enterprise-Id"] = acct.enterprise_id
    if acct.domain:
        h["X-Domain"] = acct.domain
    return h


def _timeout(cfg: C.Config) -> httpx.Timeout:
    return httpx.Timeout(
        connect=cfg.connect_timeout_s, read=cfg.timeout_s, write=60.0, pool=60.0
    )


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------


@asynccontextmanager
async def open_chat(
    cfg: C.Config, acct: Account, body: dict[str, Any]
) -> AsyncIterator[httpx.Response]:
    """打开后端聊天流。调用方负责判断 status_code 并消费/关闭。"""
    url = cfg.backend.rstrip("/") + C.CHAT_PATH
    async with httpx.AsyncClient(timeout=_timeout(cfg)) as client:
        req = client.build_request(
            "POST", url, headers=chat_headers(cfg, acct), json=body
        )
        resp = await client.send(req, stream=True)
        try:
            yield resp
        finally:
            await resp.aclose()


async def chat_collect(
    cfg: C.Config, acct: Account, body: dict[str, Any]
) -> tuple[int, bytes]:
    """完整收下一次后端响应（用于需要先看全量再决定重试的路径）。"""
    async with open_chat(cfg, acct, body) as resp:
        raw = await resp.aread()
        return resp.status_code, raw


async def post_json(
    cfg: C.Config,
    acct: Account,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    """普通 JSON 调用（刷新 token、拉模型列表）。"""
    url = cfg.backend.rstrip("/") + path
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"_raw": resp.text[:500]}


async def fetch_models(cfg: C.Config, acct: Account) -> list[str]:
    """拉账号真实可用的模型清单。

    优先走后端动态接口（实测可用，且比任何硬编码列表都新），
    失败则退回本地 product.json / 内置兜底表。
    """
    try:
        status, data = await post_json(
            cfg,
            acct,
            C.MODELS_PATH,
            {},
            headers={**chat_headers(cfg, acct), "Content-Type": "application/json"},
        )
        # GET 语义：后端对该路径用 POST 也能返回；先试 POST，失败再试 GET
        if status != 200 or not isinstance(data, dict) or data.get("code") != 0:
            status, data = await _get_models(cfg, acct)
        ids = _extract_model_ids(data)
        if ids:
            return ids
    except Exception:
        pass
    return _local_fallback_models()


async def _get_models(cfg: C.Config, acct: Account) -> tuple[int, dict[str, Any]]:
    url = cfg.backend.rstrip("/") + C.MODELS_PATH
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=chat_headers(cfg, acct))
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"_raw": resp.text[:500]}


def _extract_model_ids(data: dict[str, Any]) -> list[str]:
    """从后端返回里抽出模型 id（兼容 agents[].models 与扁平列表两种形状）。"""
    out: list[str] = []
    seen: set[str] = set()

    def push(mid: Any) -> None:
        if isinstance(mid, str) and mid and mid not in seen:
            seen.add(mid)
            out.append(mid)

    inner = data.get("data") if isinstance(data, dict) else None
    if isinstance(inner, dict):
        for agent in inner.get("agents") or []:
            if isinstance(agent, dict) and agent.get("tags") and "cli" in agent["tags"]:
                for m in agent.get("models") or []:
                    push(m)
        if not out:
            for agent in inner.get("agents") or []:
                if isinstance(agent, dict):
                    for m in agent.get("models") or []:
                        push(m)
        for m in inner.get("models") or []:
            push(m if isinstance(m, str) else (m or {}).get("id"))
    for m in (data.get("models") or []) if isinstance(data, dict) else []:
        push(m if isinstance(m, str) else (m or {}).get("id"))
    return out


def _local_fallback_models() -> list[str]:
    """本地兜底：WorkBuddy 的 product.json → 内置表。"""
    import os
    import sys

    cands: list[str] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        cands = [
            os.path.join(
                local, "Programs/WorkBuddy/resources/app.asar.unpacked/cli/product.json"
            ),
            "C:/Program Files/WorkBuddy/resources/app.asar.unpacked/cli/product.json",
        ]
    elif sys.platform == "darwin":
        cands = [
            "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/product.json"
        ]
    else:
        cands = [
            "/opt/WorkBuddy/resources/app.asar.unpacked/cli/product.json",
            os.path.expanduser("~/.local/share/WorkBuddy/cli/product.json"),
        ]

    non_chat = {"text-to-image", "image-to-image", "text-to-video"}
    for p in cands:
        if not os.path.isfile(p):
            continue
        try:
            data = json.loads(open(p, encoding="utf-8").read())
        except Exception:
            continue
        ids: list[str] = []
        for m in data.get("models") or []:
            mid = m.get("id")
            if not mid or any(t in non_chat for t in (m.get("tags") or [])):
                continue
            if m.get("vendor") == "tencent":
                continue
            ids.append(mid)
        if ids:
            return ids
    return list(C.FALLBACK_MODELS)
