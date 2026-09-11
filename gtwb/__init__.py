"""gt-wb-gateway —— 自建本地模型网关。

把 WorkBuddy / CodeBuddy 的桌面端订阅转成本机可用的 OpenAI / Anthropic 兼容 API，
供 Codex CLI、Claude Code、Cherry Studio 等客户端复用。

本包是对三家开源实现的合并与改进（协议适配层继承 MIT 授权的
ShouZhuo0413/codebuddy2openai，韧性状态机语义参考 Sliverkiss/workbuddy2api，
模块化协议分层参考 neipor/codebuddy-cli2api），详见 README 的「来源与致谢」。
"""

from .config import Config, load_config

__version__ = "1.0.0"
__all__ = ["Config", "load_config", "__version__"]
