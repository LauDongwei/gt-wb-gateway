"""配置加载：默认值 ← config.json ← 环境变量 ← 命令行参数（后者覆盖前者）。

设计原则：所有可调项都有安全默认值，零配置即可跑起来。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 上游常量（来自三家参考项目的实测/文档，非猜测）
# ---------------------------------------------------------------------------

# 聊天主链路（Chat Completions + token 刷新）。桌面端打的就是这个域。
BACKEND_CHAT = "https://copilot.tencent.com"
# 网页/计费域，作为 domain 回退与 Origin/Referer 取值来源。
WEB_ORIGIN = "https://www.codebuddy.cn"

# ---------------------------------------------------------------------------
# 客户端身份（后端据此归因用量明细里的「客户端」列）
# ---------------------------------------------------------------------------
#
# 实证来源（本机安装包，非猜测）：
#   1. 桌面端 `resources/app.asar` 向被拉起的 agent-cli 注入
#      CLIENT_INFO_PLATFORM / CLIENT_INFO_IDE_TYPE = "WorkBuddy"，
#      CLIENT_INFO_USER_AGENT_EXTENSION = `CLI/<cliVersion>`。
#   2. agent-cli 组装请求头时写：
#        X-IDE-Type    = ideType   || PRODUCT_TYPE
#        X-IDE-Name    = platform  || PRODUCT_TYPE
#        X-IDE-Version = platformVersion
#        X-Product     = deploymentType（SaaS）
#        User-Agent    = "<product>/<ver> <platform>/<ver> CLI/<cliVer>"
#   3. 桌面端注释自述该版本号"用于服务端白名单识别客户端身份"。
#
# 换句话说：后端靠这组头（而不是靠请求体）判断"这次调用来自哪个客户端"。
# 只发 Authorization 而漏掉这组头 → 用量明细的「客户端」列归因为空。
CLIENT_NAME = "WorkBuddy"
# 桌面端版本探测失败时的兜底值（本机 2026-09-10 安装版实测）。
DEFAULT_CLIENT_VERSION = "5.5.6"
# 随包 CLI 版本兜底值（`node cli/dist/codebuddy.js --version` 实测）。
DEFAULT_CLI_VERSION = "2.137.1"
# 更早版本用的 UA：那是 CodeBuddy CLI 的身份，不是 WorkBuddy 桌面端，
# 保留仅为兼容显式配置。
LEGACY_USER_AGENT = "CLI/2.63.2 CodeBuddy/2.63.2"


def _client_manifest_candidates() -> list[str]:
    """各平台 WorkBuddy 桌面端安装清单路径（含 appVersion 字段）。"""
    import sys

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return [
            os.path.join(local, "Programs/WorkBuddy/resources/install-manifest.json"),
            r"C:\Program Files\WorkBuddy\resources\install-manifest.json",
            r"D:\Program Files\WorkBuddy\resources\install-manifest.json",
        ]
    if sys.platform == "darwin":
        return [
            "/Applications/WorkBuddy.app/Contents/Resources/install-manifest.json",
        ]
    return [
        "/opt/WorkBuddy/resources/install-manifest.json",
        os.path.expanduser("~/.local/share/WorkBuddy/resources/install-manifest.json"),
    ]


_CLIENT_VERSION_CACHE: dict[str, str] = {}


def detect_client_version() -> str:
    """自动探测本机 WorkBuddy 桌面端版本；探测不到返回空串（由调用方兜底）。

    自动探测而不是写死，是因为客户端升级后版本号会变，而版本号是上游
    识别客户端身份的一部分。结果进程内缓存，不做重复磁盘 IO。
    """
    if "v" in _CLIENT_VERSION_CACHE:
        return _CLIENT_VERSION_CACHE["v"]
    found = ""
    for p in _client_manifest_candidates():
        try:
            if not os.path.isfile(p):
                continue
            data = json.loads(open(p, encoding="utf-8-sig").read())
            v = str(data.get("appVersion") or "").strip()
            if v:
                found = v
                break
        except Exception:
            continue
    _CLIENT_VERSION_CACHE["v"] = found
    return found

CHAT_PATH = "/v2/chat/completions"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
# 动态模型清单：返回账号真实可用模型，比任何硬编码列表都新。
MODELS_PATH = "/console/enterprises/personal/models"

# 兜底模型表（动态接口与本地配置都拿不到时使用）。
FALLBACK_MODELS = [
    "auto",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "kimi-k3-1",
    "kimi-k2.7",
    "minimax-m3",
    "hy3",
]


@dataclass
class Config:
    # ── 服务 ──────────────────────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8787
    api_key: str = ""  # 非空则要求客户端带 Bearer
    log_path: str | None = None  # 非空则把请求日志落盘

    # ── 协议转换 ──────────────────────────────────────────────────────────
    desensitize: bool = True  # 默认开：Codex / Claude Code 的长 system prompt 极易撞审核
    compact_harness: bool = True  # 审核降级时压缩 harness 提示词（见 preserve_harness）
    strip_tool_metadata: bool = True  # 审核降级时去掉 tool description
    retry_on_filter: bool = True  # 命中审核时用紧凑模式重试一次

    # 首轮是否忠实透传 harness 提示词与工具描述（只在"无损脱敏"下工作）。
    #
    # 实测教训：把 compact_harness 无条件应用到每个请求，会把 Codex 的 2 万字符
    # 操作手册压成 173 字符、把 9 个工具的描述全部清空。模型因此不知道工具怎么用，
    # agent 任务质量直接崩掉。而实测 1412 次请求里命中审核 0 次 —— 用"必然的
    # 能力损失"去防"从未发生的风险"不划算。
    # 现在的做法：首轮无损（只插零宽空格），只有真的被拦了才降级压缩重试。
    preserve_harness: bool = True

    # 请求了不可用模型名时的兜底模型（空串 = 保持原样报 400）。
    # 客户端（cc-switch 等）常把模型名写成 Codex 原生名（gpt-5.6-luna 等），
    # 这些名字在上游不存在 → 400 → 整条 agent 链路直接失败。
    model_fallback: str = ""
    # 显式别名映射，例如 {"gpt-5.6-luna": "deepseek-v4.1-flash"}
    model_aliases: dict[str, str] = field(default_factory=dict)

    # 诊断抓包目录（非空则把每个 /v1/responses 请求体落盘，用于排查协议问题）
    capture_dir: str | None = None

    # ── 客户端身份上报 ────────────────────────────────────────────────────
    # 后端按 X-IDE-* 与 UA 归因用量明细的「客户端」列。只发 Authorization
    # 会让该列归因为空 —— 既不便自己核对消耗，也更容易被当成来源不明的流量。
    # 默认按本机官方客户端身份上报，与官方记录保持一致。
    client_identity: bool = True
    client_name: str = CLIENT_NAME
    client_version: str = ""  # 空 = 自动探测本机安装版本
    cli_version: str = ""  # 空 = 内置兜底
    # 显式指定则覆盖上面两项拼出的 UA（一般留空即可）。
    user_agent: str = ""

    # ── 网关侧联网搜索 ────────────────────────────────────────────────────
    # 上游没有任何原生搜索能力（实测 enable_search / web_search_options /
    # 托管型 web_search 全被静默忽略）。Codex 的 {"type":"web_search"} 托管工具
    # 因此降级成普通 function，由网关自己检索后把结果回灌给模型。
    # 关掉本项则退回旧行为：丢弃 web_search 工具，客户端仍可通过 exec_command
    # 自己 curl，但拿不到原生搜索体验。
    web_search: bool = True
    # 引擎顺序（不填 = 内置默认：gnews → cn_bing → so360 → wiki）。
    # 配了官方 key 的 wsa/brave/serper/tavily 会自动优先并挤掉国内泛搜兜底。
    web_search_engines: list[str] = field(default_factory=list)
    # 搜索请求走的代理。本机 Clash 默认 7897；留空则直连（TUN 模式同样被接管）。
    web_search_proxy: str = ""
    web_search_timeout_s: float = 15.0
    # 一次请求里模型最多触发几轮「搜索→回灌」，防止模型陷入搜索死循环。
    web_search_max_rounds: int = 3
    web_search_api_key: str = ""  # 腾讯云联网搜索（WSA，国内合规，推荐）
    web_search_brave_key: str = ""
    web_search_serper_key: str = ""
    web_search_tavily_key: str = ""

    # ── 上游 ──────────────────────────────────────────────────────────────
    backend: str = BACKEND_CHAT
    web_origin: str = WEB_ORIGIN
    timeout_s: float = 300.0  # 单次上游请求总超时
    connect_timeout_s: float = 30.0

    # ── 韧性（来自 Sliverkiss 的状态机语义）────────────────────────────────
    soft_cooldown_s: int = 60  # 429 软冷却基数
    soft_cooldown_max_s: int = 7200  # 指数退避上限
    notfound_cooldown_s: int = 60  # 404 固定短冷却
    breaker_threshold: int = 3  # 连续 5xx 达此次数触发熔断
    breaker_cooldown_s: int = 1800  # 熔断冷却基数
    breaker_cooldown_max_s: int = 21600  # 熔断冷却上限
    hard_cooldown_hour: int = 4  # 余额不足 → 冷却到次日该点（等额度恢复）
    max_in_flight: int = 3  # 单账号在途请求上限
    upstream_retry: int = 2  # 传输层错误/429/5xx 首字节前自动重试次数（学习 new-api）
    retry_backoff_s: float = 1.5  # 重试退避基数（×attempt 线性退避）

    # ── 状态持久化 ────────────────────────────────────────────────────────
    state_file: str = "state.json"

    # ── 凭据 ──────────────────────────────────────────────────────────────
    auth_file: str | None = None  # 直接指定 .info 文件
    auth_dir: str | None = None  # 指定目录 → 扫描其中全部 .info（多账号）
    allow_account_rotation: bool = False  # 是否允许多账号轮转（默认关闭，见 README 风险说明）
    refresh_skew_s: int = 300  # 距过期不足此时长就先刷新

    # ── 客户端身份解析 ────────────────────────────────────────────────────

    def resolved_client_version(self) -> str:
        return self.client_version or detect_client_version() or DEFAULT_CLIENT_VERSION

    def resolved_cli_version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def official_user_agent(self) -> str:
        """按官方客户端格式拼 UA。

        官方格式：`<product>/<ver> <platform>/<ver> CLI/<cliVer>`。
        WorkBuddy 桌面端下 product 与 platform 同为 "WorkBuddy"，
        两处版本号同为桌面端版本，末段是随包 CLI 版本。
        """
        name = self.client_name or CLIENT_NAME
        v = self.resolved_client_version()
        return f"{name}/{v} {name}/{v} CLI/{self.resolved_cli_version()}"

    def effective_user_agent(self) -> str:
        """最终用于上游的 UA：显式配置优先，其次按身份开关决定。"""
        if self.user_agent:
            return self.user_agent
        if self.client_identity:
            return self.official_user_agent()
        return LEGACY_USER_AGENT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_aliases(raw: str) -> dict[str, str]:
    """解析 GTWB_MODEL_ALIASES，形如 'gpt-5.6-luna=deepseek-v4.1-flash,a=b'。"""
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


# 环境变量 → 配置字段。前缀 GTWB_。
_ENV_MAP: dict[str, tuple[str, type]] = {
    "GTWB_HOST": ("host", str),
    "GTWB_PORT": ("port", int),
    "GTWB_API_KEY": ("api_key", str),
    "GTWB_LOG": ("log_path", str),
    "GTWB_DESENSITIZE": ("desensitize", bool),
    "GTWB_COMPACT": ("compact_harness", bool),
    "GTWB_PRESERVE_HARNESS": ("preserve_harness", bool),
    "GTWB_RETRY_ON_FILTER": ("retry_on_filter", bool),
    "GTWB_MODEL_FALLBACK": ("model_fallback", str),
    "GTWB_CAPTURE_DIR": ("capture_dir", str),
    "GTWB_BACKEND": ("backend", str),
    "GTWB_WEB_ORIGIN": ("web_origin", str),
    "GTWB_TIMEOUT": ("timeout_s", float),
    "GTWB_CLIENT_IDENTITY": ("client_identity", bool),
    "GTWB_CLIENT_NAME": ("client_name", str),
    "GTWB_CLIENT_VERSION": ("client_version", str),
    "GTWB_CLI_VERSION": ("cli_version", str),
    "GTWB_USER_AGENT": ("user_agent", str),
    "GTWB_WEB_SEARCH": ("web_search", bool),
    "GTWB_WEB_SEARCH_PROXY": ("web_search_proxy", str),
    "GTWB_WEB_SEARCH_TIMEOUT": ("web_search_timeout_s", float),
    "GTWB_WEB_SEARCH_MAX_ROUNDS": ("web_search_max_rounds", int),
    "GTWB_WEB_SEARCH_API_KEY": ("web_search_api_key", str),
    "GTWB_WEB_SEARCH_BRAVE_KEY": ("web_search_brave_key", str),
    "GTWB_WEB_SEARCH_SERPER_KEY": ("web_search_serper_key", str),
    "GTWB_WEB_SEARCH_TAVILY_KEY": ("web_search_tavily_key", str),
    "GTWB_STATE_FILE": ("state_file", str),
    "GTWB_AUTH_FILE": ("auth_file", str),
    "GTWB_AUTH_DIR": ("auth_dir", str),
    "GTWB_ALLOW_ROTATION": ("allow_account_rotation", bool),
    # 兼容两家旧项目的环境变量名，降低迁移成本
    "CODEBUDDY_AUTH_FILE": ("auth_file", str),
    "CODEBUDDY_AUTH_DIR": ("auth_dir", str),
    "CODEBUDDY2OPENAI_KEY": ("api_key", str),
    "CODEBUDDY2OPENAI_LOG": ("log_path", str),
}


def _coerce(raw: str, typ: type) -> Any:
    if typ is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if typ is int:
        return int(raw)
    if typ is float:
        return float(raw)
    return raw


def load_config(
    config_path: str | Path | None = None,
    env: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """按 默认值 → 文件 → 环境 → 显式覆盖 的顺序装配配置。"""
    cfg = Config()
    valid = {f.name for f in fields(Config)}

    # 1) 配置文件
    if config_path is None:
        cand = Path.cwd() / "config.json"
        config_path = cand if cand.is_file() else None
    if config_path:
        p = Path(config_path)
        if not p.is_file():
            raise FileNotFoundError(f"配置文件不存在：{p}")
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        for k, v in data.items():
            if k in valid:
                setattr(cfg, k, v)

    # 2) 环境变量
    env = env if env is not None else os.environ
    for env_key, (field_name, typ) in _ENV_MAP.items():
        raw = env.get(env_key)
        if raw:
            setattr(cfg, field_name, _coerce(raw, typ))
    if env.get("GTWB_MODEL_ALIASES"):
        cfg.model_aliases = _parse_aliases(env["GTWB_MODEL_ALIASES"])
    if env.get("GTWB_WEB_SEARCH_ENGINES"):
        cfg.web_search_engines = [
            s.strip() for s in env["GTWB_WEB_SEARCH_ENGINES"].split(",") if s.strip()
        ]

    # 3) 显式覆盖（命令行）
    for k, v in (overrides or {}).items():
        if v is not None and k in valid:
            setattr(cfg, k, v)

    return cfg
