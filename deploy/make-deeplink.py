"""生成 CC Switch 一键导入链接（ccswitch:// deeplink）。

为什么需要它：
    CC Switch 采用「Live 接管」——它会把当前 Codex 供应商的配置**整段写进**
    ~/.codex/config.toml。所以手动往 config.toml 里 append 一个 [profiles.gtwb]，
    在 CC Switch 下次切换供应商时会被整段覆盖掉。
    正确做法是让 gtwb 成为 CC Switch 里的一个「正式供应商」，由它自己来写。

用法：
    python deploy/make-deeplink.py                       # 打印链接（本机 127.0.0.1）
    python deploy/make-deeplink.py --open                # 打印并直接唤起 CC Switch
    python deploy/make-deeplink.py --host 100.64.0.7     # 给另一台机器用（家里电脑）
    python deploy/make-deeplink.py --model kimi-k3-1     # 换默认模型

密钥来源：同项目根的 config.json。链接里含有该密钥，不要转发到聊天群里。
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent

TOML_TEMPLATE = """\
model_provider = "custom"
model = "{model}"
model_reasoning_effort = "high"
disable_response_storage = true

[model_providers.custom]
name = "{name}"
base_url = "{base_url}"
wire_api = "responses"
requires_openai_auth = true
"""


def load_config() -> dict:
    path = ROOT / "config.json"
    if not path.is_file():
        sys.exit(f"[error] 找不到 {path}；先跑一次 deploy/setup.ps1")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_url(host: str, port: int, key: str, model: str, name: str) -> str:
    base_url = f"http://{host}:{port}/v1"
    toml = TOML_TEMPLATE.format(model=model, name=name, base_url=base_url)
    params = {
        "resource": "provider",
        "app": "codex",
        "name": name,
        "endpoint": base_url,
        "apiKey": key,
        "homepage": f"http://{host}:{port}",
        "model": model,
        "configFormat": "toml",
        "config": base64.b64encode(toml.encode("utf-8")).decode("ascii"),
    }
    return "ccswitch://v1/import?" + urllib.parse.urlencode(params)


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="生成 CC Switch 供应商导入链接")
    ap.add_argument("--host", default="", help="客户端要访问的地址；默认取 config.json 的 host")
    ap.add_argument("--port", type=int, default=cfg.get("port", 8787))
    ap.add_argument("--model", default="glm-5.3", help="默认模型")
    ap.add_argument("--name", default="WorkBuddy GT Gateway")
    ap.add_argument("--open", action="store_true", help="生成后直接唤起 CC Switch")
    args = ap.parse_args()

    host = args.host or cfg.get("host") or "127.0.0.1"
    # 0.0.0.0 是「监听全部网卡」，不是可访问地址；客户端必须换成具体地址。
    if host == "0.0.0.0":
        host = "127.0.0.1"

    key = cfg.get("api_key") or ""
    if not key:
        sys.exit("[error] config.json 里 api_key 为空；先跑 deploy/setup.ps1")

    url = build_url(host, args.port, key, args.model, args.name)

    print(f"base_url : http://{host}:{args.port}/v1")
    print(f"model    : {args.model}")
    print(f"api key  : {key[:8]}...{key[-4:]}")
    print()
    print(url)
    print()

    if args.open:
        # Windows: 交给系统协议处理器，CC Switch 会弹出导入确认框
        try:
            import os

            os.startfile(url)  # type: ignore[attr-defined]
            print("已唤起 CC Switch，请在弹窗里确认导入。")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 自动唤起失败（{e}）；请手动复制上面的链接到浏览器打开。")


if __name__ == "__main__":
    main()
