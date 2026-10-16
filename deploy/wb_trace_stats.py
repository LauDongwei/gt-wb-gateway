# -*- coding: utf-8 -*-
"""WorkBuddy 桌面客户端「自身」的 token 用量聚合器（只读）。

背景
----
网关账本 `usage-stats.jsonl` 只记录**经本网关转发**的调用（Codex / Claude Code /
curl 等第三方客户端）。而 WorkBuddy 桌面客户端**自己**调用同一批模型时并不经过网关，
这部分消耗此前完全不可见 —— 于是「某个模型的整体用量」永远算不全。

好在客户端把每次模型调用都落成了本地追踪文件：

    <用户目录>/.workbuddy/traces/<workerPid>/trace_*.json    ← 旧格式（2026-10 前）
    <用户目录>/.workbuddy/traces/<workerPid>/trace_*.jsonl   ← 新格式（客户端更新后）

**两种格式都要读**（2026-10-16 踩坑：客户端静默把 `.json` 换成 `.jsonl` 事件流，
只 glob 旧后缀的聚合器会**整段漏读**，看板「今日」恒为 0 且无从报错）：

| | 旧 `.json` | 新 `.jsonl` |
|---|---|---|
| 结构 | 单个 JSON 对象，含 `trace` + `spans[]` | **事件流**，每行一个 JSON |
| 事件 | — | `trace_start` / `span_start` / `span_end` / `trace_end` |
| 用量位置 | `spans[]` 里 `type=="generation"` 的 span | `span_end` 事件里 `span.type=="generation"` |
| 写入方式 | 写完即终态 | **追加写**，运行中 size 持续增长 |
| `toolOutput` | JSON 数组字符串，`[0]` 是 OpenAI 响应体 | 同左 |

`usage{prompt_tokens, completion_tokens, total_tokens,
prompt_tokens_details.cached_tokens, completion_tokens_details.reasoning_tokens}`。
`startedAt` 是 **UTC**，聚合时统一转本地时区（否则北京时间早 8 点前的调用
会被归到前一天，看板「今日」整体错位）。

本模块把这些文件聚合为「按日 × 按模型」的 token 账，供状态看板与网关口径相加。

用法
----
    from wb_trace_stats import WbTraceIndex
    idx = WbTraceIndex()                 # 自动定位 traces 目录
    idx.daily(7)                         # 最近 7 天按日合计
    idx.models(days=7)                   # 最近 7 天按模型合计
    idx.day("2026-10-08")                # 指定整日明细（含按模型拆分）
    idx.summary()                        # 全量概览（有多少天 / 多少文件）

设计
----
- **不抛异常**：任何单文件解析失败只计数跳过，绝不影响看板。
- **增量缓存**：以 (路径, mtime, size) 为键缓存每个文件解析结果；
  trace 文件落盘后不再改写，因此重复扫描近乎零成本。
- **口径与网关一致**：prompt/completion/total/cached/reasoning 五字段同名同义，
  可直接相加得到「整体用量」。
"""
import json
import os
from datetime import datetime, timedelta, timezone

# 默认 traces 根目录（本机 WorkBuddy 用户目录）
DEFAULT_TRACES_DIR = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "traces"
)

# 单文件解析缓存： key -> {"mtime":..., "size":..., "calls":[...]}
_FILE_CACHE: dict = {}


def _day_hour(iso: str) -> tuple:
    """ISO8601 时间戳 → (本地日期, 本地小时)。

    客户端写的是 **UTC**（形如 `2026-10-16T15:21:12.914Z`）。直接按字符串切
    日期/小时会让北京时间早上 8 点前的调用被归到前一天 —— 看板的「今日」
    会整体错位，且按小时分布整段左偏 8 小时。因此这里统一转本地时区。
    """
    if not iso:
        return "", -1
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:  # 老数据没带时区标记，按 UTC 解
            dt = dt.replace(tzinfo=timezone.utc)
        loc = dt.astimezone()
        return loc.strftime("%Y-%m-%d"), loc.hour
    except Exception:
        return iso[:10], -1


def _usages_from_span(sp: dict) -> list:
    """从单个 generation span 里抽出用量记录（新旧格式通用）。

    `toolOutput` 是一段 JSON 字符串，解析后是**数组**，`[0]` 是标准 OpenAI
    响应体，含 `model` 与 `usage{...}`。
    """
    raw = sp.get("toolOutput")
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list) or not parsed:
        return []
    rec = parsed[0]
    if not isinstance(rec, dict):
        return []
    usage = rec.get("usage")
    if not isinstance(usage, dict):
        return []
    pdt = usage.get("prompt_tokens_details") or {}
    cdt = usage.get("completion_tokens_details") or {}
    if not isinstance(pdt, dict):
        pdt = {}
    if not isinstance(cdt, dict):
        cdt = {}
    day, hour = _day_hour(sp.get("startedAt") or "")
    return [{
        "day": day,
        "hour": hour,
        "model": rec.get("model") or "(unknown)",
        "p": usage.get("prompt_tokens") or 0,
        "c": usage.get("completion_tokens") or 0,
        "t": usage.get("total_tokens") or 0,
        "cached": pdt.get("cached_tokens") or 0,
        "reason": cdt.get("reasoning_tokens") or 0,
    }]


def _extract_usages_json(path: str) -> list:
    """旧格式：整个文件是一个 JSON 对象，含 `trace` 与 `spans[]`。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    trace = data.get("trace") or {}
    if not isinstance(trace, dict):
        trace = {}
    spans = data.get("spans")
    if not isinstance(spans, list):
        return []
    out = []
    for sp in spans:
        if not isinstance(sp, dict) or sp.get("type") != "generation":
            continue
        if not sp.get("startedAt"):  # 老 span 没带时间 → 回退 trace 的
            sp = dict(sp, startedAt=trace.get("startedAt") or "")
        out.extend(_usages_from_span(sp))
    return out


def _extract_usages_jsonl(path: str) -> list:
    """新格式（2026-10 客户端更新）：**事件流**，每行一个 JSON。

    事件类型：`trace_start` / `span_start` / `span_end` / `trace_end`。
    用量只存在于 `span_end` 且 `span.type == "generation"` 的事件里。
    文件是**追加写**的，size 会变，缓存键自然失效，不会读到半个文件。
    """
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                if not isinstance(evt, dict) or evt.get("type") != "span_end":
                    continue
                sp = evt.get("span") or {}
                if isinstance(sp, dict) and sp.get("type") == "generation":
                    out.extend(_usages_from_span(sp))
    except Exception:
        return []
    return out


def _extract_usages(path: str) -> list:
    """从一个 trace 文件里抽出全部 (day, hour, model, usage) 记录。

    返回 [{"day","hour","model","p","c","t","cached","reason"}...]。
    任何异常都吞掉并返回 []（看板不能因一个坏文件而挂）。
    """
    if path.endswith(".jsonl"):
        return _extract_usages_jsonl(path)
    return _extract_usages_json(path)


def _jsonl_parsable(path: str) -> bool:
    """事件流是否至少能解析出合法事件（用于 file_stats 分类）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                json.loads(line)
        return True
    except Exception:
        return False


class WbTraceIndex:
    """扫描 traces 目录并聚合 WorkBuddy 客户端自身的 token 用量。"""

    def __init__(self, traces_dir: str | None = None):
        self.traces_dir = traces_dir or os.environ.get(
            "GTWB_TRACES_DIR", DEFAULT_TRACES_DIR
        )

    # ---- 文件枚举 -------------------------------------------------
    def _list_files(self) -> list:
        files = []
        try:
            for entry in os.scandir(self.traces_dir):
                if not entry.is_dir():
                    continue
                try:
                    for f in os.scandir(entry.path):
                        if f.is_file() and f.name.startswith("trace_") \
                                and (f.name.endswith(".json")
                                     or f.name.endswith(".jsonl")):
                            files.append(f.path)
                except Exception:
                    continue
        except FileNotFoundError:
            return []
        return files

    def _load_all(self) -> list:
        """返回全部调用记录（带文件级缓存，只重解析变化的文件）。"""
        calls = []
        seen = set()
        for path in self._list_files():
            try:
                st = os.stat(path)
            except Exception:
                continue
            key = (path, int(st.st_mtime), st.st_size)
            seen.add(key)
            hit = _FILE_CACHE.get(key)
            if hit is None:
                hit = _extract_usages(path)
                _FILE_CACHE[key] = hit
            calls.extend(hit)
        # 清掉已不存在/已变化的旧缓存项，避免长跑内存膨胀
        if len(_FILE_CACHE) > 4 * max(1, len(seen)):
            for k in list(_FILE_CACHE.keys()):
                if k not in seen:
                    _FILE_CACHE.pop(k, None)
        return calls

    # ---- 聚合 -----------------------------------------------------
    @staticmethod
    def _blank() -> dict:
        return {"p": 0, "c": 0, "t": 0, "cached": 0, "reason": 0, "n": 0}

    @staticmethod
    def _add(acc: dict, rec: dict):
        acc["p"] += rec["p"]
        acc["c"] += rec["c"]
        acc["t"] += rec["t"]
        acc["cached"] += rec["cached"]
        acc["reason"] += rec["reason"]
        acc["n"] += 1

    @staticmethod
    def _finalize(acc: dict) -> dict:
        out = dict(acc)
        out["cache_rate"] = round(acc["cached"] * 100 / acc["p"], 1) if acc["p"] else None
        return out

    def day(self, date: str) -> dict:
        """指定整日（YYYY-MM-DD）的明细：合计 + 按模型拆分 + 按小时分布。"""
        recs = [r for r in self._load_all() if r["day"] == date]
        total = self._blank()
        by_model = {}
        hours = [0] * 24
        for r in recs:
            self._add(total, r)
            m = by_model.setdefault(r["model"], self._blank())
            self._add(m, r)
            if 0 <= r["hour"] < 24:
                hours[r["hour"]] += 1
        return {
            "date": date,
            "total": self._finalize(total),
            "models": [
                {"model": m, **self._finalize(v)}
                for m, v in sorted(by_model.items(), key=lambda x: -x[1]["t"])
            ],
            "hours": hours,
        }

    def daily(self, days: int = 7, end: str | None = None) -> list:
        """最近 N 天按日合计（含 end 当天，默认今天）。"""
        base = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
        recs = self._load_all()
        buckets = {}
        for r in recs:
            b = buckets.setdefault(r["day"], self._blank())
            self._add(b, r)
        out = []
        for i in range(days - 1, -1, -1):
            d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            out.append({"date": d, **self._finalize(buckets.get(d, self._blank()))})
        return out

    def models(self, days: int | None = 7, end: str | None = None) -> list:
        """按模型合计。days=None 表示全量（不限天数）。"""
        recs = self._load_all()
        if days is not None:
            base = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
            lo = (base - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            hi = base.strftime("%Y-%m-%d")
            recs = [r for r in recs if lo <= r["day"] <= hi]
        by_model = {}
        for r in recs:
            m = by_model.setdefault(r["model"], self._blank())
            self._add(m, r)
        return [
            {"model": m, **self._finalize(v)}
            for m, v in sorted(by_model.items(), key=lambda x: -x[1]["t"])
        ]

    def available_days(self) -> list:
        """有数据的日期列表（升序），供前端日期选择器用。"""
        return sorted({r["day"] for r in self._load_all() if r["day"]})

    def summary(self) -> dict:
        recs = self._load_all()
        total = self._blank()
        for r in recs:
            self._add(total, r)
        days = sorted({r["day"] for r in recs if r["day"]})
        return {
            "traces_dir": self.traces_dir,
            "files": len(self._list_files()),
            "calls": total["n"],
            "total": self._finalize(total),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "active_days": len(days),
        }

    # ---- 2026-10-13 看板改版新增（全部只读） --------------------------
    def days_all(self) -> list:
        """全部有数据日期的按日合计（升序），供「逐日双轨对账·全部历史」用。"""
        buckets = {}
        for r in self._load_all():
            b = buckets.setdefault(r["day"], self._blank())
            self._add(b, r)
        return [
            {"date": d, **self._finalize(buckets[d])}
            for d in sorted(buckets) if d
        ]

    def range_stat(self, lo: str, hi: str) -> dict:
        """闭区间 [lo, hi]（YYYY-MM-DD）的合计 + 按模型拆分，供分析页区间口径用。"""
        recs = [r for r in self._load_all() if lo <= r["day"] <= hi]
        total = self._blank()
        by_model = {}
        for r in recs:
            self._add(total, r)
            m = by_model.setdefault(r["model"], self._blank())
            self._add(m, r)
        return {
            "total": self._finalize(total),
            "models": [
                {"model": m, **self._finalize(v)}
                for m, v in sorted(by_model.items(), key=lambda x: -x[1]["t"])
            ],
        }

    def file_stats(self) -> dict:
        """trace 文件构成：总数 / 无 generation 记录（含被中断的生成）/ 坏文件。

        看板要把「为什么调用数比文件数少」讲清楚 —— 被中断的生成本来就没有
        token 可记，不是聚合器丢了数据。
        """
        files = self._list_files()
        no_usage = 0
        bad = 0
        for path in files:
            try:
                st = os.stat(path)
            except Exception:
                bad += 1
                continue
            key = (path, int(st.st_mtime), int(st.st_size))
            hit = _FILE_CACHE.get(key)
            if hit is None:
                hit = _extract_usages(path)
                _FILE_CACHE[key] = hit
            if hit:
                continue
            # 再分一次类：能解析但无 generation → 无用量；解析不了 → 坏文件
            if path.endswith(".jsonl"):
                if _jsonl_parsable(path):
                    no_usage += 1
                else:
                    bad += 1
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    json.load(f)
                no_usage += 1
            except Exception:
                bad += 1
        return {"files": len(files), "no_usage": no_usage, "bad": bad}

    def roots(self) -> list:
        """traces 根目录列表（当前单实例，留列表形态供将有多实例时直接扩展）。"""
        return [self.traces_dir]
