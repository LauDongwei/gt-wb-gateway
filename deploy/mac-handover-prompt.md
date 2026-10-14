# 任务：把这台 Mac 部署成独立上游（gt-wb-gateway）

> 把本文件全部内容复制粘贴给这台 Mac 的 WorkBuddy，让它照着执行。
>
> **它的定位是「独立上游」**：用自己的 WorkBuddy 订阅，通过本地 API 给别的客户端
> （Codex CLI / Claude Code / Cherry Studio）供模型。它不是客户端。

---

## 背景事实（不要凭假设改）

- 网关源码（公开开源，MIT）：`https://github.com/LauDongwei/gt-wb-gateway`
- 依赖：Python 3.10+、fastapi、uvicorn、httpx（`requirements.txt` 已含）
- 登录态**自动发现**，不需手填 token。macOS 路径已内置在代码里：
  `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info`
- 本机 WorkBuddy 桌面端必须**保持登录**。
- 上游账号与你手上任何其他机器的账号**互不相干**。

## 执行顺序（每步验证通过再进下一步；卡住就停下报告，不要静默降级）

### 第 0 步：先读完整部署指南

读 `docs/部署指南-macOS-20261014.md`（clone 之后就在仓库里），
本文件是它的精简执行版。下面每一步的详细解释、参数含义、验收标准都在那份指南里。

### 第 1 步：环境自检并回执

先探测再动手，把结果报回来：

```bash
sw_vers; uname -m
python3 --version; uv --version
ls ~/Library/Application\ Support/CodeBuddyExtension/Data/Public/auth/*.info 2>/dev/null
pgrep -fl "WorkBuddy|CodeBuddy" | head -3
env | grep -i proxy        # 有输出说明装了代理，后面的健康探测必须加 --noproxy
```

**判断标准**：
- `sw_vers` 确认 macOS；`uname -m` 是 `arm64` 还是 `x86_64`（只影响是否有原生依赖问题，不影响流程）
- 没装 uv 就 `brew install uv`
- 找不到 `*.info` → WorkBuddy 没登录，**停下来让我登录**，不要自己造 token
- `env | grep -i proxy` 有输出 → 记住这点，后面所有 `curl` 都要带 `--noproxy "*"`

### 第 2 步：clone + 装依赖

```bash
git clone https://github.com/LauDongwei/gt-wb-gateway.git
cd gt-wb-gateway
git log --oneline -1
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

⚠ **macOS 注意**：python 路径是 `.venv/bin/python`，**没有** `Scripts/`（那是 Windows）。

### 第 3 步：离线验收（不烧额度，必须先过）

```bash
.venv/bin/python tests/test_gateway.py
```

**必须 229 项全过、全过、全过**。有任何失败就停下报告，把失败项贴给我，
**不要改测试让它变绿**。

### 第 4 步：前台试启动

```bash
.venv/bin/python -m gtwb --show-config
.venv/bin/python -m gtwb
```

另开终端验证：

```bash
curl --noproxy "*" -s http://127.0.0.1:8787/health
```

必须看到 `"status":"ok"` 且 `account.loaded=true`。
把 `account.domain` 和 `account.nickname` 报回来 —— 我要判断是否与基线
`www.codebuddy.cn` 一致，不一致说明是国际版账号，要另外配上游域名。

### 第 5 步：配置 config.json

- 生成**一把自己的 API Key**（别用别的机器那把）
- `capture_dir` 保持 `null`（排障时才临时开，平时开了会一天写几十 GB）
- `allow_account_rotation` 保持 `false`

### 第 6 步：装开机自启（launchd，不是计划任务）

```bash
chmod +x deploy/serve-loop.sh
sed -e "s#__REPO__#$HOME/gt-wb-gateway#g" \
    -e "s#__PYTHON__#$HOME/gt-wb-gateway/.venv/bin/python#g" \
    deploy/com.gtwb.gateway.plist > ~/Library/LaunchAgents/com.gtwb.gateway.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gtwb.gateway.plist
launchctl print gui/$(id -u)/com.gtwb.gateway | head -20
```

⚠ **最关键的一条坑**：`serve-loop.sh` 如果是 CRLF 行尾，bash 会报
`bad interpreter: /bin/bash^M`，看门狗静默不运行、网关重启后就再也起不来。
装完先验：

```bash
file deploy/serve-loop.sh          # 必须显示 ASCII text，不能是 "with CRLF"
```

### 第 7 步：验证自愈

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN | awk 'NR==2{print $2}' | xargs -r kill
sleep 15 && curl --noproxy "*" -s http://127.0.0.1:8787/health
```

15 秒内应该自动恢复。

### 第 8 步：看板

```bash
open http://127.0.0.1:8735
```

三个板块：总览 / 模型用量 / 按日明细。确认都能打开。

### 第 9 步：回执

把这四项报回来：

```
1. macOS 版本 / 芯片架构 / git commit：
2. 本机自己生成的 API Key（完整明文）：
3. /v1/models 实测清单（常用 3-5 个模型名）：
4. /health 的 account.domain 与 account.nickname：
```

---

## 红线（不可协商）

1. **不要退出 WorkBuddy 桌面端登录**——网关复用它的登录态，退登等于网关失去账号。
2. **严禁把 `*.info` 登录态文件拷到任何别的机器**——那等于把两个账号合成一个，
   会触发风控、账号被封。
3. **`state.json` 也不许跨机互拷**。
4. **不要改测试让它变绿**。失败就报告，这是信息不是障碍。
5. **不要在同一个模型上反复重试**。遇到 `code 6004`（频率限制）就换模型，
   上游报错正文里会写着重置时刻，那个才是真正的恢复时间。
6. **`--noproxy "*"` 不能省**。装了代理的话，省了它健康探测会被劫持，
   看门狗会误判网关已死并反复拉起抢端口的僵尸进程。

## 卡住了怎么办

停在那一步，把以下信息报给我：执行的命令、完整输出、`/health` 的返回、
`tests/test_gateway.py` 的失败项原文。
**不要自己猜着改**——这套东西里的每个修复都有实测依据，乱改容易把别的地方弄坏。
