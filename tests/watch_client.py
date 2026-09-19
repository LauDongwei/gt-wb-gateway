"""单客户端流量监控：一次性报告 + 循环采样。

按来源 IP 聚合某个客户端的用量（请求数 / 成功率 / 缓存命中 / 延迟 / token），
也可不带 --ip 看全部客户端。多机共享时用来回答「那台机器今天跑得怎么样」。

用法:
  .venv/Scripts/python.exe tests/watch_client.py                  # 全部客户端
  .venv/Scripts/python.exe tests/watch_client.py --ip <客户端IP>   # 只看某台
  .venv/Scripts/python.exe tests/watch_client.py --ip <IP> --interval 120   # 循环采样

只读 usage-stats.jsonl，不碰主链路。
"""
import argparse
import json
import os
import sys
import time
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

USAGE = "usage-stats.jsonl"
LOG = "gtwb.log"
WATCHLOG = "_t/mac-watch.log"


def load(ip_filter=None):
    recs = []
    try:
        with open(USAGE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if ip_filter and r.get("client_ip") != ip_filter:
                    continue
                recs.append(r)
    except FileNotFoundError:
        pass
    return recs


def summarize(recs, label):
    if not recs:
        return f"{label}: 暂无记录"
    n = len(recs)
    ok = sum(1 for r in recs if r.get("status") == 200)
    bad = [r for r in recs if r.get("status") != 200]
    esc = sum(1 for r in recs if r.get("escalated"))
    filt = sum(1 for r in recs if r.get("filtered"))
    pt = sum(r.get("prompt_tokens") or 0 for r in recs)
    ct = sum(r.get("completion_tokens") or 0 for r in recs)
    cached = sum(r.get("cached_tokens") or 0 for r in recs)
    rt = sum(r.get("reasoning_tokens") or 0 for r in recs)
    lat = [r.get("elapsed") for r in recs if r.get("elapsed")]
    ttfb = [r.get("ttfb") for r in recs if r.get("ttfb")]
    fin = collections.Counter(r.get("finish") for r in recs)
    tools = collections.Counter(r.get("model") for r in recs)

    first, last = recs[0].get("ts", "?"), recs[-1].get("ts", "?")
    span = ""
    try:
        t0 = time.mktime(time.strptime(first, "%Y-%m-%d %H:%M:%S"))
        t1 = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
        span = f"{max(t1-t0,0)/60:.1f} 分钟"
    except Exception:
        pass

    out = []
    out.append(f"──── {label} ────")
    out.append(f"  窗口      : {first}  →  {last}   (跨度 {span})")
    out.append(f"  请求      : {n}  成功 {ok}  失败 {len(bad)}  降级 {esc}  审核拦截 {filt}")
    out.append(f"  成功率    : {ok/n*100:.1f}%   |   finish 分布 {dict(fin)}")
    out.append(f"  模型      : {dict(tools)}")
    if pt:
        out.append(f"  tokens    : 输入 {pt:,}  输出 {ct:,}  合计 {pt+ct:,}")
        out.append(f"  缓存命中  : {cached:,}  →  {cached/pt*100:.1f}% (输入侧)")
        out.append(f"  推理token : {rt:,}")
        if cached:
            out.append(f"  未命中输入: {pt-cached:,}  ← 真正计费的部分")
    if lat:
        line = f"  延迟      : 平均 {sum(lat)/len(lat):.1f}s  最大 {max(lat):.1f}s"
        if ttfb:
            line += f"   |  ttfb 平均 {sum(ttfb)/len(ttfb):.1f}s"
        out.append(line)
    # 上下文增长
    pts = [r.get("prompt_tokens") or 0 for r in recs]
    if pts and max(pts) > 0:
        out.append(f"  上下文    : 首轮 {pts[0]:,}  →  末轮 {pts[-1]:,}  (峰值 {max(pts):,})")
    if bad:
        out.append(f"  ⚠ 失败明细（最后 5 条）:")
        for r in bad[-5:]:
            out.append(f"      {r.get('ts')} rid={r.get('rid')} status={r.get('status')} "
                       f"finish={r.get('finish')} model={r.get('model')}")
    return "\n".join(out)


def count_warnings():
    """统计日志里的转换层警告（这些行不带 rid，用数量趋势判断噪音）。"""
    c = collections.Counter()
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "⚠" not in line:
                    continue
                if "drop non-function tool" in line:
                    c["drop_web_search"] += 1
                elif "skip tool" in line:
                    c["skip_namespace_tool"] += 1
                else:
                    c["other"] += 1
    except FileNotFoundError:
        pass
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.191.11", help="关注的客户端 IP")
    ap.add_argument("--interval", type=int, default=0, help=">0 则循环采样")
    ap.add_argument("--rounds", type=int, default=0, help="循环多少轮后退出（0=不限）")
    args = ap.parse_args()

    if args.interval <= 0:
        recs = load(args.ip)
        print(summarize(recs, f"客户端 {args.ip}"))
        print()
        print(summarize(load(None), "全部客户端"))
        print()
        w = count_warnings()
        print(f"── 转换层警告累计 ──  web_search 丢弃 {w['drop_web_search']}  "
              f"namespace 工具跳过 {w['skip_namespace_tool']}  其他 {w['other']}")
        return

    rnd = 0
    prev = 0
    while True:
        rnd += 1
        recs = load(args.ip)
        n = len(recs)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        block = [f"\n===== 采样 #{rnd}  {stamp} =====",
                 summarize(recs, f"客户端 {args.ip}")]
        if n > prev:
            block.append(f"  本轮新增 {n-prev} 条请求")
        prev = n
        text = "\n".join(block)
        print(text, flush=True)
        try:
            os.makedirs("_t", exist_ok=True)
            with open(WATCHLOG, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception as e:
            print(f"[watch] 写日志失败: {e}", file=sys.stderr)
        if args.rounds and rnd >= args.rounds:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
