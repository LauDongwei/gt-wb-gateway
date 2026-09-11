<div align="center">

# gt-wb-gateway

**把 WorkBuddy / CodeBuddy 桌面端订阅，变成你自己的本地 OpenAI / Anthropic 兼容 API**

让 Codex CLI、Claude Code、Cherry Studio 等客户端直接复用已订阅的模型
（GLM、DeepSeek、Kimi、Hunyuan、MiniMax 等）

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#二快速开始)

单账号 · 本机运行 · 零外部依赖 · 不需要 Docker

`python -m gtwb` 一行启动

</div>

---

## ✨ 为什么做这个

现成的三个开源项目各缺一块，本项目**取三家之长、补三家之短**：

| 能力 | [codebuddy2openai](https://github.com/ShouZhuo0413/codebuddy2openai) | [workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) | [codebuddy-cli2api](https://github.com/neipor/codebuddy-cli2api) | **本项目** |
|---|:--:|:--:|:--:|:--:|
| `/v1/responses`（Codex CLI 必需） | ✅ | ❌ 没有 | ✅ | ✅ |
| `/v1/chat/completions` | ✅ | ✅ | ✅ | ✅ |
| `/v1/messages`（Claude Code） | ✅ | ❌ 没有 | ✅ | ✅ |
| Codex 请求投影压缩 | ✅ | ❌ | ❌ | ✅ |
| 零宽脱敏绕审核 | ✅ | ❌ | ❌ | ✅ |
| **审核拦截自动重试** | 仅 Responses | ❌ | ❌ | ✅ **三条链路全覆盖** |
| 冷却 / 熔断状态机 | ❌ | ✅ | ❌ | ✅ |
| 结构化请求日志（TTFB / token 速率） | 部分 | ✅ | ❌ | ✅ |
| 动态模型清单 | ❌ 硬编码 | ✅ | 本地文件 | ✅ **在线接口 + 1h 缓存** |
| 跨平台登录态探测 | ✅ | 仅 macOS/Linux | **漏了 Windows** | ✅ 三平台 |
| 多账号池轮转 | ❌ | ✅ | ✅ | ⛔ **主动不做**（见[风险与边界](#%EF%B8%8F-风险与边界务必知悉)） |

顺手修掉的三个真实缺陷：

1. **模型清单过时** —— 原实现硬编码列表停在 `glm-5.2`，实际账号已有 `glm-5.3`、
   `kimi-k3-1`、`hy4-preview`、`deepseek-v4.1-flash`。本项目改为调用后端动态接口。
2. **Windows 登录态探测缺失** —— 原实现漏了 Windows 路径，Windows 用户必须手动设环境变量。
3. **有效期缺失误判** —— auth 文件没有 `expiresAt` 字段时，原实现判为「已过期」，
   导致**每个请求都白刷一次 token**。本项目对「有效期未知」不预判，只在 401 时刷新。

---

## 🚀 快速开始

前置条件只有一个：**本机 WorkBuddy / CodeBuddy 桌面端已登录**。
程序自动发现登录态文件，无需任何配置。

```bash
# 1) 安装依赖（仅 3 个包）
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt

# 2) 启动（Windows 可直接双击 start.bat）
.venv/Scripts/python.exe -m gtwb

# 3) 验证
curl http://127.0.0.1:8787/health      # 账号与依赖状态
curl http://127.0.0.1:8787/v1/models   # 模型清单
curl http://127.0.0.1:8787/status      # 冷却/熔断状态
```

跑自测套件（53 项离线断言，覆盖状态机与登录态解析）：

```bash
.venv/Scripts/python.exe tests/test_gateway.py
```

> 需要长期常驻、开机自启、崩溃自愈？运行 `deploy/setup.ps1`，
> 一键完成：生成 API key → 写配置 → 防火墙规则 → 注册带看门狗的 Windows 计划任务。
> 撤销用 `deploy/uninstall.ps1`。

---

## 🔌 客户端接入

### Codex CLI

**装了 CC Switch 的话，首选走 CC Switch 导入**，不要手改 `~/.codex/config.toml` ——
CC Switch 的「Live 接管」会整段重写它，手写的会被冲掉。一条命令生成导入链接：

```powershell
.venv\Scripts\python.exe deploy\make-deeplink.py --host 127.0.0.1 --open
```

> 验证是否真的导入成功，**要查数据库而不是看进程** —— CC Switch 是常驻托盘程序，
> 进程活着 ≠ 导入成功：
>
> ```bash
> python -c "import sqlite3,os;db=os.path.expandvars(r'%USERPROFILE%\.cc-switch\cc-switch.db');c=sqlite3.connect(db);print([r[0] for r in c.execute(\"select name from providers where app_type='codex'\")])"
> ```
>
> 另注意：CC Switch 会按 `endpointAutoSelect` 从网关**自动挑一个模型**，
> 别假设导入后的模型就是你深链里写的那个。

没装 CC Switch，才手写 `~/.codex/config.toml`（**追加**，别覆盖已有配置）：

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
export GTWB_KEY=any-value      # 服务端没设 api_key 时，随便填一个占位即可
codex --profile gtwb "你的任务"
```

<details>
<summary><b>想隔离验证（不碰现有配置）？这里有三个实测踩过的坑</b></summary>

```powershell
$env:CODEX_HOME = "$env:LOCALAPPDATA\codex-gtwb-test"   # 目录自己准备 config.toml
$env:GTWB_KEY   = "<config.json 里的 api_key>"
codex exec --skip-git-repo-check "Reply with exactly: OK"
```

| 坑 | 表现 | 正确做法 |
|---|---|---|
| **CODEX_HOME 必须从 PowerShell 设** | 在 `.bat` 里 `set CODEX_HOME` 不被采纳，codex 静默回退去读 `~/.codex/config.toml`，「隔离测试」变成在测生产配置 | 用 PowerShell 设，或直接手敲 |
| **CODEX_HOME 别放仓库里** | codex 会在该目录写 `sessions/` 和若干 sqlite 状态库（实测约 3MB） | 放 `%LOCALAPPDATA%` |
| **僵死的 `codex.exe` 会锁死后续调用** | 测试被 Ctrl+C 或外部杀掉后，之后每次 `codex` 都卡在启动阶段：不报错、不发请求、就是不动 | `Get-Process codex \| Stop-Process -Force` 后重试 |

</details>

### Claude Code / 其他客户端

走 Anthropic 的 `/v1/messages`：

```json
{
  "GLM-5.3": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "glm-5.3"
  }
}
```

模型名必须填后端真实支持的 id（用 `/v1/models` 查），不做自动映射。

Cherry Studio / LobeChat / NextChat / Open WebUI 等 OpenAI 兼容客户端：
Base URL 填 `http://127.0.0.1:8787/v1`，API Key 留空（或填启动时设的 key）。

> 想让**另一台电脑**（例如家里的机器）也用上这台机器的模型？
> 见 `docs/双机共享-家里电脑.md`：复用 ZeroTier 私有网络 + 限定来源网段的防火墙规则，
> 不需要云服务器。

---

## 📡 端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|:--:|---|
| POST | `/v1/chat/completions` | ✅ | OpenAI Chat，原生 tools / tool_calls / 流式 |
| POST | `/v1/responses` | ✅ | OpenAI Responses，适配 Codex CLI |
| POST | `/v1/messages` | ✅ | Anthropic Messages，适配 Claude Code |
| POST | `/v1/messages/count_tokens` | ✅ | Anthropic token 计数（粗估，满足客户端前置调用） |
| GET | `/v1/models` | ✅ | 动态模型清单（后端在线接口 + 1h 缓存） |
| GET | `/health` `/healthz` | ❌ | 存活探测；**只返回 `status` + `service`**，详细诊断仅对本机回环开放 |
| GET | `/status` | ✅ | 账号与冷却/熔断状态 |

鉴权：`Authorization: Bearer <api_key>` 或 `X-Api-Key: <api_key>`。
`api_key` 为空时不校验 —— **一旦监听 `0.0.0.0` 就必须设置**，否则等于把账号借给整个网段。

---

## 🛡️ 韧性机制

| 上游返回 | 归类 | 处置 |
|---|---|---|
| 429 / body 含 `11140` | 限流 | 软冷却，按次数指数退避（60s → 120s → … 封顶 2h） |
| 402 / 余额不足 | 额度耗尽 | 硬冷却到次日 04:00，等额度自然恢复 |
| 401 + `12153` / `Offline user session` | 会话失效 | 永久禁用，需重新登录桌面端 |
| 401（其他） | 令牌过期 | **自动强制刷新一次**后重试 |
| 404 | 上游偶发 | 固定 60s 短冷却，**不计入熔断**（防雪崩） |
| 5xx | 上游故障 | 计入连续失败，达阈值触发熔断并指数退避 |
| 网络中断 | 传输层 | 记账但不惩罚（对抖动连坐过于严苛） |

- 状态落盘到 `state.json`，重启后冷却/熔断计时继续生效。
- 单账号在途请求上限默认 3，防止把自己打出限流。
- 客户端接入层的 `developer` 角色自动降级为 `system`（后端不认，会触发风控 11128）。
- 安全红线：`X-Refresh-Token` **只出现在刷新端点**，聊天请求绝不携带。

### 内容审核误拦怎么处理

Codex / Claude Code 的 system prompt 天然带 `sandbox`、`credential`、`escalation`、
`exploit` 这类词，会被后端审核误判。默认开启三层防护：

1. **零宽脱敏** —— 对敏感词插入零宽空格，人眼看不出，审核词匹配失效；
2. **harness 压缩** —— 把冗长的 agent 运行时提示词压成摘要；
3. **命中重试** —— 检测到 `content-filter` 后自动改用紧凑模式重试一次。

想保留完整原始 system prompt 可加 `--no-compact`，代价是误拦概率上升
（此时第 3 层重试会自动生效）。

---

## ⚙️ 配置

复制 `config.example.json` 为 `config.json` 后修改，或用命令行参数 / 环境变量覆盖。
优先级：**默认值 → config.json → 环境变量（`GTWB_*`）→ 命令行**。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | `127.0.0.1` / `8787` | 监听地址与端口 |
| `--api-key` | 空 | 非空则要求客户端带 Bearer |
| `--log PATH` | 空 | 请求日志落盘 |
| `--auth-file` / `--auth-dir` | 自动探测 | 手动指定登录态 |
| `--no-desensitize` | 关 | 关闭脱敏（误拦概率显著上升） |
| `--no-compact` | 关 | 保留完整 system prompt |
| `--verbose` | 关 | 记录完整请求/响应体（排查审核用） |
| `--allow-rotation` | 关 | 开启多账号轮转（**不建议**） |
| `--show-config` | — | 打印最终生效配置后退出 |

---

## ✅ 实测验证记录

以下为对**真实后端**的实测结果，非纸面推演：

| 验证项 | 结果 |
|---|---|
| 登录态自动发现 | ✅ Windows / macOS / Linux 三平台路径全覆盖 |
| 动态模型清单 | ✅ 实测 30 个模型在线返回 |
| `/v1/chat/completions` 非流式 + 工具调用 | ✅ 200，`finish=tool_calls`，参数正确回传 |
| `/v1/responses` 非流式 + 流式 | ✅ Codex 事件序列完整（created → in_progress → delta → done） |
| `/v1/messages` 非流式 + 流式 | ✅ `content_block_start` / `text_delta` / `stop` 事件齐全 |
| 自测套件 | ✅ 53 项离线断言 + 24 项在线冒烟全绿 |
| 局域网 / ZeroTier 真实调用 | ✅ 远程打真实模型返回正常，对外 `/health` 自动脱敏 |
| 计划任务自启 + 双层自愈 | ✅ 进程级自愈 **14.8s**；整树崩溃后看门狗恢复 **5.3s** |
| 启动器幂等守卫 | ✅ 服务健康时重复启动 **0.2s** 退出，不抢端口不热循环 |
| CC Switch 深链导入 + 端到端 | ✅ 落库成功，`codex exec` 用导入配置返回 `GTWB-CCSW-OK` |
| Codex CLI 真实跑通 | ✅ 工具调用闭环 + 多轮 agent 循环（5 轮往返，投影压缩 `25227 → 436` 字符） |

---

## ⚠️ 风险与边界（务必知悉）

本项目是**灰区用法**：复用订阅登录态转第三方客户端，可能违反平台服务条款。

- 官方条款（企业版协议 3.4）明确写了服务内置**规避检测**能力，平台可
  **远程上报账号与 user ID**；3.3(c)(iv) 允许其**无需事先通知**限制或暂停账号。
- 风控信号是错误码 **11140**，触发场景包括高频调用、多设备/异地登录、额度耗尽。
  该码**不等于封号**，可邮件申诉，但它是真实的账号级拦截。

因此本项目的设计取向是**把风险压到最低**：

- **主动不做多账号池轮转**（`allow_account_rotation` 默认关闭）。这是条款明令禁止、
  风险最高的一档，单账号自用完全不需要。
- **默认只监听 `127.0.0.1`**，不对外暴露；如需局域网访问请自行加 TLS 与 `api_key`。
- **日志不记录任何令牌**：不读取 `Authorization` 头，落盘的只有模型名、耗时、token 数。

**建议**：单账号、本机、低频使用；不要绑工作主账号去跑密集 agent 任务。

> ⚠️ `auth` 文件里是**明文 token**，`state.json` 含账号 uid，这两个文件都不要外发或提交 git。
> 使用本项目产生的一切后果由使用者自行承担。

---

## 🙏 来源与致谢

本项目是**合并与改进**，不是从零发明。协议适配层直接继承了 MIT 授权的开源实现，
特此致谢（按贡献排序，完整许可文本见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)）：

- **[ShouZhuo0413/codebuddy2openai](https://github.com/ShouZhuo0413/codebuddy2openai)**（MIT）
  —— `responses_adapter.py`、`responses_projection.py`、`anthropic_adapter.py`、
  `desensitize.py` 四个模块源自该项目。Codex 投影压缩是三家实现里最难、最有价值的部分，
  本项目的 Codex 支持完全建立在它之上。
- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**（MIT）
  —— 冷却/熔断状态机语义、错误分类判据（`ErrKind`）、结构化请求日志格式、请求头规范
  参考其 Go 实现重写。
- **[neipor/codebuddy-cli2api](https://github.com/neipor/codebuddy-cli2api)**
  —— 模块化协议分层思路、OAuth 会话刷新与回写、动态模型发现思路。

以上均为 MIT / 开源许可，本项目的修改与新代码同样以 [MIT](LICENSE) 发布。

---

## 📁 目录结构

```
gt-wb-gateway/
├── gtwb/
│   ├── __main__.py       CLI 入口（预检、参数、启动）
│   ├── config.py         配置装配 + 上游常量
│   ├── auth.py           登录态发现/解析/刷新/原子回写
│   ├── upstream.py       后端调用 + 错误分类 + 模型清单
│   ├── resilience.py     冷却/熔断状态机 + 状态落盘
│   ├── obs.py            结构化请求日志 + 单请求统计
│   ├── server.py         HTTP 路由 + 统一执行链
│   ├── desensitize.py            ┐
│   ├── responses_adapter.py      │ 协议适配层
│   ├── responses_projection.py   │ （继承自 codebuddy2openai）
│   └── anthropic_adapter.py      ┘
├── deploy/
│   ├── setup.ps1           一键部署（生成 key / 写配置 / 防火墙 / 计划任务）
│   ├── uninstall.ps1       撤销部署（保留 config.json）
│   ├── serve-loop.bat      常驻守护（幂等守卫 + 进程退出自动重启）
│   ├── make-deeplink.py    生成 CC Switch 一键导入深链
│   └── make-client-kit.py  生成「另一台电脑接入包」（含自检脚本）
├── docs/
│   ├── 接入CC-Switch.md       CC Switch 三种接入方式 + 协议判定说明
│   └── 双机共享-家里电脑.md    ZeroTier 双机方案 / 防火墙 / 稳定性清单 / 风控红线
├── tests/                自测套件（53 项离线断言 + 24 项在线冒烟）
├── config.example.json
├── requirements.txt
└── start.bat
```

---

<div align="center">

如果这个项目对你有用，欢迎点个 ⭐

</div>
