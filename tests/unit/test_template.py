# SPDX-License-Identifier: GPL-3.0-or-later
"""模板渲染."""

from __future__ import annotations

from qmdler.core.config.schema import NamingConfig
from qmdler.core.models import SongEntry
from qmdler.core.naming.template import (
    PLACEHOLDERS,
    PRESET_COMBOS,
    RenderContext,
    TemplateRenderer,
    unknown_placeholders,
)


def _renderer(**overrides: object) -> TemplateRenderer:
    return TemplateRenderer(NamingConfig(**overrides))  # type: ignore[arg-type]


def test_all_documented_placeholders_resolve(entry: SongEntry) -> None:
    """规范里列出的每个占位符都要有取值."""
    renderer = _renderer()
    values = renderer.values(RenderContext(entry=entry, playlist_name="我的歌单", quality="FLAC"))
    assert set(values) == set(PLACEHOLDERS)
    assert all(value for value in values.values()), "占位符取值不允许为空"


def test_basic_render(entry: SongEntry) -> None:
    """基础渲染."""
    renderer = _renderer(file_template="{歌名} - {歌手}")
    ctx = RenderContext(entry=entry, playlist_name="歌单")
    assert renderer.render("{歌名} - {歌手}", ctx) == "测试歌曲 - 歌手甲、歌手乙"


def test_singer_separator_is_configurable(entry: SongEntry) -> None:
    """多歌手连接符可配, 默认不用 / 以免与路径分隔符冲突."""
    renderer = _renderer(singer_separator=", ")
    ctx = RenderContext(entry=entry)
    assert renderer.render("{歌手}", ctx) == "歌手甲, 歌手乙"


def test_track_number_is_zero_padded(entry: SongEntry) -> None:
    """曲序两位补零."""
    assert _renderer().render("{曲序}", RenderContext(entry=entry)) == "03"


def test_year_takes_only_the_year_part(entry: SongEntry) -> None:
    """{年份} 从 YYYY-MM-DD 里只取年."""
    assert _renderer().render("{年份}", RenderContext(entry=entry)) == "2021"


def test_quality_placeholder_uses_label(entry: SongEntry) -> None:
    """{音质} 展示的是中文档位名."""
    result = _renderer().render("{音质}", RenderContext(entry=entry, quality="FLAC"))
    assert "FLAC" in result


def test_missing_values_use_fallbacks(minimal_entry: SongEntry) -> None:
    """取不到值时用兜底默认值, 不产生空串."""
    renderer = _renderer()
    values = renderer.values(RenderContext(entry=minimal_entry))
    assert values["专辑"] == "未知专辑"
    assert values["歌手"] == "未知艺术家"
    assert values["专辑艺术家"] == "未知艺术家"
    assert values["歌名"] == "未知"


def test_relative_path_has_no_empty_segments(minimal_entry: SongEntry) -> None:
    """兜底值参与拼路径时也不能产生空目录名或 //."""
    renderer = _renderer(dir_template="{歌单名}/{专辑}", file_template="{歌名} - {歌手}")
    path = renderer.relative_path(RenderContext(entry=minimal_entry), ".flac")
    assert "//" not in path
    assert not path.startswith("/")
    assert path.endswith(".flac")


def test_slash_in_dir_template_creates_levels(entry: SongEntry) -> None:
    """目录模板里的 / 就是目录层级."""
    renderer = _renderer(dir_template="{歌手}/{专辑}", file_template="{曲序}. {歌名}")
    path = renderer.relative_path(RenderContext(entry=entry), ".flac")
    assert path == "歌手甲、歌手乙/测试专辑/03. 测试歌曲.flac"


def test_slash_in_file_template_does_not_create_levels(entry: SongEntry) -> None:
    """文件名模板里的 / 折成全角, 不产生目录."""
    renderer = _renderer(dir_template="", file_template="{歌手}/{歌名}")
    path = renderer.relative_path(RenderContext(entry=entry), ".mp3")
    assert path.count("/") == 0
    assert "／" in path


def test_unknown_placeholder_is_preserved_and_reported(entry: SongEntry) -> None:
    """未知占位符原样保留, 并能被检出."""
    renderer = _renderer()
    assert renderer.render("{不存在}", RenderContext(entry=entry)) == "{不存在}"
    assert unknown_placeholders("{歌名}-{不存在}") == ["不存在"]


def test_long_names_are_truncated_per_segment(entry: SongEntry) -> None:
    """每一段都按字节上限截断."""
    entry.title = "长" * 200
    renderer = _renderer(dir_template="", file_template="{歌名}", max_segment_bytes=120)
    path = renderer.relative_path(RenderContext(entry=entry), ".flac")
    stem = path[: -len(".flac")]
    assert len(stem.encode()) <= 120


def test_illegal_chars_sanitized_in_path(entry: SongEntry) -> None:
    """模板渲染出的非法字符会被清洗掉."""
    entry.title = "A/B:C"
    path = _renderer(dir_template="", file_template="{歌名}").relative_path(RenderContext(entry=entry), ".mp3")
    assert path == "A／B：C.mp3"


def test_preset_combos_all_render(entry: SongEntry) -> None:
    """四个预设模板都能渲染出合法路径."""
    for combo in PRESET_COMBOS:
        renderer = _renderer(dir_template=combo["dir"], file_template=combo["file"])
        path = renderer.relative_path(RenderContext(entry=entry, playlist_name="歌单"), ".flac")
        assert path
        assert "//" not in path
        assert path.endswith(".flac")


def test_preview_returns_full_path(entry: SongEntry) -> None:
    """预览给出完整路径, 供 UI 实时刷新."""
    renderer = _renderer(dir_template="{歌单名}", file_template="{歌名}")
    preview = renderer.preview(RenderContext(entry=entry, playlist_name="歌单"), ".flac", "/music")
    assert preview["full"] == "/music/歌单/测试歌曲.flac"
    assert preview["root"] == "/music"


def test_songmid_placeholder_disambiguates(entry: SongEntry) -> None:
    """{songmid} 能彻底避免同名 (现场版 / Remix 常见)."""
    renderer = _renderer(dir_template="", file_template="{歌名} ({songmid})")
    path = renderer.relative_path(RenderContext(entry=entry), ".flac")
    assert entry.songmid in path
