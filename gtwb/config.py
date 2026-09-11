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
# 官方 CLI 的 UA；后端会按 UA 决定放行策略。
USER_AGENT = "CLI/2.63.2 CodeBuddy/2.63.2"

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
    compact_harness: bool = True  # 压缩 harness 提示词（关掉则保留原文，审核风险更高）
    strip_tool_metadata: bool = True  # 去掉 tool description 里的安全术语
    retry_on_filter: bool = True  # 命中审核时用紧凑模式重试一次（仅非压缩模式生效）

    # ── 上游 ──────────────────────────────────────────────────────────────
    backend: str = BACKEND_CHAT
    web_origin: str = WEB_ORIGIN
    user_agent: str = USER_AGENT
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

    # ── 状态持久化 ────────────────────────────────────────────────────────
    state_file: str = "state.json"

    # ── 凭据 ──────────────────────────────────────────────────────────────
    auth_file: str | None = None  # 直接指定 .info 文件
    auth_dir: str | None = None  # 指定目录 → 扫描其中全部 .info（多账号）
    allow_account_rotation: bool = False  # 是否允许多账号轮转（默认关闭，见 README 风险说明）
    refresh_skew_s: int = 300  # 距过期不足此时长就先刷新

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 环境变量 → 配置字段。前缀 GTWB_。
_ENV_MAP: dict[str, tuple[str, type]] = {
    "GTWB_HOST": ("host", str),
    "GTWB_PORT": ("port", int),
    "GTWB_API_KEY": ("api_key", str),
    "GTWB_LOG": ("log_path", str),
    "GTWB_DESENSITIZE": ("desensitize", bool),
    "GTWB_COMPACT": ("compact_harness", bool),
    "GTWB_RETRY_ON_FILTER": ("retry_on_filter", bool),
    "GTWB_BACKEND": ("backend", str),
    "GTWB_TIMEOUT": ("timeout_s", float),
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

    # 3) 显式覆盖（命令行）
    for k, v in (overrides or {}).items():
        if v is not None and k in valid:
            setattr(cfg, k, v)

    return cfg
