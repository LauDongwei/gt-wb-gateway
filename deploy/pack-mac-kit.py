#!/usr/bin/env python
"""把 home-client-kit-mac/ 重新打包为 Mac 可直接解压的 zip。

两个必须处理的点（都是给 Mac 用时最容易踩的）：

1. **行尾必须是 LF**。Windows 上编辑出来的 CRLF 会让 shell 脚本在 macOS 上报
   `bad interpreter: /bin/bash^M`，用户第一步就失败。这里统一转 LF。
2. **shell 脚本要带可执行位**。zip 默认不保 Unix 权限，解压后 `1-import.sh`
   没有 +x。这里用 ZipInfo.external_attr 写入 0755。

用法：
    python deploy/pack-mac-kit.py
"""
from __future__ import annotations

import os
import stat
import zipfile

KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home-client-kit-mac")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home-client-kit-mac.zip")

# 需要可执行位的文件
EXECUTABLE = {"1-import.sh", "2-verify.sh"}
# 纯文本文件（全部转 LF）；其余按二进制原样打包
TEXT_EXT = {".sh", ".md", ".json", ".txt", ".toml"}


def to_lf(path: str) -> bytes:
    with open(path, "rb") as f:
        data = f.read()
    # 统一：先归一成 LF（处理 CRLF 与孤立的 CR）
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def main() -> int:
    if not os.path.isdir(KIT):
        print(f"[error] 目录不存在：{KIT}")
        return 1

    names = sorted(os.listdir(KIT))
    converted = []
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        # 顶层目录项，解压后得到 home-client-kit-mac/
        top = os.path.basename(KIT) + "/"
        zi = zipfile.ZipInfo(top)
        zi.external_attr = (stat.S_IFDIR | 0o755) << 16
        z.writestr(zi, b"")

        for name in names:
            src = os.path.join(KIT, name)
            if not os.path.isfile(src):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in TEXT_EXT:
                raw = to_lf(src)
                if open(src, "rb").read() != raw:
                    converted.append(name)
            else:
                with open(src, "rb") as f:
                    raw = f.read()

            zi = zipfile.ZipInfo(top + name)
            mode = 0o755 if name in EXECUTABLE else 0o644
            zi.external_attr = (stat.S_IFREG | mode) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, raw)

    size = os.path.getsize(OUT)
    print(f"[ok] 已生成 {OUT}")
    print(f"     条目 {len(names)} 个，体积 {size} 字节")
    if converted:
        print(f"     CRLF -> LF 修正：{', '.join(converted)}")
    else:
        print("     行尾已是 LF，无需修正")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
