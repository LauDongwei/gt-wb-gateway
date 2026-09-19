# -*- coding: utf-8 -*-
"""对生产网关做一次强制 web_search 探针，验证 ⌕→✓→最终答案 的完整链路。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8787"
key = json.load(open("config.json", encoding="utf-8")).get("api_key") or ""

body = {
    "model": "glm-5.3-flash",
    "stream": True,
    "input": [
        {"role": "user", "content":
            "Use the web_search tool to find the latest stable version of "
            "OpenAI Codex CLI, then report the version number and its release "
            "date in one sentence. You must call web_search."}
    ],
    "tools": [{"type": "web_search", "external_web_access": True}],
    "max_output_tokens": 3000,
}

req = urllib.request.Request(
    BASE + "/v1/responses",
    data=json.dumps(body).encode("utf-8"),
    headers={"Authorization": "Bearer " + key,
             "Content-Type": "application/json"})

events = []
with urllib.request.urlopen(req, timeout=180) as resp:
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("event:"):
            events.append(line[6:].strip())

from collections import Counter
c = Counter(events)
print("事件统计:", dict(c))
print("function_call 事件数（应为 0，内部工具不外发）:", c.get("response.output_item.done", 0))
sys.exit(0)
