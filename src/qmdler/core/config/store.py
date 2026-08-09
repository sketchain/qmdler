"""配置读写与预设管理.

优先级 (低 → 高): 内置默认 → TOML 文件 → 选中的预设 → 环境变量.

环境变量最高是有意的: 容器部署时用 ``QMDLER_SERVER__PORT`` 之类覆盖文件里的值,
不需要重新烘一个镜像.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import tomlkit

from . import paths
from .schema import BUILTIN_PRESETS, Settings

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 回退
    import tomli as tomllib


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典, ``override`` 优先."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


class ConfigStore:
    """配置存储."""

    def __init__(self, config_path: Path | None = None, presets_path: Path | None = None) -> None:
        """初始化.

        Args:
            config_path: 主配置文件路径, 缺省走 XDG.
            presets_path: 用户预设文件路径, 缺省走 XDG.
        """
        self.config_path = config_path or paths.config_file()
        self.presets_path = presets_path or paths.presets_file()

    # -- 预设 --------------------------------------------------------------- #

    def user_presets(self) -> dict[str, dict[str, Any]]:
        """读取用户自定义预设."""
        return {name: value for name, value in _read_toml(self.presets_path).items() if isinstance(value, dict)}

    def all_presets(self) -> dict[str, dict[str, Any]]:
        """内置 + 用户预设 (同名时用户覆盖内置)."""
        merged = copy.deepcopy(BUILTIN_PRESETS)
        merged.update(self.user_presets())
        return merged

    def save_preset(self, name: str, overrides: dict[str, Any]) -> None:
        """保存 (或覆盖) 一个用户预设."""
        self.presets_path.parent.mkdir(parents=True, exist_ok=True)
        doc = tomlkit.parse(self.presets_path.read_text("utf-8")) if self.presets_path.exists() else tomlkit.document()
        doc[name] = overrides
        self.presets_path.write_text(tomlkit.dumps(doc), "utf-8")

    def delete_preset(self, name: str) -> bool:
        """删除一个用户预设. 内置预设不可删除."""
        if not self.presets_path.exists():
            return False
        doc = tomlkit.parse(self.presets_path.read_text("utf-8"))
        if name not in doc:
            return False
        del doc[name]
        self.presets_path.write_text(tomlkit.dumps(doc), "utf-8")
        return True

    # -- 主配置 ------------------------------------------------------------- #

    def raw(self) -> dict[str, Any]:
        """磁盘上的原始配置字典 (未叠加预设与环境变量)."""
        return _read_toml(self.config_path)

    def load(self, preset: str | None = None) -> Settings:
        """加载配置.

        Args:
            preset: 强制使用的预设名; 缺省读配置文件里的 ``preset`` 字段.
        """
        file_data = self.raw()
        chosen = preset or file_data.get("preset") or "default"
        merged = file_data
        if chosen and chosen != "default":
            overrides = self.all_presets().get(chosen)
            if overrides is not None:
                merged = _deep_merge(file_data, overrides)
        merged["preset"] = chosen
        # BaseSettings 的初始化参数会被环境变量覆盖, 这正是我们要的优先级.
        return Settings(**merged)

    def save(self, settings: Settings) -> None:
        """把配置写回 TOML, 保留文件中已有的注释与排版."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        doc = tomlkit.parse(self.config_path.read_text("utf-8")) if self.config_path.exists() else tomlkit.document()
        for key, value in settings.as_dict().items():
            doc[key] = value
        self.config_path.write_text(tomlkit.dumps(doc), "utf-8")

    def update(self, patch: dict[str, Any]) -> Settings:
        """按 patch 局部更新并落盘, 返回新配置."""
        merged = _deep_merge(self.raw(), patch)
        settings = Settings(**merged)
        self.save(settings)
        return settings
