"""命令行入口：python -m gtwb"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__, obs
from .config import Config, load_config
from .server import Gateway, build_app


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gtwb",
        description="gt-wb-gateway —— 把 WorkBuddy/CodeBuddy 订阅暴露为本机 OpenAI/Anthropic 兼容 API",
    )
    ap.add_argument("--config", help="config.json 路径（默认读当前目录）")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--api-key", help="要求客户端携带的 Bearer key；不设则不校验")
    ap.add_argument("--log", metavar="PATH", help="把请求日志同时写入该文件")
    ap.add_argument("--state-file", help="健康状态落盘路径（默认 state.json）")
    ap.add_argument("--auth-file", help="直接指定登录态 .info 文件")
    ap.add_argument("--auth-dir", help="指定目录，扫描其中全部 .info")
    ap.add_argument("--allow-rotation", action="store_true", help="开启多账号轮转（默认关闭，见 README 风险说明）")
    ap.add_argument("--no-desensitize", action="store_true", help="关闭脱敏（审核拦截概率显著上升）")
    ap.add_argument("--no-compact", action="store_true", help="保留完整 system prompt，仅做零宽脱敏")
    ap.add_argument("--verbose", action="store_true", help="记录完整请求/响应体（排查审核拦截用）")
    ap.add_argument("--timeout", type=float, help="上游单请求超时秒数")
    ap.add_argument("--show-config", action="store_true", help="打印最终生效配置后退出")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    ap.add_argument("--version", action="version", version=f"gt-wb-gateway {__version__}")
    return ap


def _make_config(args: argparse.Namespace) -> Config:
    overrides: dict = {
        "host": args.host,
        "port": args.port,
        "api_key": args.api_key,
        "log_path": args.log,
        "state_file": args.state_file,
        "auth_file": args.auth_file,
        "auth_dir": args.auth_dir,
        "timeout_s": args.timeout,
    }
    if args.allow_rotation:
        overrides["allow_account_rotation"] = True
    if args.no_desensitize:
        overrides["desensitize"] = False
    if args.no_compact:
        overrides["compact_harness"] = False
    return load_config(args.config, overrides=overrides)


async def _preflight(cfg: Config, gw: Gateway) -> bool:
    w = sys.stderr.write
    w("────────── 预检 ──────────\n")
    w(f"版本    : gt-wb-gateway {__version__}\n")
    w(f"Python  : {sys.version.split()[0]}  ({sys.platform})\n")
    w(f"后端    : {cfg.backend}\n")
    w(f"脱敏    : {'开' if cfg.desensitize else '关'}｜"
      f"harness {'压缩' if cfg.compact_harness else '保留全文'}\n")

    cands = gw.accounts.discover_summary()
    if not cands:
        w("[警告] 没找到登录态文件。请先在 WorkBuddy / CodeBuddy 桌面端登录，\n"
          "       或用 --auth-file / GTWB_AUTH_FILE 指定路径。\n")
        w("──────────────────────────\n")
        return False
    w(f"候选文件: {len(cands)} 个\n")
    for p in cands:
        w(f"          {p}\n")

    ok = False
    try:
        acct = await gw.accounts.current()
        ok = True
        w(f"当前账号: {acct.nickname or '(无昵称)'}  uid={acct.uid[:8]}…\n")
        w(f"来源    : {acct.source}\n")
        w(f"域      : {acct.domain or '(未标注，回退后端域)'}\n")
        expired = acct.expires_at_ms > 0 and acct.needs_refresh(0)
        w(f"有效期  : {'已过期（将自动刷新）' if expired else '有效'}\n")
    except Exception as e:
        w(f"[警告] 读取登录态失败：{e}\n")

    try:
        ids = await gw.model_ids()
        w(f"可用模型: {len(ids)} 个 → {', '.join(ids[:12])}"
          f"{' …' if len(ids) > 12 else ''}\n")
    except Exception as e:
        w(f"[警告] 拉取模型清单失败：{e}\n")

    w("──────────────────────────\n")
    return ok


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = _make_config(args)
    except Exception as e:
        print(f"配置加载失败：{e}", file=sys.stderr)
        return 2

    obs.setup(cfg.log_path, verbose=args.verbose)

    if args.show_config:
        import json

        d = cfg.to_dict()
        if d.get("api_key"):
            d["api_key"] = "***"
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    gw = Gateway(cfg)

    if not args.skip_check:
        try:
            asyncio.run(_preflight(cfg, gw))
        except Exception as e:
            print(f"[警告] 预检异常：{e}", file=sys.stderr)

    w = sys.stderr.write
    w(f"\n✅ 监听 http://{cfg.host}:{cfg.port}\n")
    w("   GET  /v1/models             模型清单（动态拉取 + 1h 缓存）\n")
    w("   POST /v1/chat/completions   OpenAI Chat（原生 tools/流式）\n")
    w("   POST /v1/responses          OpenAI Responses（Codex CLI）\n")
    w("   POST /v1/messages           Anthropic Messages（Claude Code / CC Switch）\n")
    w("   GET  /health | /healthz     健康检查\n")
    w("   GET  /status                账号与冷却/熔断状态\n")
    if cfg.api_key:
        w("   鉴权已启用（客户端需带 Bearer key）\n")
    if cfg.log_path:
        w(f"   日志    : {cfg.log_path}\n")
    w("   按 Ctrl+C 退出\n\n")

    import uvicorn

    uvicorn.run(build_app(gw), host=cfg.host, port=cfg.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
