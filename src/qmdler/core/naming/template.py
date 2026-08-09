"""文件名 / 目录模板渲染.

文件名模板和目录结构模板共用同一套占位符. 目录模板中的 ``/`` 即目录层级.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config.schema import NamingConfig
from ..models import SongEntry, quality_label
from .sanitize import sanitize_relative_path, sanitize_segment

#: 全部占位符 → 说明. UI 直接拿这个渲染帮助面板.
PLACEHOLDERS: dict[str, str] = {
    "歌名": "歌曲标题",
    "歌手": "全部歌手, 用连接符拼接",
    "专辑": "专辑名",
    "专辑艺术家": "专辑艺术家 (取第一位歌手)",
    "作曲": "作曲 (需要额外请求, 勾选后才拉取)",
    "作词": "作词 (需要额外请求, 勾选后才拉取)",
    "年份": "发行年份",
    "曲序": "专辑内音轨号, 两位补零",
    "碟号": "碟片号",
    "音质": "实际下载到的音质档位",
    "songmid": "歌曲 MID, 加上它可彻底避免同名冲突",
    "歌单名": "任务/歌单名称",
}

#: 预设模板.
PRESET_FILE_TEMPLATES: list[str] = [
    "{歌名} - {歌手}",
    "{歌手} - {歌名}",
    "{曲序}. {歌名} - {歌手}",
    "{歌名} - {歌手} [{音质}]",
    "{歌名} - {歌手} ({songmid})",
]

PRESET_DIR_TEMPLATES: list[str] = [
    "",
    "{歌单名}",
    "{歌手}/{专辑}",
    "{歌单名}/{专辑}",
]

#: 组合预设 (目录 + 文件), 对应规范里列出的四种.
PRESET_COMBOS: list[dict[str, str]] = [
    {"name": "歌名 - 歌手", "dir": "", "file": "{歌名} - {歌手}"},
    {"name": "歌手 - 歌名", "dir": "", "file": "{歌手} - {歌名}"},
    {"name": "歌单/曲序. 歌名 - 歌手", "dir": "{歌单名}", "file": "{曲序}. {歌名} - {歌手}"},
    {"name": "歌手/专辑/曲序. 歌名", "dir": "{歌手}/{专辑}", "file": "{曲序}. {歌名}"},
]

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")


@dataclass(slots=True)
class RenderContext:
    """渲染一条路径需要的全部上下文."""

    entry: SongEntry
    playlist_name: str = ""
    quality: str = ""
    composer: str = ""
    lyricist: str = ""
    genre: str = ""
    position: int = 0


def unknown_placeholders(template: str) -> list[str]:
    """返回模板里用到的未知占位符."""
    return [token for token in _TOKEN_RE.findall(template) if token not in PLACEHOLDERS]


class TemplateRenderer:
    """按配置渲染模板."""

    def __init__(self, config: NamingConfig) -> None:
        """初始化."""
        self.config = config

    # -- 取值 ---------------------------------------------------------------- #

    def values(self, ctx: RenderContext) -> dict[str, str]:
        """算出每个占位符的取值.

        占位符取不到值时一律用兜底默认值, 绝不返回空串 —— 否则会生成空目录名.
        """
        entry = ctx.entry
        config = self.config
        singers = [name for name in entry.singer_names if name.strip()]
        singer_text = config.singer_separator.join(singers) if singers else config.unknown_artist

        track_no = ctx.position or entry.track_no
        year = (entry.year or "").strip()

        return {
            "歌名": entry.title.strip() or config.unknown_value,
            "歌手": singer_text,
            "专辑": entry.album_name.strip() or config.unknown_album,
            "专辑艺术家": (singers[0] if singers else config.unknown_artist),
            "作曲": ctx.composer.strip() or config.unknown_value,
            "作词": ctx.lyricist.strip() or config.unknown_value,
            "年份": (year.split("-")[0] if year else config.unknown_value),
            "曲序": f"{track_no:02d}" if track_no > 0 else "00",
            "碟号": str(entry.disc_no if entry.disc_no > 0 else 1),
            "音质": quality_label(ctx.quality) if ctx.quality else config.unknown_value,
            "songmid": entry.songmid or config.unknown_value,
            "歌单名": ctx.playlist_name.strip() or config.unknown_value,
        }

    def render(self, template: str, ctx: RenderContext) -> str:
        """渲染模板 (未清洗). 未知占位符原样保留, 方便用户看出自己打错了."""
        values = self.values(ctx)

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            return values.get(key, match.group(0))

        return _TOKEN_RE.sub(replace, template)

    # -- 完整路径 ------------------------------------------------------------- #

    def relative_path(self, ctx: RenderContext, extension: str) -> str:
        """渲染出相对根目录的完整路径 (含扩展名), 已清洗.

        Args:
            ctx: 渲染上下文.
            extension: 文件扩展名 (含点).
        """
        max_bytes = self.config.max_segment_bytes
        directory = sanitize_relative_path(
            self.render(self.config.dir_template, ctx),
            max_bytes=max_bytes,
            fallback=self.config.unknown_value,
        )
        filename = sanitize_segment(
            # 文件名模板里的 / 不该产生目录层级, 折成连接符.
            self.render(self.config.file_template, ctx).replace("/", "／"),
            max_bytes=max_bytes,
            extension=extension,
            fallback=self.config.unknown_value,
        )
        return f"{directory}/{filename}" if directory else filename

    def preview(self, ctx: RenderContext, extension: str, root: str) -> dict[str, Any]:
        """给 UI 的实时预览: 模板改一个字就重新算一次完整路径."""
        relative = self.relative_path(ctx, extension)
        root_clean = root.rstrip("/\\")
        return {
            "root": root_clean,
            "relative": relative,
            "full": f"{root_clean}/{relative}",
            "values": self.values(ctx),
            "unknown_dir_placeholders": unknown_placeholders(self.config.dir_template),
            "unknown_file_placeholders": unknown_placeholders(self.config.file_template),
        }
