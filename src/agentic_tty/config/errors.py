"""配置错误。"""

from __future__ import annotations

from ..foundation.errors import AgenticTtyError


class ConfigError(AgenticTtyError):
    """配置不合法：键不认识、值转不过去、文件读不了。"""
