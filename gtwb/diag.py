"""一键诊断快照。

场景：另一台机器（家里 Mac / 另一台电脑）通过本网关调模型，一旦「不好用」，
本机这边往往只有日志。这个模块把现场聚成一份能直接读的快照：

  · 生效的关键配置（客户端身份、脱敏档位、模型兜底）
  · **谁在连**：按 来源 IP × 客户端 聚合的请求数 / 成功率 / 降级 / 审核命中
  · 最近异常明细（带 rid，可与 gtwb.log 逐行对上）
  · 下一步该看什么

数据来自 `usage-stats.jsonl`（每请求一行）与 `gtwb.log`。
只读，不修改任何东西。

用法：
    python -m gtwb --diag
    python -m gtwb --diag --lines 40      # 多看几条异常
"""

from __future__ import annotations

import json
import os
from collections import deque
from typing import Any

from .config import Config

# 聚合窗口：只读记账文件末尾这么多行，避免大文件拖慢诊断
WINDOW = 5000


def _tail_jsonl(path: str, limit: int = WINDOW) -> list[dict[str, Any]]:
    """读 JSONL 末尾若干行（坏行跳过，绝不让诊断本身报错）。"""
    if not path or not os.path.isfile(path):
        return []
    out: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return list(out)


def _is_bad(rec: dict[str, Any]) -> bool:
    """这条请求算不算「需要看一眼」。"""
    st = rec.get("status")
    if isinstance(st, int) and st >= 400:
        return True
    if rec.get("escalated") or rec.get("filtered"):
        return True
    return False


def _aggregate(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (来源 IP, 客户端) 聚合。"""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in recs:
        ip = r.get("client_ip") or "(旧记录)"
        kind = r.get("client_kind") or "?"
        zone = r.get("client_zone") or ""
        g = groups.setdefault(
            (ip, kind),
            {"ip": ip, "kind": kind, "zone": zone, "n": 0, "ok": 0, "bad": 0,
             "esc": 0, "fil": 0, "last": "", "first": ""},
        )
        g["n"] += 1
        st = r.get("status")
        if isinstance(st, int) and st >= 400:
            g["bad"] += 1
        elif st == 200:
            g["ok"] += 1
        if r.get("escalated"):
            g["esc"] += 1
        if r.get("filtered"):
            g["fil"] += 1
        ts = r.get("ts") or ""
        if ts:
            g["last"] = ts
            if not g["first"]:
                g["first"] = ts
    return sorted(groups.values(), key=lambda g: g["n"], reverse=True)


def _fmt_zone(ip: str) -> str:
    """给诊断输出一个粗粒度区域标签（与 server._client_zone 同口径）。"""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return "本机"
    if ip.startswith("192.168.191."):
        return "ZeroTier"
    if ip.startswith(("192.168.", "10.", "172.")):
        return "局域网"
    if ip == "(旧记录)":
        return "-"
    return "外部"


def snapshot(cfg: Config, lines: int = 20) -> str:
    stat_path = (
        os.path.join(os.path.dirname(cfg.log_path), "usage-stats.jsonl")
        if cfg.log_path
        else ""
    )
    recs = _tail_jsonl(stat_path)
    out: list[str] = []
    w = out.append

    w("══════════ gt-wb-gateway 诊断快照 ══════════")
    w(f"日志      : {cfg.log_path or '(未写文件，仅 stderr)'}")
    w(f"记账      : {stat_path or '(未启用)'}")
    w(f"监听      : {cfg.host}:{cfg.port}")
    w("")

    # ── 关键配置：出问题时最先要确认的几项 ──
    w("── 关键配置 ──")
    if cfg.client_identity:
        w(f"客户端身份上报 : 开 → {cfg.client_name}/{cfg.resolved_client_version()}"
          f" CLI/{cfg.resolved_cli_version()}")
    else:
        w("客户端身份上报 : 关（用量明细「客户端」列会为空）")
    w(f"脱敏 / harness : {'开' if cfg.desensitize else '关'} / "
      f"{'压缩' if cfg.compact_harness else '保留全文'}")
    w(f"模型兜底       : {cfg.model_fallback or '(未设)'}"
      f"｜别名 {len(cfg.model_aliases)} 条")
    w(f"账号轮转       : {'开(不建议)' if cfg.allow_account_rotation else '关'}")
    w("")

    if not recs:
        w("（记账文件为空或不存在——说明还没有请求经过网关，")
        w("  或者 log_path 与运行时不一致）")
        return "\n".join(out)

    # ── 谁在连 ──
    with_client = [r for r in recs if r.get("client_ip")]
    groups = _aggregate(recs)
    w(f"── 客户端分布（记账文件末尾 {len(recs)} 条；其中带来源字段 {len(with_client)} 条）──")
    if not with_client:
        w("  全部是加来源字段之前的历史记录，无法区分来源。")
        w("  让客户端再发一次请求即可产生带来源的新记录。")
    else:
        w(f"  {'来源 IP':<17}{'区域':<10}{'客户端':<15}{'请求':>6}{'成功':>6}{'失败':>6}{'降级':>6}{'审核':>6}  {'最近':<19}")
        for g in groups:
            zone = g["zone"] or _fmt_zone(g["ip"])
            w(f"  {g['ip']:<17}{zone:<10}{g['kind']:<15}{g['n']:>6}{g['ok']:>6}"
              f"{g['bad']:>6}{g['esc']:>6}{g['fil']:>6}  {g['last']:<19}")
    w("")

    # ── 最近的异常 ──
    bad = [r for r in recs if _is_bad(r)]
    w(f"── 最近异常（共 {len(bad)} 条，显示最后 {min(lines, len(bad))} 条）──")
    if not bad:
        w("  无。")
    else:
        for r in bad[-lines:]:
            ip = r.get("client_ip") or "(旧记录)"
            kind = r.get("client_kind") or "?"
            flags = []
            if r.get("escalated"):
                flags.append("降级重试")
            if r.get("filtered"):
                flags.append("审核拦截")
            tail = ("  " + " ".join(flags)) if flags else ""
            w(f"  {r.get('ts','')}  rid={r.get('rid','?')}  {ip} {kind}  "
              f"{r.get('mode','?')} status={r.get('status')} "
              f"finish={r.get('finish')}{tail}")
    w("")

    # ── 下一步 ──
    w("── 怎么看 ──")
    w("  · 拿 rid 去 gtwb.log 里 grep，能看到该请求的投影档位、上游往返与错误原文：")
    w("      grep <rid> gtwb.log")
    w("  · 「降级重试」= 首轮被上游拒绝后改用紧凑模式重发。少量正常；")
    w("    某客户端持续出现，说明该客户端的 system prompt 触发了上游策略。")
    w("  · 「审核拦截」= 上游内容审核。看 gtwb.log 里同一 rid 的上下文。")
    w("  · 想看客户端到底发了什么：设 GTWB_CAPTURE_DIR 后让客户端重放一次。")
    w("  · 某客户端一条记录都没有：说明它根本没连上（网络/鉴权/地址问题），")
    w("    先在那台机器上 curl 一下 /health。")
    return "\n".join(out)
