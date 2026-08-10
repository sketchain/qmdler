# SPDX-License-Identifier: GPL-3.0-or-later
"""XDG 路径解析."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "qmdler"


def _xdg(env: str, fallback: str) -> Path:
    raw = os.environ.get(env)
    base = Path(raw).expanduser() if raw else Path.home() / fallback
    return base / APP_NAME


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/qmdler``."""
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    """``$XDG_DATA_HOME/qmdler``."""
    return _xdg("XDG_DATA_HOME", ".local/share")


def state_dir() -> Path:
    """``$XDG_STATE_HOME/qmdler``."""
    return _xdg("XDG_STATE_HOME", ".local/state")


def runtime_dir() -> Path:
    """``$XDG_RUNTIME_DIR/qmdler``, 缺失时退到 state 目录."""
    raw = os.environ.get("XDG_RUNTIME_DIR")
    if raw:
        return Path(raw).expanduser() / APP_NAME
    return state_dir()


def config_file() -> Path:
    """主配置文件路径."""
    return config_dir() / "config.toml"


def presets_file() -> Path:
    """用户自定义预设文件路径."""
    return config_dir() / "presets.toml"


def credential_file() -> Path:
    """凭证文件路径 (权限 600)."""
    return config_dir() / "credential.json"


def device_file() -> Path:
    """设备信息文件路径.

    必须持久化: 不传 ``device_path`` 时设备指纹每次重启都变, 等同于频繁换设备登录,
    极易触发风控.
    """
    return config_dir() / "device.json"


def database_file() -> Path:
    """SQLite 数据库路径."""
    return data_dir() / "qmdler.db"


def log_file() -> Path:
    """默认日志文件路径."""
    return state_dir() / "qmdler.log"


def endpoint_file() -> Path:
    """本地后端地址文件. TUI 自动拉起后端时写入, 供下次复用."""
    return runtime_dir() / "endpoint.json"


def ensure_dirs() -> None:
    """创建全部需要的目录."""
    for path in (config_dir(), data_dir(), state_dir(), runtime_dir()):
        path.mkdir(parents=True, exist_ok=True)
