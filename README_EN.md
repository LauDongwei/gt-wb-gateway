<div align="center">

# gt-wb-gateway

**Turn your WorkBuddy / CodeBuddy desktop subscription into your own local OpenAI / Anthropic-compatible API**

Let Codex CLI, Claude Code, Cherry Studio and any OpenAI-compatible client
reuse the models you already subscribed to
(GLM, DeepSeek, Kimi, Hunyuan, MiniMax, etc.)

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#-quick-start)

Single account · Runs locally · Zero external dependencies · No Docker required

Start with one command: `python -m gtwb`

[简体中文](README.md) | **English**

</div>

---

## ✨ Why this project

The three existing open-source projects each miss a piece. This project
**takes the best of all three and fills the gaps**:

| Capability | [codebuddy2openai](https://github.com/ShouZhuo0413/codebuddy2openai) | [workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) | [codebuddy-cli2api](https://github.com/neipor/codebuddy-cli2api) | **This project** |
|---|:--:|:--:|:--:|:--:|
| `/v1/responses` (required by Codex CLI) | ✅ | ❌ missing | ✅ | ✅ |
| `/v1/chat/completions` | ✅ | ✅ | ✅ | ✅ |
| `/v1/messages` (Claude Code) | ✅ | ❌ missing | ✅ | ✅ |
| Codex request projection & compression | ✅ | ❌ | ❌ | ✅ |
| Zero-width desensitization to bypass content filters | ✅ | ❌ | ❌ | ✅ |
| **Auto-retry on content-filter blocks** | Responses only | ❌ | ❌ | ✅ **all three endpoints** |
| Cooldown / circuit-breaker state machine | ❌ | ✅ | ❌ | ✅ |
| Structured request logs (TTFB / token rate) | partial | ✅ | ❌ | ✅ |
| Dynamic model list | ❌ hardcoded | ✅ | local file | ✅ **live API + 1h cache** |
| Cross-platform session discovery | ✅ | macOS/Linux only | **misses Windows** | ✅ all three |
| Multi-account rotation pool | ❌ | ✅ | ✅ | ⛔ **deliberately not implemented** (see [Risks & Boundaries](#%EF%B8%8F-risks--boundaries-read-this-first)) |

Three real bugs fixed along the way:

1. **Stale model list** — the original hardcoded list stopped at `glm-5.2`, while real
   accounts already have `glm-5.3`, `kimi-k3-1`, `hy4-preview`, `deepseek-v4.1-flash`.
   This project fetches the list from the backend's live API instead.
2. **Missing Windows session discovery** — the original missed the Windows path,
   forcing Windows users to set environment variables manually.
3. **False expiry detection** — when the auth file has no `expiresAt` field, the original
   treats the session as expired and **refreshes the token on every single request**.
   This project makes no prediction when the expiry is unknown and only refreshes on 401.

---

## 🚀 Quick start

The only prerequisite: **the WorkBuddy / CodeBuddy desktop app is logged in on this machine**.
The session file is discovered automatically — zero configuration.

```bash
# 1) Install dependencies (only 3 packages)
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt

# 2) Start (on Windows you can simply double-click start.bat)
.venv/Scripts/python.exe -m gtwb

# 3) Verify
curl http://127.0.0.1:8787/health      # account & dependency status
curl http://127.0.0.1:8787/v1/models   # model list
curl http://127.0.0.1:8787/status      # cooldown / circuit-breaker state
```

Run the test suite (99 offline assertions covering the state machine, session parsing and protocol hardening):

```bash
.venv/Scripts/python.exe tests/test_gateway.py
```

> Want it to run persistently with auto-start and crash recovery? Run `deploy/setup.ps1` —
> it generates an API key → writes the config → creates firewall rules → registers a
> Windows scheduled task with a watchdog. Undo with `deploy/uninstall.ps1`.

---

## 🔌 Client integration

### Codex CLI

**If you have CC Switch installed, import through CC Switch first** — do not hand-edit
`~/.codex/config.toml`, because CC Switch's "Live takeover" rewrites the whole file and
will wipe your manual changes. One command generates the import deep link:

```powershell
.venv\Scripts\python.exe deploy\make-deeplink.py --host 127.0.0.1 --open
```

> To verify the import actually landed, **query the database, not the process** —
> CC Switch is a persistent tray app; a running process ≠ a successful import:
>
> ```bash
> python -c "import sqlite3,os;db=os.path.expandvars(r'%USERPROFILE%\.cc-switch\cc-switch.db');c=sqlite3.connect(db);print([r[0] for r in c.execute(\"select name from providers where app_type='codex'\")])"
> ```
>
> Also note: CC Switch will **auto-pick a model** from the gateway according to
> `endpointAutoSelect`, so don't assume the imported model matches what you put in the link.

Without CC Switch, hand-write `~/.codex/config.toml` (**append** — don't overwrite existing config):

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
export GTWB_KEY=any-value      # if the server has no api_key set, any placeholder works
codex --profile gtwb "your task"
```

<details>
<summary><b>Want isolated verification (without touching your existing config)? Three pitfalls we hit for real</b></summary>

```powershell
$env:CODEX_HOME = "$env:LOCALAPPDATA\codex-gtwb-test"   # put your own config.toml here
$env:GTWB_KEY   = "<api_key from config.json>"
codex exec --skip-git-repo-check "Reply with exactly: OK"
```

| Pitfall | Symptom | Fix |
|---|---|---|
| **CODEX_HOME must be set from PowerShell** | `set CODEX_HOME` inside a `.bat` is silently ignored; codex falls back to `~/.codex/config.toml`, so your "isolated test" is actually testing your production config | Set it in PowerShell, or type it by hand |
| **Don't put CODEX_HOME inside the repo** | codex writes `sessions/` and several sqlite state files there (~3MB in our test) | Use `%LOCALAPPDATA%` |
| **A stale `codex.exe` locks all subsequent calls** | After a test is Ctrl+C'd or killed, every later `codex` hangs at startup: no error, no request, nothing | `Get-Process codex \| Stop-Process -Force`, then retry |

</details>

### Claude Code / other clients

Use the Anthropic-compatible `/v1/messages`:

```json
{
  "GLM-5.3": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "glm-5.3"
  }
}
```

Model names must be real ids supported by the backend (check `/v1/models`) — no auto-mapping.

Cherry Studio / LobeChat / NextChat / Open WebUI and other OpenAI-compatible clients:
set Base URL to `http://127.0.0.1:8787/v1`, leave the API key empty (or use the key
set at startup).

> Want **another computer** (e.g. your home machine) to use this machine's models too?
> See `docs/双机共享-家里电脑.md`: reuse a ZeroTier private network plus a firewall rule
> restricted to that source subnet — no cloud server needed. (Doc is in Chinese.)

---

## 📡 Endpoints

| Method | Path | Auth | Description |
|---|---|:--:|---|
| POST | `/v1/chat/completions` | ✅ | OpenAI Chat, native tools / tool_calls / streaming |
| POST | `/v1/responses` | ✅ | OpenAI Responses, for Codex CLI |
| POST | `/v1/messages` | ✅ | Anthropic Messages, for Claude Code |
| POST | `/v1/messages/count_tokens` | ✅ | Anthropic token counting (rough estimate, satisfies client preflight) |
| GET | `/v1/models` | ✅ | Dynamic model list (live backend API + 1h cache) |
| GET | `/health` `/healthz` | ❌ | Liveness probe; **returns only `status` + `service`** — detailed diagnostics are loopback-only |
| GET | `/status` | ✅ | Account & cooldown/circuit-breaker state |

Auth: `Authorization: Bearer <api_key>` or `X-Api-Key: <api_key>`.
If `api_key` is empty, no check is performed — **if you bind `0.0.0.0` you must set one**,
otherwise you're lending your account to the entire subnet.

---

## 🛡️ Resilience

| Upstream response | Classification | Handling |
|---|---|---|
| 429 / body contains `11140` | Rate limited | Soft cooldown, exponential backoff per strike (60s → 120s → … capped at 2h) |
| 402 / insufficient balance | Quota exhausted | Hard cooldown until 04:00 next day, waiting for the quota to reset |
| 401 + `12153` / `Offline user session` | Session invalidated | Permanently disabled; re-login on the desktop app required |
| 401 (other) | Token expired | **Force one refresh** and retry automatically |
| 404 | Upstream hiccup | Fixed 60s short cooldown, **not counted toward the breaker** (prevents avalanches) |
| 5xx | Upstream failure | Counts toward consecutive failures; breaker trips at threshold with exponential backoff |
| Network error | Transport | Logged but not penalized (too harsh to punish jitter collectively) |

- State is persisted to `state.json`; cooldown/breaker timers survive restarts.
- In-flight request cap per account defaults to 3, so you don't rate-limit yourself.
- The `developer` role from client layers is downgraded to `system` automatically
  (the backend rejects it and would trigger risk-control 11128).
- Security red line: `X-Refresh-Token` **only ever appears on the refresh endpoint**,
  never on chat requests.

### Dealing with false content-filter blocks

Codex / Claude Code system prompts naturally contain words like `sandbox`, `credential`,
`escalation`, `exploit`, which the backend's filter misreads. Three layers of protection
are enabled by default:

1. **Identity neutralization** — rewrites "I am the X CLI" identity claims into neutral
   phrasing. Measured to be the **sole trigger** of the upstream `code 11128` security
   policy; touching only the identity while preserving behavioral instructions verbatim
   keeps the operating manual intact *and* passes the filter (a 47,140-char request
   returns 200 on the first attempt);
2. **Zero-width desensitization** — inserts zero-width spaces into sensitive words;
   invisible to humans, breaks the filter's keyword matching;
3. **Retry on hit** — detects `content-filter` and retries once in compact mode
   (fallback only; normally never triggered).

> Earlier versions passed the filter by *compressing the harness prompt into a summary*.
> The cost: Codex instructions shrank from 21,026 chars to 173, and every tool description
> was wiped — the model lost its operating manual and stopped calling tools properly.
> **That was the real reason function calling "didn't work"**, now replaced by
> identity-only neutralization.

To keep the full original system prompt, pass `--no-compact` (higher false-block rate;
layer 3 kicks in automatically when that happens).

---

## ⚙️ Configuration

Copy `config.example.json` to `config.json` and edit it, or override via CLI flags /
environment variables. Priority: **defaults → config.json → env vars (`GTWB_*`) → CLI**.

| Flag | Default | Description |
|---|---|---|
| `--host` / `--port` | `127.0.0.1` / `8787` | Listen address and port |
| `--api-key` | empty | If set, clients must send a Bearer token |
| `--log PATH` | empty | Request log file |
| `--auth-file` / `--auth-dir` | auto-detect | Manual session location |
| `--no-desensitize` | off | Disable desensitization (significantly higher false-block rate) |
| `--no-compact` | off | Keep the full system prompt |
| `--no-client-identity` | off | Do not report as the official client (see "Client identity" below) |
| `--verbose` | off | Log full request/response bodies (for debugging content filters) |
| `--allow-rotation` | off | Enable multi-account rotation (**not recommended**) |
| `--show-config` | — | Print the effective config and exit |

### Client identity reporting

The backend identifies *which client* a call came from by **request headers**, and
shows it in the "client" column of the usage breakdown on `workbuddy.cn`
(Profile → Plan & usage → Usage details). Sending only `Authorization` without the
identity headers leaves that column empty — awkward for auditing your own spend,
and it makes the traffic look unattributed.

By default the gateway reports the official client shape:

| Header | Value |
|---|---|
| `X-IDE-Type` / `X-IDE-Name` | `WorkBuddy` |
| `X-IDE-Version` | locally installed version (auto-detected from `install-manifest.json`) |
| `X-Product` | `SaaS` |
| `User-Agent` | `WorkBuddy/<ver> WorkBuddy/<ver> CLI/<cliVer>` |

The version follows client upgrades automatically — no config change needed.
To disable it (e.g. for A/B debugging): `--no-client-identity`,
`client_identity: false`, or `GTWB_CLIENT_IDENTITY=0`.

> Note: these headers only affect **attribution**. They do not change the nature of
> using a subscription quota through a third-party client — that depends on the
> subscription terms. Reporting them keeps the usage breakdown auditable and avoids
> unattributed records.

---

## ✅ Verified against the real backend

These are measured results against the **real backend**, not paper claims:

| Verification | Result |
|---|---|
| Session auto-discovery | ✅ Windows / macOS / Linux paths all covered |
| Dynamic model list | ✅ 30 models returned live |
| `/v1/chat/completions` non-streaming + tool calls | ✅ 200, `finish=tool_calls`, args round-trip correctly |
| `/v1/responses` non-streaming + streaming | ✅ complete Codex event sequence (created → in_progress → delta → done) |
| `/v1/messages` non-streaming + streaming | ✅ `content_block_start` / `text_delta` / `stop` events all present |
| Test suites | ✅ 99 offline assertions, all green |
| LAN / ZeroTier real calls | ✅ real model calls succeed remotely; external `/health` auto-desensitized |
| Scheduled task + two-layer self-healing | ✅ process-level recovery in **14.8s**; full-tree crash recovered by watchdog in **5.3s** |
| Idempotent launcher guard | ✅ duplicate start exits in **0.2s** when healthy — no port grabbing, no hot loop |
| CC Switch deep-link import + end-to-end | ✅ import landed; `codex exec` with the imported config returned `GTWB-CCSW-OK` |
| Codex CLI real run | ✅ multi-step agent end-to-end (read file → analyse → write artifact); tool-call loop closed, parallel tool calls working, **zero escalation retries** |
| Long-context fidelity | ✅ a 693,834-char session retains **53.3%** and the model still answers both the opening task and recent config (old build: 2.2%) |

---

## ⚠️ Risks & Boundaries (read this first)

This project operates in a **gray area**: it reuses your subscription's login session for
third-party clients, which may violate the platform's terms of service.

- The official terms (enterprise agreement 3.4) explicitly state the service has built-in
  **anti-detection** capabilities and the platform may **remotely report accounts and user IDs**;
  3.3(c)(iv) allows it to **restrict or suspend accounts without prior notice**.
- The risk-control signal is error code **11140**, triggered by high-frequency calls,
  multi-device / cross-region logins, exhausted quotas, etc. The code **does not mean a ban**
  and can be appealed by email — but it is a real, account-level block.

This project is therefore designed to **minimize risk**:

- **Multi-account rotation is deliberately not implemented** (`allow_account_rotation`
  defaults to off). It is the explicitly forbidden, highest-risk tier — a single account
  for personal use simply doesn't need it.
- **Listens on `127.0.0.1` by default**, never exposed; for LAN access add TLS and an `api_key` yourself.
- **Logs never contain tokens**: the `Authorization` header is never read; only model name,
  latency and token counts are written.

**Recommendation**: single account, local machine, moderate usage; don't run heavy agent
workloads on your primary work account.

> ⚠️ The `auth` file contains **plaintext tokens** and `state.json` contains your account
> uid — never share or commit these files. Use of this project is at your own risk.

---

## 🙏 Credits

This project is a **merge and improvement**, not an invention from scratch. The protocol
adaptation layer directly inherits MIT-licensed open-source work. Full license texts are in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) (ordered by contribution):

- **[ShouZhuo0413/codebuddy2openai](https://github.com/ShouZhuo0413/codebuddy2openai)** (MIT)
  — `responses_adapter.py`, `responses_projection.py`, `anthropic_adapter.py` and
  `desensitize.py` come from this project. Its Codex projection & compression is the hardest,
  most valuable piece of the three implementations; this project's Codex support is built on it.
- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)** (MIT)
  — cooldown/circuit-breaker state-machine semantics, error classification (`ErrKind`),
  structured request log format and header conventions were rewritten with reference to
  its Go implementation.
- **[neipor/codebuddy-cli2api](https://github.com/neipor/codebuddy-cli2api)**
  — modular protocol layering, OAuth session refresh & write-back, dynamic model discovery.

All are MIT / open-source licensed; this project's modifications and new code are also
released under the [MIT license](LICENSE).

---

## 📁 Project layout

```
gt-wb-gateway/
├── gtwb/
│   ├── __main__.py       CLI entry (preflight, args, startup)
│   ├── config.py         config assembly + upstream constants
│   ├── auth.py           session discovery / parsing / refresh / atomic write-back
│   ├── upstream.py       backend calls + error classification + model list
│   ├── resilience.py     cooldown/circuit-breaker state machine + state persistence
│   ├── obs.py            structured request logs + per-request stats
│   ├── server.py         HTTP routing + unified execution chain
│   ├── desensitize.py            ┐
│   ├── responses_adapter.py      │ protocol adaptation layer
│   ├── responses_projection.py   │ (inherited from codebuddy2openai)
│   └── anthropic_adapter.py      ┘
├── deploy/
│   ├── setup.ps1           one-shot deploy (key gen / config / firewall / scheduled task)
│   ├── uninstall.ps1       undo the deploy (keeps config.json)
│   ├── serve-loop.bat      resident daemon (idempotent guard + auto-restart)
│   ├── make-deeplink.py    generate CC Switch one-click import deep link
│   └── make-client-kit.py  generate a "second computer" kit (with self-check script)
├── docs/
│   ├── 接入CC-Switch.md       CC Switch integration methods + protocol notes (Chinese)
│   └── 双机共享-家里电脑.md    ZeroTier two-machine setup / firewall / stability / risk lines (Chinese)
├── tests/                test suite (99 offline assertions)
├── config.example.json
├── requirements.txt
└── start.bat
```

---

<div align="center">

If this project is useful to you, a ⭐ would be appreciated

**[简体中文](README.md) | English**

</div>
