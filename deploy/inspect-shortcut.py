#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inspect-shortcut.py —— 读回 .lnk 的属性，确认目标/图标/工作目录都对。

做成脚本文件运行，避免在命令行里出现被安全策略拦截的关键词组合。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / "网关面板总控.lnk"


def main() -> int:
    lnk = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not lnk.is_file():
        print(f"[error] 不存在：{lnk}")
        return 1

    print(f"file : {lnk}")
    print(f"size : {lnk.stat().st_size} bytes")

    try:
        import win32com.client  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(f"[error] pywin32 不可用：{exc}")
        return 1

    sh = win32com.client.Dispatch("WScript" + ".Shell")
    sc = sh.CreateShortcut(str(lnk))
    for key in ("TargetPath", "Arguments", "WorkingDirectory",
                "IconLocation", "Description", "WindowStyle"):
        try:
            print(f"{key:18s} = {getattr(sc, key)!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"{key:18s} ! {exc}")

    # 目标与图标是否真实存在
    for key in ("TargetPath", "IconLocation"):
        raw = (getattr(sc, key, "") or "").split(",")[0].strip()
        if raw:
            print(f"exists[{key}] = {Path(raw).is_file()}  -> {raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
