"""日志配置: 控制台 (Rich) + 可选文件轮转."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from rich.logging import RichHandler

from .core.config.schema import LogConfig

_CONFIGURED = False


def setup_logging(config: LogConfig, *, console: bool = True) -> None:
    """按配置初始化根 logger.

    Args:
        config: 日志配置.
        console: 是否输出到控制台. TUI 模式下必须为 False, 否则日志会打乱界面.
    """
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.level, logging.INFO))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    if console:
        rich_handler = RichHandler(rich_tracebacks=True, show_path=False, omit_repeated_times=False)
        rich_handler.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))
        root.addHandler(rich_handler)

    if config.file:
        path = Path(config.file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"),
        )
        root.addHandler(file_handler)

    if not console and not config.file:
        root.addHandler(logging.NullHandler())

    # 第三方库的 INFO 噪音太大.
    for noisy in ("httpx", "httpcore", "niquests", "urllib3", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def is_configured() -> bool:
    """是否已初始化过日志."""
    return _CONFIGURED
