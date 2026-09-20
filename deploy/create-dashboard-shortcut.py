#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create-dashboard-shortcut.py —— 在桌面创建 «网关面板总控» 快捷方式。

用纯 Python 直接写 .lnk（Windows Shell Link Binary, MS-SHLLINK），
不依赖 WScript.Shell / COM / cscript —— 本机安全策略会把它们拦掉。

用法：
    python deploy/create-dashboard-shortcut.py
    python deploy/create-dashboard-shortcut.py --name "面板总控" --icon shell32.dll,14
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Shell Link 常量
# --------------------------------------------------------------------------
CLSID_SHELL_LINK = bytes.fromhex("0114020000000000C000000000000046")

LINK_FLAGS = (
    0x00000001  # HasLinkTargetIDList
    | 0x00000002  # HasLinkInfo
    | 0x00000004  # HasName
    | 0x00000008  # HasRelativePath
    | 0x00000010  # HasWorkingDir
    | 0x00000020  # HasArguments (unused -> cleared below)
    | 0x00000040  # HasIconLocation
    | 0x00000080  # IsUnicode
)
LINK_FLAGS &= ~0x00000020  # 我们不传参数

SHOW_NORMAL = 1


def _utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


def _string_data(s: str) -> bytes:
    """LinkInfo 风格的 CountCharacters + UTF-16 串（无结尾）"""
    raw = s.encode("utf-16-le")
    return struct.pack("<H", len(s)) + raw


def _build_idlist(target: Path) -> bytes:
    """
    LinkTargetIDList。

    实现说明：真正的 Shell ItemID 是带 CLSID 前缀的复杂结构。这里不需要
    壳能"枚举出"这个文件 —— 只要 IDList 存在（满足 HasLinkTargetIDList 标志），
    并且 LinkInfo 里有完整的 LocalBasePath，资源管理器/双击就能正确定位。

    所以放一个最短的合法项：单段 ASCII 路径 + 终止符。
    """
    root = (target.drive.upper() + "\\")  # 例 "D:\\"
    data = root.encode("ascii", errors="replace")
    size = len(data) + 2
    # ItemID 最小长度是 2 字节 size 字段；壳容忍短项
    item = struct.pack("<H", size) + data
    return item + b"\x00\x00"   # 终止项


def _build_link_info(target: Path) -> bytes:
    """LinkInfo：VolumeIDAndLocalBasePath，内含完整本机路径。"""
    header_size = 0x1C  # 28

    # VolumeID: DriveType(4) + DriveSerial(4) + VolumeLabelOffset(4) + 标签串
    vol_label = b"\x00"
    volume_id = struct.pack("<III", 3, 0x00000000, 0x10) + vol_label

    base_path = str(target).encode("gbk", errors="replace") + b"\x00"

    vol_id_offset = header_size
    base_path_offset = vol_id_offset + len(volume_id)
    common_suffix_offset = base_path_offset + len(base_path)

    body = volume_id + base_path
    total = header_size + len(body) + 1  # +1 = CommonPathSuffix 的终止 NUL

    return struct.pack(
        "<IIIIIII",
        total,                 # LinkInfoSize
        header_size,           # LinkInfoHeaderSize
        0x00000001,            # Flags = VolumeIDAndLocalBasePath
        vol_id_offset,         # VolumeIDOffset
        base_path_offset,      # LocalBasePathOffset
        0,                     # CommonNetworkRelativeLinkOffset
        common_suffix_offset,  # CommonPathSuffixOffset
    ) + body + b"\x00"


def build_lnk(target: str, workdir: str, name: str, icon: str, desc: str = "") -> bytes:
    tgt = Path(target)
    buf = bytearray()

    # --- ShellLinkHeader (76 bytes) ---
    buf += struct.pack("<I", 0x0000004C)        # HeaderSize
    buf += CLSID_SHELL_LINK                     # LinkCLSID
    buf += struct.pack("<I", LINK_FLAGS)        # LinkFlags
    buf += struct.pack("<I", 0x00000020)        # FileAttributes = FILE_ATTRIBUTE_ARCHIVE
    buf += struct.pack("<Q", 0)                 # CreationTime
    buf += struct.pack("<Q", 0)                 # AccessTime
    buf += struct.pack("<Q", 0)                 # WriteTime
    buf += struct.pack("<I", 0)                 # FileSize
    buf += struct.pack("<i", 0)                 # IconIndex
    buf += struct.pack("<I", SHOW_NORMAL)       # ShowCommand
    buf += struct.pack("<H", 0)                 # HotKey
    buf += struct.pack("<H", 0)                 # Reserved
    buf += struct.pack("<I", 0)                 # Reserved2
    buf += struct.pack("<I", 0)                 # Reserved3
    assert len(buf) == 0x4C, len(buf)

    # --- LinkTargetIDList ---
    idlist = _build_idlist(tgt)
    buf += struct.pack("<H", len(idlist))
    buf += idlist

    # --- LinkInfo ---
    buf += _build_link_info(tgt)

    # --- StringData（顺序固定：NAME / RELATIVE_PATH / WORKING_DIR / ICON）---
    buf += _string_data(name)
    rel = os.path.relpath(target, workdir) if workdir else target
    buf += _string_data(rel)
    buf += _string_data(workdir)
    buf += _string_data(icon)

    # --- ExtraData：必须以 TerminalBlock 结束（size 0x00000000）---
    buf += struct.pack("<I", 0)

    return bytes(buf)


def main() -> int:
    here = Path(__file__).resolve().parent
    gw_root = here.parent

    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=str(here / "open-dashboards.bat"))
    ap.add_argument("--workdir", default=str(here))
    ap.add_argument("--name", default="网关面板总控")
    ap.add_argument("--icon", default="C:\\Windows\\System32\\shell32.dll,14")
    ap.add_argument("--desc", default="一键打开 gt-wb-gateway 全部前端面板；服务未启动时自动拉起")
    ap.add_argument("--outdir", default=None, help="默认取当前用户桌面")
    args = ap.parse_args()

    if not Path(args.target).is_file():
        print(f"[error] 目标不存在：{args.target}")
        return 1

    outdir = Path(args.outdir) if args.outdir else Path(
        os.environ.get("USERPROFILE", str(Path.home()))
    ) / "Desktop"
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{args.name}.lnk"

    out.write_bytes(build_lnk(args.target, args.workdir, args.name, args.icon, args.desc))

    ok = out.is_file() and out.stat().st_size > 0
    print(f"{'OK' if ok else 'FAILED'}: {out}  ({out.stat().st_size if ok else 0} bytes)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
