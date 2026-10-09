# -*- coding: utf-8 -*-
"""gt-wb-gateway 实时状态看板（只读，独立于网关进程）。

  pythonw deploy/status_server.py            # 默认 0.0.0.0:8735
  python deploy/status_server.py --port 8735 --root D:/workbuddy/研究院/gt-wb-gateway

数据源：
  - usage-stats.jsonl   每请求记账（网关写入）
  - gtwb.log            运行日志（取尾部，统计搜索/降级）
  - http://127.0.0.1:8787/health  网关存活探测
  - ~/.workbuddy/traces WorkBuddy 桌面客户端自身的调用追踪（只读聚合）
不写任何文件，网关挂了看板照样活着并如实显示"离线"。
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from wb_trace_stats import WbTraceIndex
except Exception:                                    # 缺文件也不能让看板挂
    WbTraceIndex = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USAGE = os.path.join(ROOT, "usage-stats.jsonl")
LOG = os.path.join(ROOT, "gtwb.log")
GATEWAY = "http://127.0.0.1:8787/health"
PORT = 8735

for i, a in enumerate(sys.argv):
    if a == "--port" and i + 1 < len(sys.argv):
        PORT = int(sys.argv[i + 1])
    if a == "--root" and i + 1 < len(sys.argv):
        ROOT = sys.argv[i + 1]
        USAGE = os.path.join(ROOT, "usage-stats.jsonl")
        LOG = os.path.join(ROOT, "gtwb.log")


def load_records():
    recs = []
    try:
        with open(USAGE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return recs


def gateway_health():
    try:
        with urlopen(GATEWAY, timeout=3) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
            return {"ok": True, "raw": j}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def tail_log_lines(n=1500):
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 600_000))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except FileNotFoundError:
        return []


def client_zone(ip):
    if not ip:
        return "本机"
    if ip.startswith("192.168.191."):
        return "ZeroTier"
    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
        return "局域网"
    return "外部"


_WB_IDX = None


def _wb_index():
    """进程级复用同一个 WbTraceIndex（内部有文件级缓存，避免重复解析）。"""
    global _WB_IDX
    if _WB_IDX is None and WbTraceIndex is not None:
        _WB_IDX = WbTraceIndex()
    return _WB_IDX


def build_data():
    recs = load_records()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    def dts(r):
        return (r.get("ts") or "")[:10]

    today_recs = [r for r in recs if dts(r) == today]
    health = gateway_health()

    # ---- 今日 KPI ----
    ok = [r for r in today_recs if r.get("status") == 200]
    pin = sum(r.get("prompt_tokens") or 0 for r in today_recs)
    pout = sum(r.get("completion_tokens") or 0 for r in today_recs)
    cached = sum(r.get("cached_tokens") or 0 for r in today_recs)
    lat = [r.get("elapsed") for r in today_recs if r.get("elapsed")]
    ttfb = [r.get("ttfb") for r in today_recs if r.get("ttfb")]

    total_tok = sum(r.get("total_tokens") or 0 for r in today_recs)

    kpi = {
        "requests": len(today_recs),
        "ok": len(ok),
        "fail": len(today_recs) - len(ok),
        "success_rate": round(len(ok) * 100 / len(today_recs), 1) if today_recs else None,
        "prompt_tokens": pin,
        "completion_tokens": pout,
        "total_tokens": total_tok,
        "uncached_tokens": pin - cached,
        "cache_rate": round(cached * 100 / pin, 1) if pin else None,
        "avg_elapsed": round(sum(lat) / len(lat), 1) if lat else None,
        "max_elapsed": round(max(lat), 1) if lat else None,
        "avg_ttfb": round(sum(ttfb) / len(ttfb), 1) if ttfb else None,
        "escalated": sum(1 for r in today_recs if r.get("escalated")),
        "filtered": sum(1 for r in today_recs if r.get("filtered")),
    }

    # ---- 按客户端 ----
    clients = {}
    for r in today_recs:
        ip = r.get("client_ip") or "本机"
        c = clients.setdefault(ip, {"ip": ip, "zone": client_zone(ip), "kind": r.get("client_kind") or "-",
                                    "n": 0, "ok": 0, "pin": 0, "cached": 0, "last": ""})
        c["n"] += 1
        if r.get("status") == 200:
            c["ok"] += 1
        c["pin"] += r.get("prompt_tokens") or 0
        c["cached"] += r.get("cached_tokens") or 0
        c["last"] = max(c["last"], r.get("ts") or "")
    client_rows = sorted(clients.values(), key=lambda c: -c["n"])

    # ---- 按模型（网关侧，今日）----
    models = {}
    for r in today_recs:
        m = r.get("model") or "?"
        e = models.setdefault(m, {"requests": 0, "p": 0, "c": 0, "t": 0, "cached": 0})
        e["requests"] += 1
        e["p"] += r.get("prompt_tokens") or 0
        e["c"] += r.get("completion_tokens") or 0
        e["t"] += r.get("total_tokens") or 0
        e["cached"] += r.get("cached_tokens") or 0

    # ---- 客户端侧（WorkBuddy 桌面端自身调用，全部来自 traces）----
    wb = {"available": False}
    if WbTraceIndex is not None:
        try:
            widx = _wb_index()
            wb_day = widx.day(today)
            wb = {
                "available": True,
                "total": wb_day["total"],
                "models": wb_day["models"],
                "hours": wb_day["hours"],
                "daily": widx.daily(7),
                "days_all": widx.available_days(),
                "summary": widx.summary(),
            }
        except Exception as e:
            wb = {"available": False, "error": str(e)[:160]}

    # ---- 模型用量合并视图：网关 + 客户端 = 合计 ----
    merged = {}
    for m, e in models.items():
        d = merged.setdefault(m, {"model": m, "gw_n": 0, "gw_t": 0, "gw_cached": 0,
                                  "wb_n": 0, "wb_t": 0, "wb_cached": 0, "wb_p": 0})
        d["gw_n"] += e["requests"]
        d["gw_t"] += e["t"]
        d["gw_cached"] += e["cached"]
    for m in (wb.get("models") or []):
        name = m["model"]
        d = merged.setdefault(name, {"model": name, "gw_n": 0, "gw_t": 0, "gw_cached": 0,
                                     "wb_n": 0, "wb_t": 0, "wb_cached": 0, "wb_p": 0})
        d["wb_n"] += m["n"]
        d["wb_t"] += m["t"]
        d["wb_cached"] += m["cached"]
        d["wb_p"] += m["p"]
    for d in merged.values():
        d["total_t"] = d["gw_t"] + d["wb_t"]
        d["total_n"] = d["gw_n"] + d["wb_n"]
    model_rows = sorted(merged.values(), key=lambda x: -x["total_t"])

    # ---- 最近 7 天趋势 ----
    days = []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        rs = [r for r in recs if dts(r) == d]
        okk = sum(1 for r in rs if r.get("status") == 200)
        pin_d = sum(r.get("prompt_tokens") or 0 for r in rs)
        cach_d = sum(r.get("cached_tokens") or 0 for r in rs)
        days.append({"date": d, "requests": len(rs), "ok": okk,
                     "pin": pin_d, "cache": round(cach_d * 100 / pin_d, 1) if pin_d else None})

    # ---- 今日按小时 ----
    hours = [0] * 24
    for r in today_recs:
        try:
            hours[int((r.get("ts") or "00")[11:13])] += 1
        except Exception:
            pass

    # ---- 异常与降级（今日，最多 20 条）----
    bad = [r for r in today_recs if r.get("status") != 200 or r.get("escalated") or r.get("filtered")]
    bad.sort(key=lambda r: r.get("ts") or "")
    bad = bad[-20:]

    # ---- 日志统计：搜索 / 截断提示 ----
    lines = tail_log_lines()
    search_announce = sum(1 for l in lines if "⇄ 网关代跑工具" in l)
    search_exec = sum(1 for l in lines if "⌕" in l)
    search_ok = sum(1 for l in lines if "✓ web_search" in l)
    search_final = sum(1 for l in lines if "⏹" in l)
    trunc = sum(1 for l in lines if "判定为截断" in l)

    # ---- 最近请求（最多 25 条）----
    recent = today_recs[-25:]
    recent.reverse()

    return {
        "generatedAt": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
        "gateway": health,
        "kpi": kpi,
        "clients": client_rows,
        "models": models,
        "modelRows": model_rows,
        "wb": wb,
        "days": days,
        "hours": hours,
        "bad": bad,
        "search": {"announce": search_announce, "exec": search_exec, "ok": search_ok,
                   "final": search_final, "trunc": trunc},
        "recent": recent,
    }


def build_day(date):
    """指定整日（YYYY-MM-DD）的消耗明细：网关侧 + 客户端侧 + 合计。

    网关侧来自 usage-stats.jsonl，客户端侧来自 traces，两者口径一致可直接相加。
    """
    recs = load_records()

    def dts(r):
        return (r.get("ts") or "")[:10]

    rs = [r for r in recs if dts(r) == date]
    gw_total = {"n": len(rs), "p": 0, "c": 0, "t": 0, "cached": 0, "reason": 0}
    gw_models = {}
    gw_hours = [0] * 24
    okn = 0
    for r in rs:
        if r.get("status") == 200:
            okn += 1
        p = r.get("prompt_tokens") or 0
        c = r.get("completion_tokens") or 0
        t = r.get("total_tokens") or 0
        ca = r.get("cached_tokens") or 0
        re_ = r.get("reasoning_tokens") or 0
        gw_total["p"] += p
        gw_total["c"] += c
        gw_total["t"] += t
        gw_total["cached"] += ca
        gw_total["reason"] += re_
        m = r.get("model") or "?"
        e = gw_models.setdefault(m, {"model": m, "n": 0, "p": 0, "c": 0, "t": 0, "cached": 0})
        e["n"] += 1
        e["p"] += p
        e["c"] += c
        e["t"] += t
        e["cached"] += ca
        try:
            gw_hours[int((r.get("ts") or "00")[11:13])] += 1
        except Exception:
            pass
    gw_total["ok"] = okn
    gw_total["cache_rate"] = round(gw_total["cached"] * 100 / gw_total["p"], 1) if gw_total["p"] else None
    for e in gw_models.values():
        e["cache_rate"] = round(e["cached"] * 100 / e["p"], 1) if e["p"] else None

    # 客户端侧
    wb_total = {"n": 0, "p": 0, "c": 0, "t": 0, "cached": 0, "reason": 0, "cache_rate": None}
    wb_models = []
    wb_hours = [0] * 24
    wb_available = False
    if WbTraceIndex is not None:
        try:
            widx = _wb_index()
            d = widx.day(date)
            wb_total = d["total"]
            wb_models = d["models"]
            wb_hours = d["hours"]
            wb_available = True
        except Exception:
            pass

    # 合计（按模型）
    merged = {}
    for m, e in gw_models.items():
        d = merged.setdefault(m, {"model": m, "gw_t": 0, "gw_n": 0, "wb_t": 0, "wb_n": 0})
        d["gw_t"] += e["t"]
        d["gw_n"] += e["n"]
    for m in wb_models:
        d = merged.setdefault(m["model"], {"model": m["model"], "gw_t": 0, "gw_n": 0,
                                           "wb_t": 0, "wb_n": 0})
        d["wb_t"] += m["t"]
        d["wb_n"] += m["n"]
    for d in merged.values():
        d["total_t"] = d["gw_t"] + d["wb_t"]
        d["total_n"] = d["gw_n"] + d["wb_n"]
    model_rows = sorted(merged.values(), key=lambda x: -x["total_t"])

    combined = {
        "n": gw_total["n"] + wb_total.get("n", 0),
        "p": gw_total["p"] + wb_total.get("p", 0),
        "c": gw_total["c"] + wb_total.get("c", 0),
        "t": gw_total["t"] + wb_total.get("t", 0),
        "cached": gw_total["cached"] + wb_total.get("cached", 0),
        "reason": gw_total["reason"] + wb_total.get("reason", 0),
    }
    combined["cache_rate"] = round(combined["cached"] * 100 / combined["p"], 1) if combined["p"] else None

    return {
        "date": date,
        "gatewayTotal": gw_total,
        "clientTotal": wb_total,
        "combinedTotal": combined,
        "modelRows": model_rows,
        "gwHours": gw_hours,
        "wbHours": wb_hours,
        "wbAvailable": wb_available,
    }


PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>gt-wb-gateway 状态看板</title>
<style>
  :root { --bg:#0a0d12; --panel:#10151d; --card:#131a24; --border:#1e2836;
    --text:#e8ecf3; --muted:#8b98ab; --blue:#5b8cff; --green:#34d399;
    --orange:#f59e0b; --purple:#a78bfa; --red:#ef4444; --cyan:#22d3ee; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text);
    font-family:-apple-system,"SF Pro SC","PingFang SC","Microsoft YaHei",sans-serif;
    padding:24px clamp(16px,4vw,48px) 48px; }
  .hd { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:18px; }
  .hd h1 { font-size:22px; font-weight:700; letter-spacing:.2px; }
  .pill { padding:4px 12px; border-radius:999px; font-size:12.5px; font-weight:600;
    border:1px solid var(--border); background:var(--card); color:var(--muted); }
  .pill.on { color:var(--green); border-color:rgba(52,211,153,.35); background:rgba(52,211,153,.08); }
  .pill.off { color:var(--red); border-color:rgba(239,68,68,.4); background:rgba(239,68,68,.08); }
  .pill.warn { color:var(--orange); border-color:rgba(245,158,11,.4); background:rgba(245,158,11,.08); }
  .hd .refresh { margin-left:auto; font-size:12px; color:var(--muted); }
  /* ---- 板块切换 ---- */
  .tabs { display:flex; gap:6px; margin-bottom:18px; border-bottom:1px solid var(--border);
    padding-bottom:0; flex-wrap:wrap; }
  .tab { padding:9px 18px; font-size:14px; font-weight:600; color:var(--muted);
    cursor:pointer; border:none; background:transparent; border-bottom:2px solid transparent;
    margin-bottom:-1px; transition:color .15s,border-color .15s; }
  .tab:hover { color:var(--text); }
  .tab.active { color:var(--blue); border-bottom-color:var(--blue); }
  .tab .tag { font-size:11px; font-weight:500; color:var(--muted); margin-left:6px; }
  .pane { display:none; }
  .pane.active { display:block; animation:fade .25s ease; }
  @keyframes fade { from{opacity:0; transform:translateY(4px);} to{opacity:1; transform:none;} }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:12px; margin-bottom:18px; }
  .kpi { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:16px 18px; }
  .kpi .lb { font-size:12px; color:var(--muted); margin-bottom:6px; }
  .kpi .v { font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }
  .kpi .s { font-size:11.5px; color:var(--muted); margin-top:4px; }
  .v.g{color:var(--green)} .v.b{color:var(--blue)} .v.o{color:var(--orange)} .v.p{color:var(--purple)} .v.c{color:var(--cyan)}
  .sec { background:var(--panel); border:1px solid var(--border); border-radius:16px;
    padding:18px 20px; margin-bottom:16px; }
  .sec h2 { font-size:14.5px; font-weight:650; margin-bottom:12px; color:var(--text); }
  .sec h2 small { font-weight:400; color:var(--muted); margin-left:8px; font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--muted); font-weight:500; padding:6px 10px;
    border-bottom:1px solid var(--border); font-size:12px; white-space:nowrap; }
  td { padding:7px 10px; border-bottom:1px solid rgba(30,40,54,.5); font-variant-numeric:tabular-nums; white-space:nowrap; }
  tr:last-child td { border-bottom:none; }
  .bars { display:flex; align-items:flex-end; gap:4px; height:110px; padding-top:6px; }
  .bar { flex:1; background:linear-gradient(180deg,var(--blue),rgba(91,140,255,.25));
    border-radius:4px 4px 0 0; min-height:2px; position:relative; }
  .bar span { position:absolute; bottom:-20px; left:50%; transform:translateX(-50%);
    font-size:10px; color:var(--muted); white-space:nowrap; }
  .bar b { position:absolute; top:-18px; left:50%; transform:translateX(-50%);
    font-size:10.5px; color:var(--text); font-weight:600; }
  .chip { display:inline-block; padding:2px 9px; border-radius:999px; font-size:11.5px;
    background:var(--card); border:1px solid var(--border); color:var(--muted); margin:0 6px 6px 0; }
  .ok{color:var(--green)} .bad{color:var(--red)} .warn{color:var(--orange)}
  .mut{color:var(--muted)}
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:900px){ .grid2{grid-template-columns:1fr;} }
  .empty { color:var(--muted); font-size:13px; padding:14px 4px; }
  .dayrow { display:flex; align-items:center; gap:10px; padding:5px 0; font-size:12.5px; }
  .dayrow .d { width:86px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .dayrow .track { flex:1; height:9px; background:rgba(30,40,54,.8); border-radius:99px; overflow:hidden; }
  .dayrow .fill { height:100%; background:linear-gradient(90deg,var(--purple),var(--blue)); border-radius:99px; }
  .dayrow .n { width:190px; text-align:right; font-variant-numeric:tabular-nums; }
  /* ---- 模型合并表 ---- */
  .srcbar { display:inline-block; width:56px; height:7px; border-radius:99px; overflow:hidden;
    background:rgba(30,40,54,.9); vertical-align:middle; margin-right:6px; }
  .srcbar i { display:block; height:100%; }
  .legend { font-size:12px; color:var(--muted); margin-top:10px; }
  .legend b { font-weight:600; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:3px; margin:0 5px 0 14px; vertical-align:middle; }
  .dot:first-child { margin-left:0; }
  /* ---- 日期查询 ---- */
  .datebar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:16px; }
  .datebar input[type=date] { background:var(--card); border:1px solid var(--border); color:var(--text);
    padding:7px 12px; border-radius:9px; font-size:13.5px; font-family:inherit; }
  .datebar input[type=date]::-webkit-calendar-picker-indicator { filter:invert(.7); cursor:pointer; }
  .btn { background:var(--blue); color:#fff; border:none; padding:8px 18px; border-radius:9px;
    font-size:13.5px; font-weight:600; cursor:pointer; font-family:inherit; }
  .btn:hover { filter:brightness(1.1); }
  .btn.ghost { background:var(--card); border:1px solid var(--border); color:var(--muted); }
  .quick { font-size:12.5px; color:var(--blue); cursor:pointer; padding:4px 10px;
    border-radius:7px; border:1px solid var(--border); background:var(--card); font-family:inherit; }
  .quick:hover { border-color:var(--blue); }
  .sumrow { display:flex; gap:16px; flex-wrap:wrap; }
  .sumcard { flex:1; min-width:150px; background:var(--card); border:1px solid var(--border);
    border-radius:12px; padding:14px 16px; }
  .sumcard .lb { font-size:12px; color:var(--muted); margin-bottom:5px; }
  .sumcard .v { font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }
  .sumcard .s { font-size:11.5px; color:var(--muted); margin-top:3px; }
</style>
</head>
<body>
<div id="app">加载中…</div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtN = n => n == null ? "—" : Number(n).toLocaleString("en-US");
const fmtM = n => n == null ? "—" : n >= 1e6 ? (n/1e6).toFixed(2)+"M" : n >= 1e3 ? (n/1e3).toFixed(1)+"k" : String(n);
const pct = (a,b) => (!b ? null : Math.round(a*100/b));

let TAB = "overview";
let DATA = null;
let DAY = null;

/* ================= 总览 ================= */
function paneOverview(d) {
  const k = d.kpi || {};
  const search = d.search || {};
  const hours = d.hours || [];
  const hmax = Math.max(1, ...hours);
  const bars = hours.map((v, i) =>
    `<div class="bar" style="height:${Math.max(2, v/hmax*100)}%">${v?`<b>${v}</b>`:""}<span>${i}</span></div>`).join("");

  const clientRows = (d.clients||[]).map(c => {
    const cr = c.pin ? Math.round(c.cached*100/c.pin) : null;
    return `<tr>
      <td><code>${esc(c.ip)}</code></td>
      <td><span class="chip">${esc(c.zone)}</span></td>
      <td>${esc(c.kind)}</td>
      <td>${c.n}</td>
      <td class="${c.ok===c.n?'ok':'warn'}">${c.ok}/${c.n}</td>
      <td>${fmtM(c.pin)}</td>
      <td>${cr==null?'—':cr+'%'}</td>
      <td class="mut">${esc(c.last)}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="empty">今日暂无请求</td></tr>`;

  const modelChips = Object.entries(d.models||{}).map(([m,n]) =>
    `<span class="chip">${esc(m)} · ${n.requests}</span>`).join("") || `<span class="mut">—</span>`;

  const badRows = (d.bad||[]).map(r =>
    `<tr><td class="mut">${esc(r.ts)}</td><td><code>${esc(r.rid)}</code></td>
     <td>${esc(r.model||"")}</td><td class="${r.status==200?'warn':'bad'}">${r.status}</td>
     <td>${r.escalated?'<span class="warn">降级重试</span>':''}${r.filtered?'<span class="warn">审核拦截</span>':''}</td>
     <td class="mut">${esc((r.client_ip||'本机'))}</td></tr>`).join("")
    || `<tr><td colspan="6" class="empty ok">今日无失败 / 降级 / 拦截 ✓</td></tr>`;

  const recentRows = (d.recent||[]).map(r =>
    `<tr><td class="mut">${esc((r.ts||"").slice(11))}</td><td><code>${esc(r.rid)}</code></td>
     <td>${esc(r.model||"")}</td>
     <td class="${r.status==200?'ok':'bad'}">${r.status}</td>
     <td>${r.elapsed!=null?r.elapsed.toFixed(1)+"s":"—"}</td>
     <td>${fmtN(r.total_tokens)}</td>
     <td>${r.cached_tokens&&r.prompt_tokens?Math.round(r.cached_tokens*100/r.prompt_tokens)+"%":"—"}</td>
     <td class="mut">${esc((r.client_ip||'本机'))}</td></tr>`).join("")
    || `<tr><td colspan="8" class="empty">今日暂无请求</td></tr>`;

  const dayMax = Math.max(1, ...(d.days||[]).map(x=>x.requests || 0));
  const dayRows = (d.days||[]).map(x => `
    <div class="dayrow">
      <span class="d">${esc(x.date.slice(5))}</span>
      <div class="track"><div class="fill" style="width:${(x.requests||0)/dayMax*100}%"></div></div>
      <span class="n">${x.requests} 次 · 缓存 ${x.cache==null?"—":x.cache+"%"}</span>
    </div>`).join("");

  const escPill = k.escalated ? `<span class="pill warn">降级 ${k.escalated}</span>` : "";
  const filtPill = k.filtered ? `<span class="pill warn">审核 ${k.filtered}</span>` : "";

  return `
  <div class="grid">
    <div class="kpi"><div class="lb">今日请求（网关）</div><div class="v b">${k.requests??0}</div>
      <div class="s">成功 ${k.ok??0} · 失败 <span class="${k.fail?'bad':''}">${k.fail??0}</span></div></div>
    <div class="kpi"><div class="lb">成功率</div><div class="v ${k.success_rate>=99?'g':'o'}">${k.success_rate==null?"—":k.success_rate+"%"}</div></div>
    <div class="kpi"><div class="lb">缓存命中</div><div class="v p">${k.cache_rate==null?"—":k.cache_rate+"%"}</div>
      <div class="s">未命中 ${fmtM(k.uncached_tokens)}</div></div>
    <div class="kpi"><div class="lb">输入 tokens</div><div class="v">${fmtM(k.prompt_tokens)}</div></div>
    <div class="kpi"><div class="lb">输出 tokens</div><div class="v c">${fmtM(k.completion_tokens)}</div></div>
    <div class="kpi"><div class="lb">延迟 平均/最大</div><div class="v">${k.avg_elapsed??"—"}</div>
      <div class="s">平均秒 · ttfb ${k.avg_ttfb??"—"}s</div></div>
  </div>

  <div class="sec">
    <h2>今日按小时请求量<small>${esc(d.today)}</small></h2>
    <div class="bars">${bars}</div>
    <div style="height:22px"></div>
  </div>

  <div class="grid2">
    <div class="sec">
      <h2>客户端（今日）</h2>
      <table><tr><th>IP</th><th>来源</th><th>类型</th><th>请求</th><th>成功</th><th>输入 tok</th><th>缓存</th><th>最后</th></tr>
      ${clientRows}</table>
      <div style="margin-top:10px">${modelChips}</div>
    </div>
    <div class="sec">
      <h2>近 7 天趋势</h2>
      ${dayRows}
      <h2 style="margin-top:18px">网关代跑搜索（日志尾部统计）</h2>
      <div style="font-size:13px;line-height:2">
        通告工具 <b>${search.announce??0}</b> 次 ·
        执行搜索 <b>${search.exec??0}</b> 轮 ·
        引擎成功 <b class="ok">${search.ok??0}</b> 次 ·
        收尾轮 <b>${search.final??0}</b> 次
        ${search.trunc?`<br><span class="warn">上游截断 ${search.trunc} 次</span>`:""}
      </div>
    </div>
  </div>

  <div class="sec">
    <h2>异常请求（失败 / 降级 / 审核拦截，今日最多 20 条）</h2>
    <table><tr><th>时间</th><th>rid</th><th>模型</th><th>状态</th><th>标记</th><th>客户端</th></tr>
    ${badRows}</table>
  </div>

  <div class="sec">
    <h2>最近请求（最多 25 条，新→旧）</h2>
    <table><tr><th>时间</th><th>rid</th><th>模型</th><th>状态</th><th>耗时</th><th>tokens</th><th>缓存</th><th>客户端</th></tr>
    ${recentRows}</table>
  </div>`;
}

/* ================= 模型用量（合并） ================= */
function paneModels(d) {
  const wb = d.wb || {};
  const rows = d.modelRows || [];
  const gt = rows.reduce((s,r)=>s+r.gw_t,0);
  const wt = rows.reduce((s,r)=>s+r.wb_t,0);
  const gp = rows.reduce((s,r)=>s+r.gw_t,0);
  const maxT = Math.max(1, ...rows.map(r=>r.total_t));

  const body = rows.map(r => {
    const gwW = r.total_t ? r.gw_t/r.total_t*100 : 0;
    const wbW = r.total_t ? r.wb_t/r.total_t*100 : 0;
    return `<tr>
      <td><b>${esc(r.model)}</b></td>
      <td>${fmtN(r.gw_n)}</td>
      <td>${fmtM(r.gw_t)}</td>
      <td>${fmtN(r.wb_n)}</td>
      <td>${fmtM(r.wb_t)}</td>
      <td style="font-weight:700">${fmtM(r.total_t)}</td>
      <td style="min-width:200px">
        <span class="srcbar" title="网关 ${fmtN(r.gw_t)} / 客户端 ${fmtN(r.wb_t)}">
          <i style="width:${gwW}%;background:var(--blue);float:left"></i>
          <i style="width:${wbW}%;background:var(--purple);float:left"></i>
        </span>
        <span class="mut" style="font-size:11.5px">${pct(r.total_t,maxT)}%</span>
      </td></tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">今日暂无数据</td></tr>`;

  const wbSum = wb.summary || {};
  const hist = wb.available ? `
    <div class="sec">
      <h2>WorkBuddy 客户端自身消耗<small>来自本地 traces 追踪</small></h2>
      <div class="sumrow">
        <div class="sumcard"><div class="lb">累计 total tokens</div><div class="v c">${fmtM((wbSum.total||{}).t)}</div>
          <div class="s">${fmtN(wbSum.calls)} 次调用 · ${wbSum.active_days} 个活跃日</div></div>
        <div class="sumcard"><div class="lb">缓存命中率</div><div class="v p">${(wbSum.total||{}).cache_rate??"—"}%</div>
          <div class="s">cached ${fmtM((wbSum.total||{}).cached)}</div></div>
        <div class="sumcard"><div class="lb">覆盖区间</div><div class="v" style="font-size:15px">${esc(wbSum.first_day||"—")} → ${esc(wbSum.last_day||"—")}</div>
          <div class="s">本地 ${wbSum.files} 个 trace 文件</div></div>
      </div>
      <h2 style="margin-top:18px">近 7 天（客户端侧）</h2>
      ${wbDayRows(wb.daily||[])}
    </div>` : `<div class="sec"><div class="empty">客户端追踪数据不可用：${esc(wb.error||"未找到 traces 目录")}</div></div>`;

  return `
  <div class="sec">
    <h2>今日模型用量 · 合计视图<small>网关 + WorkBuddy 客户端</small></h2>
    <table>
      <tr><th>模型</th><th>网关 请求</th><th>网关 tokens</th><th>客户端 请求</th><th>客户端 tokens</th><th>合计 tokens</th><th>占比</th></tr>
      ${body}
    </table>
    <div class="legend">
      <span class="dot" style="background:var(--blue)"></span><b>网关</b>（Codex / Claude Code / curl 经本网关）
      <span class="dot" style="background:var(--purple)"></span><b>客户端</b>（WorkBuddy 桌面端自身）
      <br>今日合计：网关 <b>${fmtN(gt)}</b> + 客户端 <b>${fmtN(wt)}</b> = <b style="color:var(--cyan)">${fmtN(gt+wt)}</b> tokens
    </div>
  </div>
  ${hist}`;
}

function wbDayRows(daily) {
  const max = Math.max(1, ...daily.map(x=>x.t||0));
  return daily.map(x => `
    <div class="dayrow">
      <span class="d">${esc(x.date.slice(5))}</span>
      <div class="track"><div class="fill" style="width:${(x.t||0)/max*100}%;background:linear-gradient(90deg,var(--cyan),var(--purple))"></div></div>
      <span class="n">${fmtM(x.t)} · ${x.n||0} 次 · 缓存 ${x.cache_rate==null?"—":x.cache_rate+"%"}</span>
    </div>`).join("") || `<div class="empty">暂无数据</div>`;
}

/* ================= 按日明细 ================= */
function paneDay(d) {
  if (!DAY) return `<div class="empty">选择日期后查看整日消耗明细</div>`;
  if (DAY.error) return `<div class="empty bad">${esc(DAY.error)}</div>`;
  const g = DAY.gatewayTotal || {}, c = DAY.clientTotal || {}, tot = DAY.combinedTotal || {};
  const rows = DAY.modelRows || [];
  const maxT = Math.max(1, ...rows.map(r=>r.total_t));

  const body = rows.map(r => `
    <tr>
      <td><b>${esc(r.model)}</b></td>
      <td>${fmtM(r.gw_t)}</td>
      <td>${fmtM(r.wb_t)}</td>
      <td style="font-weight:700">${fmtM(r.total_t)}</td>
      <td class="mut">${fmtN(r.total_n)}</td>
      <td style="min-width:140px">
        <span class="srcbar" title="网关 ${fmtN(r.gw_t)} / 客户端 ${fmtN(r.wb_t)}">
          <i style="width:${r.total_t?r.gw_t/r.total_t*100:0}%;background:var(--blue);float:left"></i>
          <i style="width:${r.total_t?r.wb_t/r.total_t*100:0}%;background:var(--purple);float:left"></i>
        </span>
        <span class="mut" style="font-size:11.5px">${pct(r.total_t,maxT)}%</span>
      </td>
    </tr>`).join("") || `<tr><td colspan="6" class="empty">该日无数据</td></tr>`;

  const hmax = Math.max(1, ...(DAY.gwHours||[]), ...(DAY.wbHours||[]));
  const bars = (DAY.gwHours||[]).map((v,i) => {
    const w = (DAY.wbHours||[])[i] || 0;
    const h = Math.max(2, (v+w)/hmax*100);
    const gwH = (v+w) ? v/(v+w)*100 : 0;
    return `<div class="bar" style="height:${h}%;background:none" title="${i}时 · 网关${v} 客户端${w}">
      <div style="height:${gwH}%;background:var(--blue)"></div>
      <div style="height:${100-gwH}%;background:var(--purple)"></div>
      <span>${i}</span></div>`;
  }).join("");

  return `
  <div class="sumrow" style="margin-bottom:16px">
    <div class="sumcard"><div class="lb">整日合计 tokens</div><div class="v c">${fmtM(tot.t)}</div>
      <div class="s">${fmtN(tot.n)} 次调用 · 缓存命中 ${tot.cache_rate==null?"—":tot.cache_rate+"%"}</div></div>
    <div class="sumcard"><div class="lb">网关（第三方）</div><div class="v b">${fmtM(g.t)}</div>
      <div class="s">${fmtN(g.n)} 次 · 输入 ${fmtM(g.p)} · 输出 ${fmtM(g.c)}</div></div>
    <div class="sumcard"><div class="lb">WorkBuddy 客户端</div><div class="v p">${fmtM(c.t)}</div>
      <div class="s">${fmtN(c.n)} 次 · 缓存 ${c.cache_rate==null?"—":c.cache_rate+"%"}</div></div>
    <div class="sumcard"><div class="lb">缓存命中 / 未命中</div><div class="v">${fmtM(tot.cached)}</div>
      <div class="s">未命中 ${fmtM((tot.p||0)-(tot.cached||0))}</div></div>
  </div>

  <div class="sec">
    <h2>按模型拆分<small>${esc(DAY.date)}</small></h2>
    <table>
      <tr><th>模型</th><th>网关 tokens</th><th>客户端 tokens</th><th>合计 tokens</th><th>调用数</th><th>占比</th></tr>
      ${body}
    </table>
  </div>

  <div class="sec">
    <h2>按小时调用分布<small>蓝=网关 · 紫=客户端</small></h2>
    <div class="bars">${bars}</div>
    <div style="height:22px"></div>
  </div>`;
}

/* ================= 骨架 ================= */
function render(d) {
  DATA = d;
  const g = d.gateway || {};
  const healthPill = g.ok ? `<span class="pill on">● 网关在线 · 8787</span>`
                          : `<span class="pill off">● 网关离线</span>`;
  const wb = d.wb || {};
  const wbPill = wb.available ? `<span class="pill on">● 客户端追踪在线</span>`
                              : `<span class="pill warn">● 客户端追踪缺失</span>`;
  const weekAll = (wb.summary||{}).total || {};
  const tabs = [
    ["overview", "总览", ""],
    ["models", "模型用量", weekAll.t ? fmtM(weekAll.t) : ""],
    ["day", "按日明细", ""],
  ];
  const tabHtml = tabs.map(([k,label,tag]) =>
    `<button class="tab ${TAB===k?'active':''}" data-tab="${k}">${label}${tag?`<span class="tag">${tag}</span>`:""}</button>`
  ).join("");

  document.getElementById("app").innerHTML = `
  <div class="hd">
    <h1>gt-wb-gateway 状态看板</h1>
    ${healthPill} ${wbPill}
    <span class="refresh">数据时间 ${esc(d.generatedAt)} · 每 30s 自动刷新</span>
  </div>
  <div class="tabs">${tabHtml}</div>
  <div class="pane active" id="pane">${
    TAB==="overview" ? paneOverview(d) : TAB==="models" ? paneModels(d) : paneDayShell()
  }</div>`;

  document.querySelectorAll(".tab").forEach(el => {
    el.onclick = () => { TAB = el.dataset.tab; render(DATA); };
  });
  bindDayControls();
}

function paneDayShell() {
  const days = (DATA && DATA.wb && DATA.wb.days_all) || [];
  const quicks = days.slice(-6).reverse().map(x =>
    `<button class="quick" data-date="${x}">${x}</button>`).join("");
  const today = (DATA && DATA.today) || new Date().toISOString().slice(0,10);
  return `
  <div class="sec">
    <h2>选择日期 · 查看整日消耗</h2>
    <div class="datebar">
      <input type="date" id="dayinput" value="${esc(DAY ? (DAY.date||today) : today)}" max="${esc(today)}">
      <button class="btn" id="daygo">查询</button>
      ${quicks ? `<span class="mut" style="font-size:12.5px;margin-left:6px">快捷：</span>${quicks}` : ""}
    </div>
    <div class="mut" style="font-size:12px">数据可用日期：${days.length?esc(days.join(" · ")):"—"}</div>
  </div>
  <div id="daybody">${paneDay(DATA)}</div>`;
}

function bindDayControls() {
  const go = document.getElementById("daygo");
  const inp = document.getElementById("dayinput");
  if (go) go.onclick = () => loadDay(inp.value);
  if (inp) inp.onkeydown = e => { if (e.key === "Enter") loadDay(inp.value); };
  document.querySelectorAll(".quick").forEach(b => {
    b.onclick = () => { if (inp) inp.value = b.dataset.date; loadDay(b.dataset.date); };
  });
}

async function loadDay(date) {
  if (!date) return;
  const body = document.getElementById("daybody");
  if (body) body.innerHTML = `<div class="empty">查询 ${esc(date)} …</div>`;
  try {
    const r = await fetch("/api/day?date=" + encodeURIComponent(date) + "&t=" + Date.now());
    DAY = await r.json();
    if (DAY.error) DAY = { error: DAY.error, date };
  } catch (e) {
    DAY = { error: "查询失败：" + e.message, date };
  }
  if (body) body.innerHTML = paneDay(DATA);
}

async function load() {
  try {
    const r = await fetch("/api/data?t=" + Date.now());
    render(await r.json());
  } catch (e) {
    document.getElementById("app").innerHTML = `<div class="empty">数据加载失败：${esc(e.message)}</div>`;
  }
}
load();
setInterval(load, 30000);
</script>
</body>
</html>
"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/data"):
            try:
                self._send(200, json.dumps(build_data(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)[:300]}, ensure_ascii=False))
        elif self.path.startswith("/api/day"):
            try:
                q = parse_qs(urlparse(self.path).query)
                date = (q.get("date") or [""])[0]
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    self._send(400, json.dumps({"error": "date 需为 YYYY-MM-DD"}, ensure_ascii=False))
                else:
                    self._send(200, json.dumps(build_day(date), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)[:300]}, ensure_ascii=False))
        elif self.path.startswith("/health"):
            self._send(200, json.dumps({"ok": True, "service": "gateway-dashboard"}))
        else:
            self._send(200, PAGE, "text/html; charset=utf-8")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    # 后台预热：首次全量扫 traces 约 6~8 秒，放到线程里先跑，
    # 避免第一个 /api/data 请求阻塞（缓存命中后单次仅几十毫秒）。
    def _warm():
        try:
            if WbTraceIndex is not None:
                _wb_index().summary()
        except Exception:
            pass
    import threading
    threading.Thread(target=_warm, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"dashboard on http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
