# SPDX-License-Identifier: GPL-3.0-or-later
"""配置、模板预览、音质表、路径校验."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from ...core.config.schema import NamingConfig, Settings
from ...core.fsutil import check_path, list_directories
from ...core.models import (
    DEFAULT_QUALITY_CHAIN,
    QUALITY_TABLE,
    SongEntry,
    quality_caveat,
    quality_extension,
    quality_label,
)
from ...core.naming.template import (
    PLACEHOLDERS,
    PRESET_COMBOS,
    PRESET_DIR_TEMPLATES,
    PRESET_FILE_TEMPLATES,
    RenderContext,
    TemplateRenderer,
)
from ...core.sources.service import DEFAULT_LIMIT
from ..deps import Ctx

router = APIRouter(prefix="/config", tags=["config"])


class ConfigPatch(BaseModel):
    """局部更新配置."""

    patch: dict[str, Any]


class PresetRequest(BaseModel):
    """切换 / 保存预设."""

    name: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class PreviewRequest(BaseModel):
    """模板实时预览."""

    dir_template: str = ""
    file_template: str = ""
    singer_separator: str = "、"
    #: 队列第一首歌的快照; 缺省用一条示例数据.
    song: dict[str, Any] | None = None
    playlist_name: str = "示例歌单"
    quality: str = "FLAC"
    root: str = ""


class PathCheckRequest(BaseModel):
    """校验保存目录."""

    path: str
    create: bool = False


_SAMPLE_SONG: dict[str, Any] = {
    "songmid": "003w2xz20QlUZt",
    "songid": 1234567,
    "title": "示例歌曲 (Live)",
    "singers": [{"mid": "0025NhlN2yWrP4", "name": "示例歌手"}, {"mid": "", "name": "合作歌手"}],
    "album_name": "示例专辑",
    "album_mid": "000abcdefg",
    "media_mid": "003w2xz20QlUZt",
    "song_type": 0,
    "interval": 245,
    "track_no": 3,
    "disc_no": 1,
    "year": "2021-08-06",
    "status": 0,
    "sa": 0,
    "sizes": {"FLAC": 38412345, "MP3_320": 9812345, "MP3_128": 3924937},
    "pay": {"pay_month": 1, "pay_play": 0, "pay_down": 1, "price_track": 0},
    "size_try": 1200000,
    "try_begin_ms": 60000,
    "try_end_ms": 90000,
    "vs": [],
    "vi": [],
    "vf": [],
}


@router.get("")
async def get_config(context: Ctx) -> dict[str, Any]:
    """当前生效的配置.

    附带 ``limits``: 拉取上限的默认值与最大值。两个前端都从这里读，
    **不各自写死一个数** —— 之前 WebUI 写 2000/10000、TUI 写 2000、
    后端是 2000，改一处就会漏两处。
    """
    return {
        **context.settings.as_dict(),
        "limits": {"fetch_default": DEFAULT_LIMIT, "fetch_max": DEFAULT_LIMIT},
    }


@router.patch("")
async def patch_config(payload: ConfigPatch, context: Ctx) -> dict[str, Any]:
    """局部更新并落盘."""
    try:
        settings = context.config_store.update(payload.patch)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.json()) from exc
    await context.reload_settings(settings)
    return settings.as_dict()


@router.get("/presets")
async def list_presets(context: Ctx) -> dict[str, Any]:
    """全部配置预设."""
    return {"active": context.settings.preset, "presets": context.config_store.all_presets()}


@router.post("/presets/activate")
async def activate_preset(payload: PresetRequest, context: Ctx) -> dict[str, Any]:
    """一键切换预设."""
    presets = context.config_store.all_presets()
    if payload.name != "default" and payload.name not in presets:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"预设不存在: {payload.name}")
    # 一次性把预设的值写进配置文件（见 ConfigStore 的模块 docstring）：
    # 之后用户在设置界面改动这些字段能真正生效，不会重启后被预设盖回去。
    settings = context.config_store.activate_preset(payload.name)
    await context.reload_settings(settings)
    return settings.as_dict()


@router.post("/presets")
async def save_preset(payload: PresetRequest, context: Ctx) -> dict[str, Any]:
    """保存一个用户预设."""
    context.config_store.save_preset(payload.name, payload.overrides)
    return {"ok": True, "presets": list(context.config_store.all_presets())}


@router.delete("/presets/{name}")
async def delete_preset(name: str, context: Ctx) -> dict[str, bool]:
    """删除用户预设."""
    return {"ok": context.config_store.delete_preset(name)}


@router.get("/qualities")
async def qualities() -> dict[str, Any]:
    """音质档位表, 供拖拽排序界面使用."""
    return {
        "items": [
            {
                "code": code,
                "label": label,
                "extension": quality_extension(code),
                "caveat": quality_caveat(code),
            }
            for code, (label, _) in QUALITY_TABLE.items()
        ],
        "default_chain": DEFAULT_QUALITY_CHAIN,
        "notes": {
            "encrypted": (
                "加密档位走 GetEVkey，拿回的是 .mflac / .mgg 等加密流，"
                "本工具不解密，下载下来无法播放，因此默认不在列表中。"
            ),
            "special": "试听片段 / 伴奏 / AI 演奏 / 铃声属于特殊档位，默认不暴露。",
            "acc96": "ACC_96 对部分 VIP 歌曲即使无会员也能拿到试听级链接，可作兜底但会标注为试听。",
            "vip": (
                "会员等级不是万能的：单曲付费、该音质文件本就不存在、地区版权限制、"
                "设备受限，这几种情况与会员等级无关。"
            ),
        },
    }


@router.get("/templates")
async def templates() -> dict[str, Any]:
    """占位符说明与预设模板."""
    return {
        "placeholders": PLACEHOLDERS,
        "file_presets": PRESET_FILE_TEMPLATES,
        "dir_presets": PRESET_DIR_TEMPLATES,
        "combos": PRESET_COMBOS,
        "hint": "同名冲突频繁（现场版 / Remix）时，模板里加上 {songmid} 可彻底避免。",
    }


@router.post("/templates/preview")
async def preview_template(payload: PreviewRequest, context: Ctx) -> dict[str, Any]:
    """实时预览: 模板改一个字就刷新完整路径."""
    base = context.settings.naming
    config = NamingConfig(
        dir_template=payload.dir_template if payload.dir_template is not None else base.dir_template,
        file_template=payload.file_template or base.file_template,
        singer_separator=payload.singer_separator or base.singer_separator,
        max_segment_bytes=base.max_segment_bytes,
        unknown_album=base.unknown_album,
        unknown_artist=base.unknown_artist,
        unknown_value=base.unknown_value,
    )
    entry = SongEntry.from_dict(payload.song or _SAMPLE_SONG)
    renderer = TemplateRenderer(config)
    ctx = RenderContext(
        entry=entry,
        playlist_name=payload.playlist_name,
        quality=payload.quality,
        composer="示例作曲",
        lyricist="示例作词",
    )
    return {
        **renderer.preview(ctx, quality_extension(payload.quality), payload.root or context.settings.paths.save_root),
        "quality_label": quality_label(payload.quality),
    }


@router.post("/path/check")
async def check_save_path(payload: PathCheckRequest) -> dict[str, Any]:
    """校验保存目录: 存在性、可写性、剩余空间."""
    return check_path(payload.path, create=payload.create).as_dict()


@router.get("/path/list")
async def list_dirs(path: str = "~") -> dict[str, Any]:
    """列出子目录, 供 TUI 的目录选择器补全."""
    return {"path": path, "directories": list_directories(path)}


@router.get("/schema")
async def config_schema() -> dict[str, Any]:
    """配置的 JSON Schema, 前端可据此自动生成表单."""
    return Settings.model_json_schema()
