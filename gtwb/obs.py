"""可观测性：结构化单行请求日志 + 单请求统计。

合并了 Sliverkiss/workbuddy2api 的「每请求一行表格日志（TTFB / token 速率 / uid 前 8 位）」
与 ShouZhuo0413 的「rid 贯穿 + 审核拦截标记」，但只记必要字段：
请求头/令牌/API key 一律不落盘（Sliverkiss 的做法：不读 Authorization 头）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_LOG_PATH: str | None = None
_STAT_PATH: str | None = None
_VERBOSE = False

# 单份日志上限。常驻进程必须自己收敛日志体积，否则长期跑会把磁盘写满。
_LOG_MAX_BYTES = 8 * 1024 * 1024
_STAT_MAX_BYTES = 32 * 1024 * 1024


def setup(log_path: str | None = None, verbose: bool = False) -> None:
    global _LOG_PATH, _STAT_PATH, _VERBOSE
    _LOG_PATH = log_path
    # 用量记账 JSONL 与日志同目录，供用量看板聚合
    _STAT_PATH = os.path.join(os.path.dirname(log_path), "usage-stats.jsonl") if log_path else None
    _VERBOSE = verbose


def _rotate(path: str, limit: int) -> None:
    """单文件超限则轮转一份（保留 .1），只留最近两份。"""
    try:
        if os.path.getsize(path) < limit:
            return
        backup = path + ".1"
        if os.path.exists(backup):
            os.remove(backup)
        os.replace(path, backup)
    except OSError:
        pass  # 轮转失败绝不影响主链路


def _emit(line: str) -> None:
    with _LOCK:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        if _LOG_PATH:
            try:
                _rotate(_LOG_PATH, _LOG_MAX_BYTES)
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # 日志失败绝不影响主链路


def _write_stat(record: dict) -> None:
    """用量记账：每条请求一行 JSONL，失败绝不影响主链路。"""
    if not _STAT_PATH:
        return
    try:
        with _LOCK:
            _rotate(_STAT_PATH, _STAT_MAX_BYTES)
            with open(_STAT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log(msg: str, rid: str = "") -> None:
    prefix = f"[{rid}] " if rid else ""
    _emit(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {prefix}{msg}")


def debug(rid: str, title: str, payload: Any) -> None:
    """仅在 verbose 下输出完整请求/响应体（用于排查审核拦截）。"""
    if not _VERBOSE:
        return
    body = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False, indent=2)
    )
    log(f"── {title} ──\n{body}", rid)


def truncate(s: Any, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


class RequestStat:
    """单次请求的统计：TTFB、token 用量、finish_reason、是否被审核拦截。"""

    def __init__(self, model: str, mode: str, rid: str) -> None:
        self.model = model
        self.mode = mode
        self.rid = rid
        self.t0 = time.perf_counter()
        self.ttfb: float | None = None
        self.status: int | None = None
        self.tokens: int | None = None
        self.finish: str | None = None
        self.tool_calls: list[str] = []
        self.filtered = False
        self.escalated = False   # 是否走过"紧凑模式重试"（首轮被上游拒绝）
        self.uid8 = ""
        self.usage: dict | None = None   # 后端完整 usage（供用量记账）
        self.elapsed: float | None = None

    def first_byte(self) -> None:
        if self.ttfb is None:
            self.ttfb = time.perf_counter() - self.t0

    # ── 增量解析后端 SSE，只为统计，不改动转发的字节 ───────────────────────
    def feed_sse(self, chunk: str) -> None:
        for line in chunk.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                if _looks_filtered(data):
                    self.filtered = True
                continue
            if isinstance(obj, dict) and obj.get("usage"):
                u = obj["usage"]
                self.tokens = (u or {}).get("total_tokens") or self.tokens
                if isinstance(u, dict):
                    self.usage = u  # 完整保留（含 cached/reasoning 明细）
            for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
                if ch.get("finish_reason"):
                    self.finish = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        self.tool_calls.append(nm)
            if _looks_filtered(data):
                self.filtered = True

    def done(self) -> None:
        elapsed = time.perf_counter() - self.t0
        self.elapsed = elapsed
        self._record()
        tag = " ⚠️内容审核拦截" if self.filtered or self.finish == "content-filter" else ""
        parts = [
            f"◀ {self.mode} {self.model}",
            f"{self.status or '?'}",
            f"{elapsed:.1f}s",
            f"ttfb={self.ttfb:.2f}s" if self.ttfb is not None else "ttfb=-",
        ]
        if self.finish:
            parts.append(f"finish={self.finish}")
        if self.tool_calls:
            parts.append(f"tools={self.tool_calls}")
        if self.tokens:
            rate = f"{self.tokens / elapsed:.0f}tok/s" if elapsed > 0 else "-"
            parts.append(f"tokens={self.tokens}({rate})")
        if self.uid8:
            parts.append(f"uid={self.uid8}")
        if self.escalated:
            parts.append("escalated=1")
        log(" | ".join(parts) + tag, self.rid)

    def _record(self) -> None:
        """把本请求追加进用量 JSONL（扁平字段，看板直接聚合）。"""
        u = self.usage or {}
        pt = u.get("prompt_tokens_details") or {}
        ct = u.get("completion_tokens_details") or {}
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": self.mode,
            "model": self.model,
            "status": self.status,
            "elapsed": round(self.elapsed, 3) if self.elapsed is not None else None,
            "ttfb": round(self.ttfb, 3) if self.ttfb is not None else None,
            "finish": self.finish,
            "filtered": self.filtered,
            "escalated": self.escalated,
            "uid8": self.uid8,
            "tools": len(self.tool_calls),
            "prompt_tokens": u.get("prompt_tokens"),
            "completion_tokens": u.get("completion_tokens"),
            "total_tokens": u.get("total_tokens"),
            "cached_tokens": pt.get("cached_tokens"),
            "reasoning_tokens": ct.get("reasoning_tokens"),
        }
        _write_stat(rec)


def _looks_filtered(text: str) -> bool:
    t = (text or "").lower()
    return any(
        k in t
        for k in ("content-filter", "content_filter", "敏感内容", "内容审核", "无法响应您的请求")
    )


# 公开别名：服务层判断「是否被内容审核拦截」时使用
looks_filtered = _looks_filtered


def new_rid() -> str:
    return os.urandom(4).hex()
