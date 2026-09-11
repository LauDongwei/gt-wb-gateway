"""生成「家里电脑接入包」——把另一台机器要用到的全部材料打成一个小文件夹。

包里有什么：
    1-import.bat    打开 CC Switch 导入对话框（读 deeplink.txt）
    2-verify.bat    三步入网自检：ping / health / 真实模型调用
    deeplink.txt    ccswitch:// 一键导入链接（含 API 密钥）
    key.txt         API 密钥（供 2-verify.bat 读取）
    body.json       自检用的请求体
    README.md       给人看的三步说明

用法：
    python deploy/make-client-kit.py                        # 自动探测 ZeroTier 地址
    python deploy/make-client-kit.py --host 203.0.113.81  # 手动指定（示例地址）
    python deploy/make-client-kit.py --out D:/kit           # 指定输出目录

注意：deeplink.txt 与 key.txt 含明文密钥，整包只在自己的机器之间传递。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

# 文件名带连字符，不能普通 import，按路径加载
_spec = importlib.util.spec_from_file_location("make_deeplink", HERE / "make-deeplink.py")
assert _spec and _spec.loader
_make_deeplink = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_make_deeplink)
build_url = _make_deeplink.build_url

ZT_CLI = r"C:\Program Files (x86)\ZeroTier\One\zerotier-cli.bat"


def detect_zerotier_ip() -> str:
    """从 ZeroTier 网络里取本机被分配的 IPv4。"""
    try:
        out = subprocess.run(
            [ZT_CLI, "listnetworks"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        ).stdout
    except Exception:
        return ""
    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})/\d{1,2}\b", out)
    return m.group(1) if m else ""


BAT_IMPORT = """@echo off
REM --- Step 1: register this gateway in CC Switch (opens the import dialog) ---
setlocal
cd /d "%~dp0"
if not exist "deeplink.txt" (
  echo [error] deeplink.txt is missing.
  pause
  exit /b 1
)
set /p DL=<deeplink.txt
echo Opening the CC Switch import dialog...
start "" "%DL%"
echo.
echo If nothing happened, paste the line below into your browser address bar:
echo.
type deeplink.txt
echo.
pause
"""

BAT_VERIFY = """@echo off
REM --- Step 2: verify this machine can reach the gateway over ZeroTier ---
setlocal
cd /d "%~dp0"
set "HOST=__HOST__"
set "PORT=__PORT__"
if not exist "key.txt" (
  echo [error] key.txt is missing.
  pause
  exit /b 1
)
set /p KEY=<key.txt

echo ==========================================================
echo   gt-wb-gateway connectivity check
echo   target : http://%HOST%:%PORT%
echo ==========================================================
echo.
echo [1/3] ping %HOST%
ping -n 2 %HOST%
echo.
echo [2/3] GET /health
curl -s -m 10 "http://%HOST%:%PORT%/health"
echo.
echo.
echo [3/3] POST /v1/chat/completions  (real model call)
curl -s -m 120 "http://%HOST%:%PORT%/v1/chat/completions" ^
  -H "Authorization: Bearer %KEY%" ^
  -H "Content-Type: application/json" ^
  --data-binary "@body.json"
echo.
echo.
echo ----------------------------------------------------------
echo If step 3 printed JSON containing "choices", you are ready.
echo ==========================================================
pause
"""

BODY = {
    "model": "deepseek-v4.1-flash",
    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
}

README = """# 家里电脑接入包

公司机就是**唯一的出口**，WorkBuddy 登录态只留在那台机器上。
这台机器只是通过 ZeroTier 去调用它，**不要在这台机器上安装 WorkBuddy 客户端登录同一账号**。

## 前提

- 这台机器已装 ZeroTier，并已加入你的 ZeroTier 网络（控制台可查 24 位网络 ID）
- 已到 https://my.zerotier.com 给这台设备点过 **Auth** 授权
- 这台机器已装 CC Switch

## 三步

### 1. 导入供应商

双击 `1-import.bat`，CC Switch 会弹出导入确认框，点确认即可。
导入后 CC Switch 里会多出一个 **WorkBuddy GT Gateway** 供应商。

### 2. 自检

双击 `2-verify.bat`。三步全过就算通了：

| 步骤 | 预期 |
|---|---|
| ping | 有回包（延迟看链路，几十到几百毫秒都正常） |
| `/health` | `{"status":"ok","service":"gt-wb-gateway"}` |
| 真实模型调用 | 返回 JSON，含 `"choices"` 字段 |

### 3. 在 CC Switch 里选它

切到 **WorkBuddy GT Gateway**，模型选 `deepseek-v4.1-flash`、`glm-5.3`、`kimi-k3-1` 等。
完整清单可以在浏览器打开 `http://__HOST__:__PORT__/v1/models` 看。

## Clash 建议加的一条规则

本机 Clash 如果开了 TUN，建议在规则最前面加一条（不是必须，但省心）：

```
IP-CIDR,<ZeroTier网段>,DIRECT,no-resolve
```

## 排错

| 现象 | 原因 |
|---|---|
| ping 不通 | ZeroTier 未授权，或这台机器没加入同一个网络 |
| ping 通但 curl 超时 | 公司机的防火墙没放行，或网关没在跑 |
| 返回 401 | `key.txt` 与公司机 `config.json` 里的 `api_key` 不一致 |
| 返回 403 / 11140 | 撞上腾讯风控，别再继续重试，隔一阵子再说 |

## 安全

`deeplink.txt` 和 `key.txt` 里是明文密钥。整包只在**自己的机器之间**传递，
不要发群、不要传网盘公开链接。密钥泄露了就重跑一次
`deploy/setup.ps1 -ApiKey <新密钥>` 换掉它。
"""


def main() -> None:
    cfg_path = ROOT / "config.json"
    if not cfg_path.is_file():
        sys.exit(f"[error] 找不到 {cfg_path}；先跑一次 deploy/setup.ps1")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))

    ap = argparse.ArgumentParser(description="生成家里电脑接入包")
    ap.add_argument("--host", default="", help="客户端访问地址；默认自动探测 ZeroTier IP")
    ap.add_argument("--port", type=int, default=cfg.get("port", 8787))
    ap.add_argument("--model", default="deepseek-v4.1-flash")
    ap.add_argument("--out", default=str(ROOT / "deploy" / "home-client-kit"))
    args = ap.parse_args()

    host = args.host or detect_zerotier_ip()
    if not host:
        sys.exit("[error] 没能自动探测到 ZeroTier 地址，请用 --host 手动指定")

    key = cfg.get("api_key") or ""
    if not key:
        sys.exit("[error] config.json 里 api_key 为空；先跑 deploy/setup.ps1")

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    url = build_url(host, args.port, key, args.model, "WorkBuddy GT Gateway")

    (out / "deeplink.txt").write_text(url + "\n", encoding="utf-8", newline="\n")
    (out / "key.txt").write_text(key + "\n", encoding="utf-8", newline="\n")
    (out / "body.json").write_text(
        json.dumps(BODY, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    # .bat 必须是 CRLF + 纯 ASCII；中文只出现在 README.md 里，避免 cmd 乱码
    for name, text in (
        ("1-import.bat", BAT_IMPORT),
        ("2-verify.bat", BAT_VERIFY.replace("__HOST__", host).replace("__PORT__", str(args.port))),
    ):
        payload = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
        (out / name).write_bytes(payload)

    (out / "README.md").write_text(
        README.replace("__HOST__", host).replace("__PORT__", str(args.port)),
        encoding="utf-8",
        newline="\n",
    )

    print(f"已生成接入包: {out}")
    print(f"  endpoint     : http://{host}:{args.port}/v1")
    print(f"  deeplink 长度: {len(url)} 字符")
    print(f"  api key      : {key[:8]}...{key[-4:]}")
    print()
    print("包里文件:")
    for f in sorted(out.iterdir()):
        print(f"  {f.name:20s} {f.stat().st_size:>7d} bytes")


if __name__ == "__main__":
    main()
