"""登录态发现、解析、刷新与原子回写。

合并三家的做法并修掉各自的坑：
  - ShouZhuo0413：mtime 监听（桌面端刷新后自动重载）+ 原子回写
  - neipor：兼容多种回包形状；expiresAt=0 视为「不过期」而不是「已过期」
  - 我们发现并修正：若文件里缺 expiresAt，绝不做「预判过期」，
    否则每次请求都会白白触发一次刷新（原实现的隐性开销）。
  - neipor 默认清单漏了 Windows 路径，这里补上。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import config as C

# account 里可能承载企业 id 的字段名（个人账号通常全为空）。
_ENTERPRISE_KEYS = ("enterpriseId", "enterprise_id", "tenantId", "tenant_id", "orgId")


@dataclass
class Account:
    uid: str = ""
    nickname: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at_ms: int = 0  # 0 = 未知/不过期
    refresh_expires_at_ms: int = 0
    domain: str = ""
    enterprise_id: str = ""
    source: str = ""  # auth 文件绝对路径
    auth: dict[str, Any] = field(default_factory=dict)
    account: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.nickname or self.uid or "unknown"

    def uid8(self) -> str:
        return self.uid[:8]

    def needs_refresh(self, skew_s: int = 300) -> bool:
        if self.expires_at_ms <= 0:
            return False  # 未知有效期 → 不预判，等 401 再刷
        return time.time() * 1000 + skew_s * 1000 >= self.expires_at_ms


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


def candidate_paths() -> list[Path]:
    """按优先级列出可能的登录态文件。"""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        desktop = local / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    elif sys.platform == "darwin":
        desktop = (
            home
            / "Library"
            / "Application Support"
            / "CodeBuddyExtension"
            / "Data"
            / "Public"
            / "auth"
        )
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        desktop = xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"

    out: list[Path] = []
    for d in (desktop, home / ".codebuddy" / "auth"):
        if d.is_dir():
            out.extend(sorted(d.glob("*.info")))
    return out


def _first(container: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = container.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, (int, float)) and v:
            return str(v)
    return ""


def parse_account(path: str | Path) -> Account:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"无法读取登录态文件 {p}：{e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"登录态文件格式异常（顶层不是对象）：{p}")

    auth = data.get("auth") or {}
    # 「当前登录」可能在 account，也可能只在 accounts[0] 里。
    account = data.get("account") or {}
    if not account and isinstance(data.get("accounts"), list) and data["accounts"]:
        account = data["accounts"][0] or {}

    token = auth.get("accessToken") or ""
    if not token:
        raise RuntimeError(f"登录态文件里没有 accessToken，请先在桌面端登录：{p}")

    expires_at = int(auth.get("expiresAt") or 0)
    if not expires_at:
        # 只有 expiresIn 时，用 lastRefreshTime（若有）或当前时间推算
        exp_in = auth.get("expiresIn")
        if exp_in:
            base = int(auth.get("lastRefreshTime") or time.time() * 1000)
            expires_at = base + int(exp_in) * 1000

    refresh_expires_at = int(auth.get("refreshExpiresAt") or 0)

    return Account(
        uid=str(account.get("uid") or ""),
        nickname=str(account.get("nickname") or ""),
        access_token=token,
        refresh_token=auth.get("refreshToken") or "",
        expires_at_ms=expires_at,
        refresh_expires_at_ms=refresh_expires_at,
        domain=str(auth.get("domain") or ""),
        enterprise_id=_first(account, _ENTERPRISE_KEYS),
        source=str(p),
        auth=auth,
        account=account,
    )


# ---------------------------------------------------------------------------
# 刷新
# ---------------------------------------------------------------------------


def _endpoint(cfg: C.Config, acct: Account) -> str:
    dom = (acct.domain or cfg.backend).strip().rstrip("/")
    if not dom.startswith(("http://", "https://")):
        dom = "https://" + dom
    return dom


async def refresh_account(cfg: C.Config, acct: Account) -> Account:
    """用 refreshToken 换新 accessToken，成功后原子回写 auth 文件。"""
    from . import upstream  # 延迟导入，避免与 upstream 的循环依赖

    if not acct.refresh_token:
        raise RuntimeError(f"{acct.source} 里没有 refreshToken，需要重新登录")

    url = _endpoint(cfg, acct) + C.REFRESH_PATH
    headers = upstream.refresh_headers(cfg, acct)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json={})
        try:
            payload = resp.json()
        except Exception:
            payload = {}

    inner = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    new_token = payload.get("accessToken") or inner.get("accessToken") or ""
    if not new_token:
        # 4xx 视为会话已失效；其余情况保留旧 token，让调用方按 401 流程处理。
        if 400 <= resp.status_code < 500:
            raise RuntimeError(
                f"刷新登录态被拒（HTTP {resp.status_code}），请重新登录桌面端"
                f"（或删除并重新生成 {acct.source}）"
            )
        return acct

    acct.access_token = new_token
    if payload.get("refreshToken") or inner.get("refreshToken"):
        acct.refresh_token = payload.get("refreshToken") or inner["refreshToken"]

    expires = payload.get("expiresAt") or inner.get("expiresAt")
    exp_in = payload.get("expiresIn") or inner.get("expiresIn")
    if expires:
        acct.expires_at_ms = int(expires)
    elif exp_in:
        acct.expires_at_ms = int(time.time() * 1000) + int(exp_in) * 1000
    if payload.get("domain") or inner.get("domain"):
        acct.domain = payload.get("domain") or inner["domain"]

    changed: dict[str, Any] = {
        "accessToken": acct.access_token,
        "refreshToken": acct.refresh_token,
        "lastRefreshTime": int(time.time() * 1000),
    }
    if acct.expires_at_ms:
        changed["expiresAt"] = acct.expires_at_ms
    if exp_in:
        changed["expiresIn"] = int(exp_in)
    _write_back(acct, changed)
    return acct


def _write_back(acct: Account, changed: dict[str, Any]) -> None:
    """把刷新结果写回原文件：保留其余字段，先写临时文件再原子替换。"""
    p = Path(acct.source)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    auth = data.setdefault("auth", {})
    auth.update({k: v for k, v in changed.items() if v is not None})
    acct.auth.update({k: v for k, v in changed.items() if v is not None})
    try:
        mode = os.stat(p).st_mode if hasattr(os, "stat") else None
        tmp = p.with_suffix(p.suffix + ".gtwb.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, p)
        if mode is not None and sys.platform != "win32":
            os.chmod(p, mode & 0o777)  # 保持 0600 之类的既有权限
    except OSError:
        pass  # 回写失败仅影响下次启动，不阻断本次请求


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------


class AccountManager:
    """缓存当前账号；文件被外部刷新时自动重载；临近过期时自动续期。"""

    def __init__(self, cfg: C.Config) -> None:
        self.cfg = cfg
        self._accounts: list[Account] = []
        self._index = 0
        self._mtime: float = 0.0
        self._lock = asyncio.Lock()

    # ── 发现 ──────────────────────────────────────────────────────────────
    def _discover(self) -> list[Path]:
        if self.cfg.auth_file:
            p = Path(self.cfg.auth_file)
            return [p] if p.is_file() else []
        if self.cfg.auth_dir:
            d = Path(self.cfg.auth_dir)
            return sorted(d.glob("*.info")) if d.is_dir() else []
        return candidate_paths()

    def discover_summary(self) -> list[str]:
        return [str(p) for p in self._discover()]

    # ── 取用 ──────────────────────────────────────────────────────────────
    async def current(self) -> Account:
        async with self._lock:
            if not self._accounts:
                await self._load_locked()
            acct = self._accounts[self._index]
            # 桌面端在外部刷新过 → 按 mtime 重载，避免用到已被替换的旧 token
            try:
                mt = os.stat(acct.source).st_mtime
            except OSError:
                mt = 0.0
            if mt and mt != self._mtime:
                await self._load_locked()
                acct = self._accounts[self._index]
            if acct.needs_refresh(self.cfg.refresh_skew_s):
                try:
                    await refresh_account(self.cfg, acct)
                except Exception:
                    # 刷新失败但手上还有 token → 先继续用，401 时再走强制刷新
                    if not acct.access_token:
                        raise
            return acct

    async def force_refresh(self) -> Account:
        """上游返回 401 且判定为非会话死亡时调用一次。"""
        async with self._lock:
            if not self._accounts:
                await self._load_locked()
            acct = self._accounts[self._index]
            await refresh_account(self.cfg, acct)
            return acct

    async def rotate(self) -> bool:
        """切到下一个账号。未显式开启多账号轮转时永远返回 False。"""
        if not self.cfg.allow_account_rotation:
            return False
        async with self._lock:
            if len(self._accounts) <= 1:
                return False
            self._index = (self._index + 1) % len(self._accounts)
            return True

    async def all_accounts(self) -> list[Account]:
        async with self._lock:
            if not self._accounts:
                await self._load_locked()
            return list(self._accounts)

    # ── 内部 ──────────────────────────────────────────────────────────────
    async def _load_locked(self) -> None:
        paths = self._discover()
        if not paths:
            raise RuntimeError(
                "未找到登录态文件。请先在 WorkBuddy / CodeBuddy 桌面端登录，"
                "或用 --auth-file 指定路径。"
            )
        accounts: list[Account] = []
        for p in paths:
            try:
                accounts.append(parse_account(p))
            except Exception:
                continue
        if not accounts:
            raise RuntimeError(
                f"扫描到 {len(paths)} 个候选文件，但都不可用（无 accessToken）。"
                "请确认桌面端已登录。"
            )
        self._accounts = accounts
        if self._index >= len(accounts):
            self._index = 0
        try:
            self._mtime = os.stat(accounts[self._index].source).st_mtime
        except OSError:
            self._mtime = 0.0

    def summary(self) -> dict[str, Any]:
        """给 /health 用的账号摘要（不含任何令牌内容）。"""
        if not self._accounts:
            return {"loaded": False}
        acct = self._accounts[self._index]
        return {
            "loaded": True,
            "uid": acct.uid,
            "nickname": acct.nickname,
            "source": acct.source,
            "domain": acct.domain,
            "token_expires_at": acct.expires_at_ms,
            "token_expired": (
                acct.expires_at_ms > 0
                and time.time() * 1000 >= acct.expires_at_ms
            ),
            "account_count": len(self._accounts),
            "rotation_enabled": self.cfg.allow_account_rotation,
        }
