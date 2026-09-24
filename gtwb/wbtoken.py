"""WorkBuddy 加密登录态（$wbEncrypted）解密桥。

WorkBuddy 2026-09 版起把 auth 文件里的 accessToken / refreshToken / nickname
等字段加密成 ``{"$wbEncrypted":1,"envelope":"<base64>"}``（AES-256-GCM）。
网关若把该字段原样塞进请求头，上游立刻 401（实测报错：

    Header value must be str or bytes, not <class 'dict'>

本模块动态复用 **wb-encrypted-token-decrypt** skill 里的解密实现，
把密钥与算法留在 skill 一处 —— 客户端下次换 key 时只改 skill，网关不必动。

找不到解密器时**不静默降级**：只有真的遇到加密字段才报错，
旧版客户端的明文登录态照常工作。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

SKILL_REL = Path(".workbuddy") / "skills" / "wb-encrypted-token-decrypt" / "scripts" / "wb_decrypt.py"

_module: Any = None
_attempted = False
_error = ""


def _candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("GTWB_WB_DECRYPT", "").strip()
    if env:
        out.append(Path(env))
    out.append(Path.home() / SKILL_REL)
    return out


def _load() -> Any:
    """加载解密器（只试一次，结果缓存）。返回模块或 None。"""
    global _module, _attempted, _error
    if _attempted:
        return _module
    _attempted = True
    tried: list[str] = []
    for p in _candidates():
        tried.append(str(p))
        if not p.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_gtwb_wb_decrypt", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001 - 解密器是外部文件，任何异常都只降级为"不可用"
            _error = f"加载 {p} 失败：{exc}"
            continue
        if not hasattr(mod, "unwrap_wbencrypted"):
            _error = f"{p} 不是有效的解密器（缺 unwrap_wbencrypted）"
            continue
        _module = mod
        return _module
    if not _error:
        _error = "未找到解密器，候选路径：" + "；".join(tried)
    return None


def available() -> bool:
    """解密器是否就绪。"""
    return _load() is not None


def last_error() -> str:
    return _error


def is_encrypted(value: Any) -> bool:
    """是否 WorkBuddy 加密包装。"""
    return isinstance(value, dict) and value.get("$wbEncrypted") == 1


def unwrap(value: Any, framing: str = "field", what: str = "字段") -> Any:
    """``$wbEncrypted`` → 明文 str；其余原样返回。

    非加密值直接返回，所以调用方可以无脑套一层。
    """
    if not is_encrypted(value):
        return value
    mod = _load()
    if mod is None:
        raise RuntimeError(
            f"{what} 是 WorkBuddy 加密格式，但解密器不可用（{_error}）。"
            f"装上 wb-encrypted-token-decrypt skill，"
            f"或用 GTWB_WB_DECRYPT 指向 wb_decrypt.py。"
        )
    raw = mod.unwrap_wbencrypted(value, framing)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return raw
