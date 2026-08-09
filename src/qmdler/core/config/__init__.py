"""配置子系统."""

from . import paths
from .schema import (
    BUILTIN_PRESETS,
    AuthConfig,
    CoverConfig,
    DownloadConfig,
    LogConfig,
    LyricConfig,
    NamingConfig,
    PathsConfig,
    QualityConfig,
    ServerConfig,
    Settings,
    TagConfig,
)
from .store import ConfigStore

__all__ = [
    "BUILTIN_PRESETS",
    "AuthConfig",
    "ConfigStore",
    "CoverConfig",
    "DownloadConfig",
    "LogConfig",
    "LyricConfig",
    "NamingConfig",
    "PathsConfig",
    "QualityConfig",
    "ServerConfig",
    "Settings",
    "TagConfig",
    "paths",
]
