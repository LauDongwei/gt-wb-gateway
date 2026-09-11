# 在 CC Switch 里接入 gt-wb-gateway

> 面向本机（这台常开的机器）。家里那台机器请看 `双机共享-家里电脑.md`。

## 先搞清楚一件事：为什么不能直接改 `config.toml`

CC Switch 管 Codex 用的是 **「Live 接管」**：

1. 切换供应商时，它把该供应商的配置**整段写进** `~/.codex/config.toml`；
2. 本地代理开启时，还会把 `base_url` 改写成 `http://127.0.0.1:15721/v1`，并塞一行
   `experimental_bearer_token = "PROXY_MANAGED"`；
3. 停止代理时，用一份「Live 备份」把原配置还原。

本机实测证据（`~/.cc-switch/cc-switch.db` 的 `providers` 表 vs 磁盘）：

| 位置 | `lssh007` 供应商的地址 |
|---|---|
| db 里的供应商定义 | `https://lsshlink.com/v1` |
| 磁盘上的 `~/.codex/config.toml` | `http://127.0.0.1:15721/v1` |

地址被改写成了 15721 —— 说明**磁盘上的 config.toml 是 CC Switch 生成的产物**。
所以手动 append 一个 `[profiles.gtwb]`，在下次切换供应商时会被**整段覆盖**。

正确做法：**让 gtwb 变成 CC Switch 里的一个正式供应商**，由它自己写配置。

---

## 路线 A：一键导入（最快，推荐）

```powershell
cd D:\workbuddy\研究院\gt-wb-gateway
.venv\Scripts\python.exe deploy\make-deeplink.py --open
```

会唤起 CC Switch 的导入确认框，点确认即可。它做的是：
读 `config.json` 里的密钥 → 拼出一个 `ccswitch://v1/import?...` 深链 → 交给 CC Switch。

导入后在主界面能看到一个叫 **WorkBuddy GT Gateway** 的 Codex 供应商，点「启用」就切过去了。

> 深链里含你的 API Key。别把这条链接转发到群里。

---

## 路线 B：GUI 手动添加

1. **添加供应商** → 选 **Codex** → **自定义配置**
2. 填：

| 字段 | 值 |
|---|---|
| 名称 | `WorkBuddy GT Gateway` |
| API 地址 / endpoint | `http://127.0.0.1:8787/v1` |
| API Key | 从 `config.json` 的 `api_key` 字段复制 |

3. `config.toml` 内容填：

```toml
model_provider = "custom"
model = "glm-5.3"
model_reasoning_effort = "high"
disable_response_storage = true

[model_providers.custom]
name = "WorkBuddy GT Gateway"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
requires_openai_auth = true
```

---

## 路线 C：不用 CC Switch 管（保守派）

关掉 CC Switch 对 Codex 的代理接管，然后手动往 `~/.codex/config.toml` 追加：

```toml
[model_providers.gtwb]
name = "WorkBuddy via gt-wb-gateway"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "GTWB_KEY"

[profiles.gtwb]
model = "glm-5.3"
model_provider = "gtwb"
```

```bash
export GTWB_KEY=<你的 key>
codex --profile gtwb "任务"
```

代价：失去 CC Switch 的一键切换和故障转移。

---

## 三个必须知道的点

### 1. 协议判定要落在 `responses` 上

CC Switch 判断上游说什么协议的顺序（源码 `proxy/providers/codex.rs`）：

```
meta.apiFormat  →  settings.api_format  →  settings.apiFormat  →  TOML 里的 wire_api  →  按 base_url 猜
```

取值只有三种：`anthropic` / `openai_chat` / `openai_responses`。

- 走**路线 A / B**：TOML 里写了 `wire_api = "responses"`，会被正确识别为原生 Responses，**原样透传** `/v1/responses`。
- 若被当成 `openai_chat`：CC Switch 会转成 `/v1/chat/completions` 再发。gtwb 也支持这条路径，但 Codex 的推理流还原度会打折。
- 想彻底钉死：在供应商编辑里把 `apiFormat` 设为 `openai_responses`。

### 2. 链路会多一跳

```
Codex  →  127.0.0.1:15721 (CC Switch 代理)  →  127.0.0.1:8787 (gtwb)  →  copilot.tencent.com
```

能正常工作，但 **CC Switch 不开，Codex 就全断**。这也是「家里那台机器不要走 CC Switch 代理」的原因：
15721 只监听回环，外面进不来。

### 3. 可以挂故障转移

CC Switch 支持 failover 队列。把 gtwb 加进队列后，当前供应商连续失败会自动切到它。
对 Codex 这种长任务算是个不错的兜底：`Lssh 0.07` 挂了 → 自动落到 gtwb。

---

## 切回原来的供应商

系统托盘点一下 `Lssh 0.07` 就回去了，无需重启（Codex 需要重启终端）。

---

## 自检

```powershell
# 网关活着吗
curl http://127.0.0.1:8787/health

# 它到底暴露了哪些模型（以真实后端为准，不要凭记忆填模型名）
curl -H "Authorization: Bearer <你的key>" http://127.0.0.1:8787/v1/models
```
