"""跨服务共享的最小内核：配置、数据库、事件总线、鉴权与 HTTP 客户端。"""

from .config import ServiceSettings, get_settings
from .errors import ApiError, install_error_handlers
from .logging import configure_logging, get_logger

__all__ = [
    "ServiceSettings",
    "get_settings",
    "ApiError",
    "install_error_handlers",
    "configure_logging",
    "get_logger",
]
