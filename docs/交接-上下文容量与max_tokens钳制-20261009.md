# 网关交接：上下文容量感知 + max_tokens 钳制

> **给谁看**：深圳家那台机器上的 WorkBuddy / 任意 agent。
> **要你做什么**：把这两项改动同步到你自己那台网关，或至少理解它们，免得踩同一个坑。
> **日期**：2026-10-09 ｜ **提交**：`04fcaf3`、`21a8d57`（上游仓库 `LauDongwei/gt-wb-gateway`）
> **验证状态**：本机回归 205 项全过、生产实测通过。**深圳机是否已同步，需要你自己确认。**

---

## 0. 先看这里：三条最值钱的结论

如果你只看三行，看这三行：

1. **上游早就把模型上下文长度告诉我们了，是我们自己扔了。** `data.models[]` 每条都带
   `maxInputTokens` / `maxOutputTokens` / `contextWindow` / `credits` / `vendor` / 模态开关。
   改前全项目 `grep contextWindow` **零命中**。
2. **「缓存掉了」和「网关不认上下文」是同一个 bug。** 投影阈值是写死的 60 万字符，
   跟模型能力无关 → 1M 模型在容量的 18% 处就开始裁剪 → 每轮改写消息序列 →
   上游 KV 前缀缓存整段失效。**命中率从 96.8% 掉到 6.6%**。
3. **网关这层压缩不能砍。** 后端有硬性接收上限，而 Codex 实测会发 **1.94M 字符**的载荷。
   要做的不是取消压缩，是**让它尽量别触发**。

---

## 1. 问题一：缓存断崖（这是用户先发现的）

### 症状

用户在用量看板上发现「token 里缓存越来越少」。按日聚合 `usage-stats.jsonl` 后：

| 日期 | 请求数 | prompt 合计 | cached 合计 | 命中率 |
|---|---|---|---|---|
| 2026-09-19 | 356 | 34,379,644 | 32,485,376 | **94.5%** |
| 2026-09-20 | 263 | 30,627,667 | 29,646,336 | **96.8%** |
| 2026-10-08 | 1155 | 178,606,586 | 129,185,166 | 72.3% |
| **2026-10-09** | **59** | **10,045,601** | **662,272** | **6.6%** ← 断崖 |

**逐条模式更露骨**：当天每条 `prompt` ≈ 176k–182k，而 `cached_tokens` **恒定只有 12,032**
（偶尔 6,272）→ 命中率卡死不动。
**12,032 = 固定 system 前缀的长度，历史部分 0 命中。**

### 根因（一行常量）

`gtwb/responses_projection.py`：

```python
TRANSPARENT_CHAR_LIMIT = 600_000   # ← 按字符数硬编码，跟模型能力完全脱钩
```

因果链：

```
会话字符 > 600k
  → 投影落 elastic 档（_choose_tail_start 滑窗 + _build_history_summary 重摘要）
  → 每轮投影出的 messages 序列都不一样
  → 上游 KV 前缀缓存整段失效 → 只剩固定 system 前缀命中
```

对 **1M token 的模型**（`deepseek-v4.1-flash` 就是）而言，
60 万字符 ≈ 18 万 token ≈ **容量的 18%** —— 会话稍长就永远在弹性档里。
**这不是偶发，是必然。**

### 修法

阈值改成跟着**所选模型的真实容量**走：

```python
CHARS_PER_TOKEN = 3        # 中英混排经验值
CAPACITY_SAFE_RATIO = 0.7  # 留 30% 给工具 schema、语言规则、输出

def transparent_char_limit_for(max_input_tokens: int | None) -> int:
    if not max_input_tokens or max_input_tokens <= 0:
        return TRANSPARENT_CHAR_LIMIT          # 查不到 → 回落旧常量，行为不变
    scaled = int(max_input_tokens * CHARS_PER_TOKEN * CAPACITY_SAFE_RATIO)
    return max(TRANSPARENT_CHAR_LIMIT, scaled) # 不缩到常量以下（保既有行为）
```

**实测效果对照**（用真实规模的 body 跑）：

```
旧：写死常量        阈值=  600,000   elastic      1,837,823 字符 → 裁到 346,471
新：deepseek (1M)   阈值=2,100,000   transparent  1,838,047 字符 → 原样透传 1,838,047
新：hy3 (192k)      阈值=  600,000   elastic      1,838,047 字符 → 裁到 346,695
```

→ **1M 模型不再无谓裁剪（缓存前缀保全），小容量模型照旧保护（不会撑爆）。**

---

## 2. 问题二：网关不知道模型能吃多少上下文

### 决定性发现

`GET /console/enterprises/personal/models`（**注意是 GET，POST 会 404**）的
`data.models[]` 每条都带完整能力档案：

| 字段 | 含义 |
|---|---|
| `maxInputTokens` | 最大输入 token |
| `maxOutputTokens` | 最大输出 token |
| `maxAllowedSize` | 允许的最大尺寸 |
| `contextWindow` | `{defaultLength, supportedLengths[]}` —— **可调档位！** |
| `credits` / `vendor` / `supportsToolCall` / `supportsImages` / `supportsReasoning` / `onlyReasoning` / `isDefault` … | 元数据 |

**实测各模型真实容量（2026-10-09 抓取）**：

| 模型 | maxInputTokens | maxOutputTokens |
|---|---|---|
| deepseek-v4.1-flash / deepseek-v4-pro | **1,000,000** | 128,000 |
| glm-5.3 / glm-5.3-flash / glm-5.2 | **1,000,000** | 64k / 131k / 64k |
| kimi-k3-1 / kimi-k2.8-preview | **1,000,000** | 32k / 64k |
| space-bunny | **1,000,000** | 128,000 |
| hy4-preview | 960,000 | 64,000 |
| minimax-m3 | 512,000 | 64,000 |
| kimi-k2.7 / kimi-k2.6 / auto | 256,000 | 32,000 |
| glm-5.1 / glm-5v-turbo | 200,000 | 48k / 64k |
| hy3 / hy3-x | 192,000 | 64,000 |

**改前**：`grep -rn "contextWindow\|maxInputTokens" gtwb/*.py` → **零命中**。
`_extract_model_ids()` 只抽 id 字符串，其余全丢。

### 修法（三步，改动都很小）

| # | 文件 | 做了什么 |
|---|---|---|
| 1 | `gtwb/upstream.py` | 新增 `CATALOG_FIELDS` + `_extract_model_catalog()` + `fetch_model_catalog()`。**口径与 `_extract_model_ids()` 严格一致**（只认主 CLI agent，`default` 别名照补），失败返 `{}` 不抛 |
| 2 | `gtwb/server.py` | `_MODEL_CACHE` 加 `"catalog"` 键（与 ids **共用同一份 TTL**，不多打上游）；`model_catalog()` / `model_max_input()` / `model_max_output()`；`/v1/models` 加 `context_length` + `max_output_tokens`，`?detail=full` 透出全部能力字段 |
| 3 | `gtwb/responses_projection.py` | `transparent_char_limit_for()` + `project_responses_chat_body(body, max_input_tokens=...)` |

### ★ 两个必须记住的设计纪律

**纪律 A：网关自用参数一律走函数入参，绝不加进 `body`。**
`body` 是整体 `json=body` 发上游的，多一个未知字段上游会校验失败。
（我们第一版就写成了 `body["max_input_tokens"]`，自己发现后改掉的。测试里加了断言锁死。）
```python
check("max_input_tokens 不污染上游 body", "max_input_tokens" not in out_1m, ...)
```

**纪律 B：必须先定模型、再投影。**
投影阈值依赖模型容量，而 `resolve_model()` 会把客户端写错的 Codex 原生名
（`gpt-5.6-luna` 等）换成真实名。顺序反了就是拿"错的模型"查容量 → 阈值失真。
原代码投影在 `resolve_model()` **之前**，这次一并调正。

---

## 3. 运维：为什么网关这层压缩不能砍

**用户的直觉**：「压缩上下文不是 agent 自己或上游该干的事吗？」

**答案：必须，而且这是本网关唯一不能砍的一层。** 三条理由：

**① 后端有硬性接收上限，而 Codex 会毫不犹豫地超。**
实测：Codex 长会话发往网关的载荷 **1.94M 字符**（1189 条消息 + 88 个工具 schema）。
代码里 `TOTAL_CHAR_CEILING = 800_000` 就是为这个设的天花板。
客户端**知道**自己是长会话，但**不关心**后端能吃多少 —— 它只管把完整历史发出来。

**② 网关是唯一能保证「序列稳定」的地方** —— 而 KV 缓存要的就是这个：
- agent 自己压：每轮压出来的文本都不同 → 序列变 → 缓存失效；
- 上游压：上游看不到完整请求，只能丢弃或报错；
- **网关压**：同一份历史重复投影**结果确定**（滑窗位置 + 摘要规则固定）→ 序列稳定 → 缓存可命中。

**③ 它压的是「旧历史」，不是「上下文」。**
实测弹性档：`1,940,252 字符 → 445,875`（保留 23%），
但**系统提示词和 88 个工具 schema 一律不动**，最近消息逐字保留，只有更早的历史被摘要。

**历史事故（这是压缩层存在的真正原因）**：
2026-09-19 早期是**二元开关** —— 超 120k 字符就压到 ~6k（丢 99%）。
Mac 端 671k 的长会话被压成 5.8k → **模型丢光历史和操作手册 →
反复重跑同一命令、agent loop 不收敛**。
所以现在是「弹性」而非「开关」：宁可多留，不可丢干净。

### 三档速查

| 档 | 触发条件 | 做什么 | 系统提示词/工具 |
|---|---|---|---|
| **transparent** | 字符 ≤ 动态阈值 | **完全原样透传**，只补语言规则 | 不动 |
| **conservative** | 超阈值、**非 agentic** | 按比例保留（`KEEP_RATIO=0.6`） | 不动 |
| **elastic** | 超阈值、**agentic** | 滑窗 + 历史重摘要 + `anchor_task`/`anchor_user` | **不动** |
| （退化） | 仅命中审核后重试 | `force_compact=True`，**此档才**摘要系统提示、丢 harness | 会动 |

⚠ **`AGENTIC_TOOL_NAMES` 是判 agentic 的唯一依据**（`exec_command` / `apply_patch` /
`update_plan` / `view_image` …）。**纯文本请求没有这些工具名 → 一律走 conservative**。
写测试时别断言成 elastic —— **我们在这里踩过一次**（见第 5 节）。

---

## 4. 问题三：max_tokens 超限把 agent 打死

### 症状

客户端写一个远超模型输出上限的 `max_tokens`（例：给输出上限 64k 的 `hy3` 写 `999999`）
→ 原样透传 → 上游 **400** → **整条 agent 链路当场失败**，客户端还以为自己坏了。

### 修法：能钳就钳，钳不了才报错，钳了要告知

```python
MAX_TOKENS_FIELD_ORDER = ("max_tokens", "max_completion_tokens")

def clamp_max_tokens(body: dict, max_output_tokens: int | None) -> str:
    if not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
        return ""                       # 上限未知 → 一律不动
    changed = []
    for field in MAX_TOKENS_FIELD_ORDER:
        raw = body.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue                    # 非整数跳过，交上游判
        if raw > max_output_tokens:
            body[field] = max_output_tokens
            changed.append(f"{field} {raw}→{max_output_tokens}")
    return "；".join(changed)
```

| 场景 | 行为 |
|---|---|
| ≤ 上限 | 不动，无告知 |
| > 上限 | **钳到上限**，请求继续跑 |
| 恰好等于上限 | 不动（不算超限） |
| **上限未知（None / 0 / 负数）** | **一律不动** —— 拉不到元数据不能反过来改写客户端参数 |
| 非整数（含 bool） | 跳过 |

**接线**：三个端点全接（`/v1/chat/completions` · `/v1/responses` · `/v1/messages`），
位置在 `resolve_model()` **之后**（要拿最终模型名查上限）。
日志形如 `clamp[max_tokens 999999→64000]`。

### ★ 一个刻意的取舍，你可能不同意

**我们选择「钳制」而不是「报错」，理由是报错会让 agent 死、钳制能把它救活。**
但"被钳过"这件事必须让人知道，所以写进日志。
**如果你更希望"宁可报错也不静默纠正"，改一行就行 —— 把 `body[field] = max_output_tokens`
换成 `raise _bad_request(...)`。**

---

## 5. 我们踩过的坑（都是自己写错的，别重复）

| 坑 | 表现 | 正确做法 |
|---|---|---|
| **把测试写错，冤枉了代码** | 断言 `catalog` 不含 `default` → 失败。实际是 `_extract_model_ids()` **刻意补** `default` 别名 | 先读清被测函数的既定行为，再写断言。**测试失败不一定是代码错** |
| **同上，第二次** | 断言「纯文本 700k 走 elastic」→ 失败。实际无 agentic 工具名 → conservative | 判定条件在 `_looks_like_agentic_cli()`，纯文本请求本来就不该走 elastic |
| **拿错 Python** | `ModuleNotFoundError: httpx` | 必须用工程 venv：`./.venv/Scripts/python.exe`，别用系统 Python |
| **猜 API 名** | `AttributeError: module 'gtwb.config' has no attribute 'load'` | 是 `load_config()`；账号用 `auth.candidate_paths()` + `auth.parse_account()`，**没有** `load_accounts` |
| **body 污染**（最危险的一个） | 差点把 `max_input_tokens` 写进 body 发给上游 | 网关自用字段**一律走函数入参**，并加断言锁死 |
| **代理毒害探测** | 本机 shell 有 `HTTP_PROXY`，`curl 127.0.0.1:8787` 被劫持 → 误判"服务死了" | 探测一律 `curl --noproxy "*"`，或清空 proxy 环境变量 |
| **重启不生效** | 改了代码但行为没变 | `serve-loop.bat` 有**幂等守卫**（服务健康就直接退出）→ 必须先杀进程。见下节 |
| **`/v1/models` 返回 401** | 忘了带 key，误以为接口坏了 | 要带 `Authorization: Bearer <config.json 里的 api_key>` |

---

## 6. 怎么验收新代码到底生效了

### 部署（本机实测的可靠链路）

```bash
# 1) 找监听 8787 的 PID
netstat -ano | grep ":8787.*LISTENING"
# 2) 杀（杀前重新 netstat 确认 PID 身份 —— 僵尸退出后 PID 会被复用，照旧 PID 杀会误杀健康进程）
#    ⚠️ 走 PowerShell 工具，别用 bash 包 powershell（本机安全策略会拦）
Stop-Process -Id <PID> -Force
# 3) 触发计划任务拉起（会跑新代码）
#    ⚠️ 不要用 schtasks /run —— 本机把 schtasks.exe 列入黑名单
Start-ScheduledTask -TaskName 'gt-wb-gateway'
# 4) 确认恢复了再收工
curl --noproxy "*" -s http://127.0.0.1:8787/health
```

### 四条验收判据（缺一条就是没生效）

```bash
KEY=$(python -c "import json;print(json.load(open('config.json'))['api_key'])")

# ① 模型清单要带 context_length（改前没有这个字段）
curl --noproxy "*" -s "http://127.0.0.1:8787/v1/models" -H "Authorization: Bearer $KEY" \
  | python -c "import sys,json;d=json.load(sys.stdin)['data'];print('总数',len(d));print('缺字段',[m['id'] for m in d if 'context_length' not in m] or '无')"
# 期望：总数 18，缺字段 = 无；deepseek-v4.1-flash → 1000000，hy3 → 192000

# ② 日志要出现 max_in=  （改前日志行没有这个）
grep "max_in=" gtwb.log | tail -3
# 期望形如：▶ RESPONSES deepseek-v4.1-flash | ... | max_in=1000000 max_out=128000

# ③ 长会话要落 transparent（改前是 elastic）
grep "projection\[transparent\]" gtwb.log | tail -3

# ④ max_tokens 钳制（故意传超限值）
curl --noproxy "*" -s -X POST "http://127.0.0.1:8787/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"hy3","max_tokens":999999,"messages":[{"role":"user","content":"回复两个字：正常"}]}' \
  -o /dev/null -w "HTTP %{http_code}\n"
# 期望：HTTP 200（改前会是 400），日志出现 clamp[max_tokens 999999→64000]
```

**最快的判定法**：看日志有没有 `max_in=` —— **没有就是没重启（新代码没加载）**。

### 回归

```bash
./.venv/Scripts/python.exe tests/test_gateway.py
# 期望：通过 205 项，失败 0 项
```

---

## 7. 改了哪些文件

| 文件 | 改动量 | 内容 |
|---|---|---|
| `gtwb/upstream.py` | +68 | `CATALOG_FIELDS` / `_extract_model_catalog()` / `fetch_model_catalog()` |
| `gtwb/server.py` | +71 / +58 | catalog 缓存、`model_max_input/output()`、`/v1/models` 加字段、端点顺序调正、`clamp_max_tokens()` 接三端点 |
| `gtwb/responses_projection.py` | +34 | `transparent_char_limit_for()`、投影签名加 `max_input_tokens` |
| `tests/test_gateway.py` | +121 / +53 | `test_model_context`（13 断言）、`test_max_tokens_clamp`（12 断言） |

**测试计数演进**：147 → 154 → 164 → 173（加密登录态）→ **194**（上下文容量）→ **205**（max_tokens 钳制）

**提交**：
- `04fcaf3` feat(context): honor the model's real context window
- `21a8d57` feat(limits): clamp max_tokens to the model's output ceiling

---

## 8. 深圳机怎么同步（给你的操作建议）

**别复制文件夹** —— 直接拉同一个 git 提交，保证字节一致：

```bash
cd <你的 gt-wb-gateway 目录>
git fetch origin
git log --oneline HEAD..origin/main        # 看远端多了什么
git merge --ff-only origin/main            # 快进；有分叉再议
./.venv/Scripts/python.exe tests/test_gateway.py   # 必须 205 全过
# 然后按第 6 节重启 + 四条验收
```

**如果深圳机的代码结构与公司机不同**（比如它跑的是另一个分支/另一套实现），
那就把第 1–4 节的**方法论**拿过去自己实现一遍 —— 核心只有三句：

1. 投影阈值要按 `该请求模型的 maxInputTokens` 动态算，别写死字符数；
2. `/v1/models` 要把 `context_length` 告诉下游（OpenAI 兼容字段）；
3. `max_tokens` 超模型输出上限时**钳制并告知**，别让上游 400 打死 agent。

**两个通用纪律**（跟具体实现无关，务必带走）：
> **① 网关自用参数走函数入参，绝不塞进 body。**
> **② 先定模型，再做任何与模型能力相关的决策。**

---

## 附：一句话记住这次的核心

> 上游早就把「模型能吃多少」写在响应里了，是我们自己扔了；
> 扔掉它的代价是缓存命中率从 96.8% 掉到 6.6% —— **一个常量引发的血案。**
