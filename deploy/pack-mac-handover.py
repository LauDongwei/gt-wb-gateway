"""打包 MacBook 交接包（zip）。

三个必须做对的地方，都是踩过的坑：
  1. macOS 不能原生解压 .rar → 只出 .zip
  2. shell 脚本必须是 LF 行尾，否则 Mac 报 `bad interpreter: /bin/bash^M`
  3. zip 默认不保 Unix 权限 → 入口脚本要显式设 0755

用法：python deploy/pack-mac-handover.py
"""
from __future__ import annotations

import pathlib
import stat
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / "gtwb-mac-handover-20261014.zip"
PKG = "gtwb-mac-handover"

# (源文件, 包内路径) —— 代码本体不入包，Mac 上 git clone 拿（便于后续升级）
FILES = [
    ("docs/交接包README-20261014.md", "README.md"),
    ("deploy/mac-handover-prompt.md", "HANDOFF.md"),
    ("docs/部署指南-macOS-20261014.md", "docs/部署指南-macOS-20261014.md"),
    ("docs/全史交接-网关一个月-20260911至20261009.md", "docs/全史交接-网关一个月.md"),
    ("deploy/serve-loop.sh", "deploy/serve-loop.sh"),
    ("deploy/com.gtwb.gateway.plist", "deploy/com.gtwb.gateway.plist"),
]
# skill 单独从用户级目录取（它不在工程仓库里）
SKILL_SRC = pathlib.Path.home() / ".workbuddy" / "skills" / "gt-wb-gateway" / "SKILL.md"
SKILL_DST = "skill/gt-wb-gateway/SKILL.md"

EXECUTABLE = {"deploy/serve-loop.sh"}


def _assert_lf(path: pathlib.Path) -> None:
    """shell 脚本含 CRLF 就会在 Mac 上炸，这里硬拦住。"""
    if path.suffix not in (".sh",):
        return
    raw = path.read_bytes()
    if b"\r\n" in raw:
        raise SystemExit(f"[abort] {path.name} 含 CRLF 行尾，Mac 上会报 bad interpreter")


def main() -> int:
    items = [(ROOT / src, dst) for src, dst in FILES]
    if SKILL_SRC.is_file():
        items.append((SKILL_SRC, SKILL_DST))
    else:
        raise SystemExit(f"[abort] 找不到 skill：{SKILL_SRC}")

    missing = [str(s) for s, _ in items if not s.is_file()]
    if missing:
        raise SystemExit("[abort] 源文件缺失：\n  " + "\n  ".join(missing))

    for src, _ in items:
        _assert_lf(src)

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, dst in items:
            arc = f"{PKG}/{dst}"
            info = zipfile.ZipInfo(arc, date_time=time.localtime(src.stat().st_mtime)[:6])
            data = src.read_bytes()
            if dst in EXECUTABLE:
                # 0o755 << 16 是 Unix 权限位；不设的话解压后没有执行权限
                info.external_attr = (0o755 << 16) | stat.S_IFREG
            else:
                info.external_attr = (0o644 << 16) | stat.S_IFREG
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)

    # ── 自检：解出来再验一遍，不信任自己的写入 ────────────────────────
    with zipfile.ZipFile(OUT) as zf:
        names = zf.namelist()
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"[abort] 压缩包损坏：{bad}")
        sh = zf.read(f"{PKG}/deploy/serve-loop.sh")
        if b"\r\n" in sh:
            raise SystemExit("[abort] 包内 serve-loop.sh 含 CRLF")
        plist = zf.read(f"{PKG}/deploy/com.gtwb.gateway.plist")
        # plist 是模板：占位符必须原样保留，由 Mac 上 sed 替换成实际路径
        if b"__REPO__" not in plist or b"__PYTHON__" not in plist:
            raise SystemExit("[abort] 包内 plist 缺 __REPO__/__PYTHON__ 占位符，Mac 上 sed 无从替换")
        import plistlib
        plistlib.loads(plist)  # XML 必须合法，否则 launchctl 装不上
        for n in names:
            assert n.startswith(PKG + "/"), f"包内路径异常：{n}"

    print(f"[ok] {OUT.name}  {OUT.stat().st_size/1024:.0f} KB  {len(names)} 个文件")
    for n in sorted(names):
        print("   ", n)
    print("\n[提醒] 包内含内部信息（ZeroTier nwid / 账号 uid / 公司名），"
          "仅限内部传递，勿上传公开仓库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
