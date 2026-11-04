import logging
import sys

# 进程级开关：重复调用 configure_logging 会叠加 handler 导致日志重复输出，只允许配置一次。
_CONFIGURED = False


def configure_logging(service_name: str, level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s level=%(levelname)s service=" + service_name + " %(name)s %(message)s"
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
