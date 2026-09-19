"""刷新各接入包里的 deeplink.txt（确保带上 experimental_bearer_token）。

背景：接入包最初生成的 deeplink 里没有 `experimental_bearer_token`，鉴权只能
依赖客户端本地的 `~/.codex/auth.json`；那文件一旦缺失就 401，而报错指向"登录"，
排查成本很高。make-deeplink.py 现在会内置该字段，这里负责把已存在的包刷新一遍。

同时统一写成 **LF** 行尾（`newline="\\n"`）——Windows 上生成的文件若是 CRLF，
在 macOS 的 shell 里会引发 `bad interpreter` 类问题。

用法：
    python deploy/refresh-deeplinks.py
"""
from __future__ import annotations

import base64
import pathlib
import subprocess
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 需要刷新的接入包（目录名 -> 备注）
TARGETS = ["home-client-kit-mac", "home-client-kit"]


def build_url() -> str:
    res = subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "make-deeplink.py"),
         "--host", "192.168.191.81", "--model", "deepseek-v4.1-flash"],
        capture_output=True, text=True, encoding="utf-8",
    )
    for line in res.stdout.splitlines():
        if line.startswith("ccswitch://"):
            return line.strip()
    sys.exit(f"未取到链接:\n{res.stdout}\n{res.stderr}")


def has_token(url: str) -> bool:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    cfg = base64.b64decode(q["config"][0]).decode()
    return "experimental_bearer_token" in cfg


def main() -> int:
    url = build_url()
    token_ok = has_token(url)
    print(f"生成的链接含 bearer token: {token_ok}")
    if not token_ok:
        print("[warn] 链接里没有 bearer token，请检查 make-deeplink.py")

    for name in TARGETS:
        d = ROOT / "deploy" / name
        if not d.is_dir():
            print(f"跳过（目录不存在）：{name}")
            continue
        out = d / "deeplink.txt"
        out.write_text(url + "\n", encoding="utf-8", newline="\n")
        raw = out.read_bytes()
        print(f"written: {out}  (CR={raw.count(b'\\r')}, {len(raw)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
