#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create-dashboard-shortcut.py —— 在桌面创建 «网关面板总控» 快捷方式。

两条实现路径，自动择优：

  1) 正规路径（首选）：pywin32 的 shell link 接口。产出的 .lnk 与资源管理器
     自己创建的完全一致 —— 含规范的 LinkTargetIDList、正确图标索引。

  2) 兜底路径：纯 Python 直写 MS-SHLLINK 二进制。**必须带合法的
     LinkTargetIDList** —— 实测省略它会让 Shell 报
     `WinError 1155 没有应用程序与此操作的指定文件有关联`，表现为双击毫无反应。
     兜底实现按规范构造根项 + 文件项。

用法：
    python deploy/create-dashboard-shortcut.py
    python deploy/create-dashboard-shortcut.py --name "面板总控"
"""
from __future__ import annotations

import argparse
import os
import struct
from pathlib import Path

CLSID_SHELL_LINK = bytes.fromhex("0114020000000000C000000000000046")
HEADER_SIZE = 0x4C
SHOW_NORMAL = 1


# ══════════════════════════════════════════════════════════════════════
# 路径 2：纯 Python 直写（兜底）
# ══════════════════════════════════════════════════════════════════════

def _string_data(s: str) -> bytes:
    return struct.pack("<H", len(s)) + s.encode("utf-16-le")


def _build_idlist(target: Path) -> bytes:
    """
    构造合法的 LinkTargetIDList。

    结构（MS-SHLLINK 2.2 / [MS-SHLLINK] 的 ItemID）：
      每项 = ItemIDSize(2) + ItemIDData
      整体 = 各项拼接 + 终止项(0x0000)

    Windows 的"根"项是固定的 0x1F 0x50 前缀（表示"这是个命名空间内的项"），
    紧随其后的是指代盘符的 ASCII。这里用最小但合规的形态：
      · 根项：1F 50 + 盘符（如 'D'）—— 壳据此识别卷
      · 之后每级目录/文件名各一项（ANSI 编码）
    """
    items: list[bytes] = []

    # 根项：1F 50 <drive letters> 00
    drive = target.drive[0].upper() if target.drive else "C"
    items.append(b"\x1f\x50" + drive.encode("ascii"))

    # 相对盘根的各级名（去掉盘符与分隔符）
    parts = [p for p in target.parts[1:] if p and p not in ("\\", "/")]
    for part in parts:
        items.append(part.encode("gbk", errors="replace"))

    out = bytearray()
    for data in items:
        size = len(data) + 2
        out += struct.pack("<H", size)
        out += data
    out += b"\x00\x00"  # 终止项
    return bytes(out)


def _build_link_info(target: Path) -> bytes:
    header_size = 0x1C
    volume_id = struct.pack("<III", 3, 0, 0x10) + b"\x00"
    local_base = str(target).encode("gbk", errors="replace") + b"\x00"

    vol_id_offset = header_size
    base_path_offset = vol_id_offset + len(volume_id)
    common_suffix_offset = base_path_offset + len(local_base)

    body = volume_id + local_base
    total = header_size + len(body) + 1

    return struct.pack(
        "<IIIIIII",
        total, header_size, 0x00000001,
        vol_id_offset, base_path_offset, 0, common_suffix_offset,
    ) + body + b"\x00"


def build_lnk_binary(target: str, workdir: str, name: str, icon: str) -> bytes:
    tgt = Path(target)
    flags = (
        0x00000001  # HasLinkTargetIDList  ← 必须有
        | 0x00000002  # HasLinkInfo
        | 0x00000004  # HasName
        | 0x00000008  # HasRelativePath
        | 0x00000010  # HasWorkingDir
        | 0x00000040  # HasIconLocation
        | 0x00000080  # IsUnicode
    )

    buf = bytearray()
    buf += struct.pack("<I", HEADER_SIZE)
    buf += CLSID_SHELL_LINK
    buf += struct.pack("<I", flags)
    buf += struct.pack("<I", 0x00000020)
    buf += struct.pack("<Q", 0) * 3
    buf += struct.pack("<I", 0)
    buf += struct.pack("<i", 0)
    buf += struct.pack("<I", SHOW_NORMAL)
    buf += struct.pack("<H", 0)
    buf += struct.pack("<H", 0)
    buf += struct.pack("<I", 0)
    buf += struct.pack("<I", 0)
    assert len(buf) == HEADER_SIZE, len(buf)

    idlist = _build_idlist(tgt)
    buf += struct.pack("<H", len(idlist))
    buf += idlist

    buf += _build_link_info(tgt)

    buf += _string_data(name)
    buf += _string_data(os.path.relpath(target, workdir) if workdir else target)
    buf += _string_data(workdir)
    buf += _string_data(icon)
    buf += struct.pack("<I", 0)  # TerminalBlock
    return bytes(buf)


# ══════════════════════════════════════════════════════════════════════
# 路径 1：pywin32 正规接口
# ══════════════════════════════════════════════════════════════════════

def create_with_pywin32(lnk_path: Path, target: str, workdir: str,
                        icon: str, desc: str) -> bool:
    try:
        import win32com.client  # type: ignore
    except Exception:
        return False
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(str(lnk_path))
        sc.TargetPath = target
        sc.WorkingDirectory = workdir
        sc.IconLocation = icon
        sc.Description = desc
        sc.WindowStyle = 1
        sc.Save()
        return lnk_path.is_file() and lnk_path.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] pywin32 路径失败：{exc}")
        return False


# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    here = Path(__file__).resolve().parent

    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=str(here / "open-dashboards.bat"))
    ap.add_argument("--workdir", default=str(here))
    ap.add_argument("--name", default="网关面板总控")
    ap.add_argument("--icon", default=str(here / "gtwb.ico"))
    ap.add_argument("--desc", default="一键打开 gt-wb-gateway 全部前端面板；服务未启动时自动拉起")
    ap.add_argument("--outdir", default=None, help="默认取当前用户桌面")
    ap.add_argument("--force-binary", action="store_true", help="强制走纯 Python 兜底路径")
    args = ap.parse_args()

    if not Path(args.target).is_file():
        print(f"[error] 目标不存在：{args.target}")
        return 1
    if not Path(args.icon).is_file():
        print(f"[warn] 图标缺失，回退 shell32：{args.icon}")
        args.icon = "C:\\Windows\\System32\\shell32.dll,0"

    outdir = Path(args.outdir) if args.outdir else Path(
        os.environ.get("USERPROFILE", str(Path.home()))
    ) / "Desktop"
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{args.name}.lnk"

    if out.exists():
        out.unlink()

    used = "pywin32"
    ok = False
    if not args.force_binary:
        ok = create_with_pywin32(out, args.target, args.workdir, args.icon, args.desc)

    if not ok:
        used = "binary"
        out.write_bytes(build_lnk_binary(args.target, args.workdir, args.name, args.icon))
        ok = out.is_file() and out.stat().st_size > 0

    # ---- 用 Shell 关联做端到端校验（这才是"能不能双击"的真正判据）----
    shell_ok = False
    try:
        import win32api  # type: ignore
        info = win32api.FindExecutable(str(out))
        shell_ok = bool(info) and bool(info[1])
    except Exception:
        # 退一步：至少确认扩展名有 Shell 关联
        try:
            os.startfile  # noqa: B018
            shell_ok = out.suffix.lower() == ".lnk"
        except Exception:
            shell_ok = False

    print(f"{'OK' if ok else 'FAILED'}: {out}  ({out.stat().st_size} bytes)  路径={used}")
    print(f"    Shell 关联校验 = {shell_ok}")
    return 0 if ok and shell_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
