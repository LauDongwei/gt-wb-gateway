"""账号健康状态机：冷却 / 熔断 / 在途限流 / 状态落盘。

语义取自 Sliverkiss/workbuddy2api 的 internal/pool/pool.go（三家实现里最完整的一套）：
  - 429 软冷却，按连续次数指数退避（60s → 120s → … 封顶 2h）
  - 402 / 额度不足 硬冷却到次日固定时刻（等额度自然恢复）
  - 404 固定短冷却，且不计入熔断（防雪崩）
  - 5xx 计入连续失败，达阈值触发熔断并指数退避
  - 会话失效（12153）永久禁用，需人工重新登录
  - 在途租约限流（同一账号并发上限）

但**去掉了账号池轮转**：默认单账号，仅保留“健康度”判断，
避免落入条款明令禁止、风险最高的用法（见 README 的风险说明）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config as C
from .upstream import ErrKind


@dataclass
class AccountHealth:
    uid: str = ""
    disabled: bool = False
    disabled_reason: str = ""
    cool_until: float = 0.0  # epoch 秒
    cool_reason: str = ""
    softs: int = 0  # 连续软冷却次数（用于指数退避）
    fails: int = 0  # 连续 5xx 次数
    err_total: int = 0
    breaker_until: float = 0.0
    breaker_hits: int = 0
    in_flight: int = 0
    last_ok: float = 0.0

    def cooling(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self.cool_until > now or self.breaker_until > now

    def available(self, now: float | None = None) -> bool:
        return not self.disabled and not self.cooling(now)

    def reason(self, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        if self.disabled:
            return f"disabled: {self.disabled_reason}"
        if self.breaker_until > now:
            return f"breaker {int(self.breaker_until - now)}s"
        if self.cool_until > now:
            return f"cooldown {int(self.cool_until - now)}s ({self.cool_reason})"
        return "ok"


class HealthRegistry:
    def __init__(self, cfg: C.Config) -> None:
        self.cfg = cfg
        self._states: dict[str, AccountHealth] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._path = Path(cfg.state_file) if cfg.state_file else None
        self._load()

    # ── 查询 ──────────────────────────────────────────────────────────────
    def get(self, uid: str) -> AccountHealth:
        with self._lock:
            st = self._states.get(uid)
            if st is None:
                st = AccountHealth(uid=uid)
                self._states[uid] = st
            return st

    def available(self, uid: str) -> bool:
        st = self.get(uid)
        if not st.available():
            return False
        return st.in_flight < self.cfg.max_in_flight

    def acquire(self, uid: str) -> bool:
        with self._lock:
            st = self._states.setdefault(uid, AccountHealth(uid=uid))
            if not st.available() or st.in_flight >= self.cfg.max_in_flight:
                return False
            st.in_flight += 1
            return True

    def release(self, uid: str) -> None:
        with self._lock:
            st = self._states.get(uid)
            if st and st.in_flight > 0:
                st.in_flight -= 1

    # ── 结果上报 ──────────────────────────────────────────────────────────
    def note_success(self, uid: str) -> None:
        """成功即清空所有惩罚计数（与 Sliverkiss 的 NoteSuccess 同语义）。"""
        with self._lock:
            st = self._states.setdefault(uid, AccountHealth(uid=uid))
            st.softs = 0
            st.fails = 0
            st.breaker_hits = 0
            st.breaker_until = 0.0
            st.cool_until = 0.0
            st.cool_reason = ""
            st.last_ok = time.time()
            self._dirty = True
        self._persist_if_dirty()

    def note_error(self, uid: str, kind: ErrKind) -> None:
        now = time.time()
        with self._lock:
            st = self._states.setdefault(uid, AccountHealth(uid=uid))
            st.err_total += 1

            if kind is ErrKind.HARD_CREDIT:
                st.cool_until = _next_hour(self.cfg.hard_cooldown_hour, now)
                st.cool_reason = "额度不足"
                st.softs = 0

            elif kind is ErrKind.SOFT_RATE:
                st.softs += 1
                dur = min(
                    self.cfg.soft_cooldown_s * (2 ** (st.softs - 1)),
                    self.cfg.soft_cooldown_max_s,
                )
                st.cool_until = now + dur
                st.cool_reason = f"限流(第{st.softs}次)"

            elif kind is ErrKind.NOT_FOUND:
                # 固定短冷却，不喂熔断计数
                st.cool_until = now + self.cfg.notfound_cooldown_s
                st.cool_reason = "上游 404"

            elif kind is ErrKind.SESSION_DEAD:
                st.disabled = True
                st.disabled_reason = "会话失效，需重新登录桌面端"

            elif kind is ErrKind.SERVER:
                st.fails += 1
                if st.fails >= self.cfg.breaker_threshold:
                    st.breaker_hits += 1
                    dur = min(
                        self.cfg.breaker_cooldown_s * (2 ** (st.breaker_hits - 1)),
                        self.cfg.breaker_cooldown_max_s,
                    )
                    st.breaker_until = now + dur

            # AUTH / NETWORK / CLIENT：只换路不惩罚（防雪崩）
            self._dirty = True
        self._persist_if_dirty()

    def enable(self, uid: str) -> None:
        """人工解除禁用（重新登录后调用）。"""
        with self._lock:
            st = self._states.get(uid)
            if st:
                st.disabled = False
                st.disabled_reason = ""
                st.cool_until = 0.0
                st.breaker_until = 0.0
                st.softs = 0
                st.fails = 0
                self._dirty = True
        self._persist_if_dirty()

    # ── 观测 ──────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            return {
                uid: {**asdict(st), "state": st.reason(now)}
                for uid, st in self._states.items()
            }

    # ── 持久化 ────────────────────────────────────────────────────────────
    def persist_now(self) -> None:
        """强制把当前状态落盘（测试与优雅退出用）。"""
        with self._lock:
            self._dirty = True
        self._persist_if_dirty()

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for uid, raw in (data.get("accounts") or {}).items():
            if not isinstance(raw, dict):
                continue
            known = {f for f in AccountHealth.__dataclass_fields__}
            self._states[uid] = AccountHealth(
                **{k: v for k, v in raw.items() if k in known}
            )

    def _persist_if_dirty(self) -> None:
        if not self._dirty or not self._path:
            return
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "updated_at": int(time.time()),
                "accounts": {
                    uid: {k: v for k, v in asdict(st).items() if k != "in_flight"}
                    for uid, st in self._states.items()
                },
            }
            self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._path)
        except OSError:
            pass


def _next_hour(hour: int, now: float) -> float:
    """下一个「当天/次日 hour 点」的时间戳。"""
    lt = time.localtime(now)
    target = time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, lt.tm_wday, 0, -1)
    )
    if target <= now:
        target += 86400
    return target
