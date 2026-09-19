# -*- coding: utf-8 -*-
"""gt-wb-gateway 实时状态看板（只读，独立于网关进程）。

  pythonw deploy/status_server.py            # 默认 0.0.0.0:8735
  python deploy/status_server.py --port 8735 --root D:/workbuddy/研究院/gt-wb-gateway

数据源：
  - usage-stats.jsonl   每请求记账（网关写入）
  - gtwb.log            运行日志（取尾部，统计搜索/降级）
  - http://127.0.0.1:8787/health  网关存活探测
不写任何文件，网关挂了看板照样活着并如实显示"离线"。
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

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

    kpi = {
        "requests": len(today_recs),
        "ok": len(ok),
        "fail": len(today_recs) - len(ok),
        "success_rate": round(len(ok) * 100 / len(today_recs), 1) if today_recs else None,
        "prompt_tokens": pin,
        "completion_tokens": pout,
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

    # ---- 按模型 ----
    models = {}
    for r in today_recs:
        m = r.get("model") or "?"
        models[m] = models.get(m, 0) + 1

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
        "days": days,
        "hours": hours,
        "bad": bad,
        "search": {"announce": search_announce, "exec": search_exec, "ok": search_ok,
                   "final": search_final, "trunc": trunc},
        "recent": recent,
    }


PAGE = """<!DOCTYPE html>
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
    padding:28px clamp(16px,4vw,48px) 48px; }
  .hd { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:22px; }
  .hd h1 { font-size:22px; font-weight:700; letter-spacing:.2px; }
  .pill { padding:4px 12px; border-radius:999px; font-size:12.5px; font-weight:600;
    border:1px solid var(--border); background:var(--card); color:var(--muted); }
  .pill.on { color:var(--green); border-color:rgba(52,211,153,.35); background:rgba(52,211,153,.08); }
  .pill.off { color:var(--red); border-color:rgba(239,68,68,.4); background:rgba(239,68,68,.08); }
  .hd .refresh { margin-left:auto; font-size:12px; color:var(--muted); }
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
  .dayrow .n { width:120px; text-align:right; font-variant-numeric:tabular-nums; }
</style>
</head>
<body>
<div id="app">加载中…</div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtN = n => n == null ? "—" : n.toLocaleString("en-US");
const fmtM = n => n == null ? "—" : n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? (n/1e3).toFixed(1)+"k" : String(n);

function render(d) {
  const g = d.gateway || {};
  const k = d.kpi || {};
  const healthPill = g.ok
    ? `<span class="pill on">● 网关在线 · 8787</span>`
    : `<span class="pill off">● 网关离线</span>`;
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
    `<span class="chip">${esc(m)} · ${n}</span>`).join("") || `<span class="mut">—</span>`;

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

  const dayMax = Math.max(1, ...(d.days||[]).map(x=>x.requests));
  const dayRows = (d.days||[]).map(x => `
    <div class="dayrow">
      <span class="d">${esc(x.date.slice(5))}</span>
      <div class="track"><div class="fill" style="width:${x.requests/dayMax*100}%"></div></div>
      <span class="n">${x.requests} 次 · 缓存 ${x.cache==null?"—":x.cache+"%"}</span>
    </div>`).join("");

  const escPill = k.escalated ? `<span class="pill warn">降级 ${k.escalated}</span>` : "";
  const filtPill = k.filtered ? `<span class="pill warn">审核 ${k.filtered}</span>` : "";

  document.getElementById("app").innerHTML = `
  <div class="hd">
    <h1>gt-wb-gateway 状态看板</h1>
    ${healthPill} ${escPill} ${filtPill}
    <span class="refresh">数据时间 ${esc(d.generatedAt)} · 每 30s 自动刷新</span>
  </div>

  <div class="grid">
    <div class="kpi"><div class="lb">今日请求</div><div class="v b">${k.requests??0}</div>
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
</html>"""


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
        elif self.path.startswith("/health"):
            self._send(200, json.dumps({"ok": True, "service": "gateway-dashboard"}))
        else:
            self._send(200, PAGE, "text/html; charset=utf-8")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"dashboard on http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
