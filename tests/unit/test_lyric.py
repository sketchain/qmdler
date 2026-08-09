"""QRC → LRC 转换."""

from __future__ import annotations

from qmdler.core.metadata import lyric

QRC_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<QrcInfos><LyricInfo LsrcType="1">'
    '<Lyric_1 LyricType="1" LyricContent="[1000,2000]你(1000,400)好(1400,600)世(2000,500)界(2500,500)\n'
    '[4000,1500]再(4000,700)见(4700,800)\n'
    '"/></LyricInfo></QrcInfos>'
)

LRC_TEXT = "[ti:歌名]\n[00:01.00]第一行\n[00:04.00]第二行\n"


def test_unwrap_qrc_extracts_content() -> None:
    """从 XML 外壳里取出正文."""
    content = lyric.unwrap_qrc(QRC_XML)
    assert content.startswith("[1000,2000]")
    assert "QrcInfos" not in content


def test_unwrap_passthrough_for_bare_content() -> None:
    """已经是裸时间轴时原样返回."""
    assert lyric.unwrap_qrc("[0,100]a(0,100)") == "[0,100]a(0,100)"


def test_parse_qrc_word_timings() -> None:
    """逐字时间轴解析正确."""
    lines = lyric.parse_qrc(QRC_XML)
    assert len(lines) == 2
    first = lines[0]
    assert first.start_ms == 1000
    assert first.duration_ms == 2000
    assert first.text == "你好世界"
    assert [word.text for word in first.words] == ["你", "好", "世", "界"]
    assert first.words[0].start_ms == 1000
    assert first.words[0].duration_ms == 400
    assert first.words[3].start_ms == 2500


def test_format_timestamp_is_centiseconds() -> None:
    """LRC 标准时间戳是百分之一秒."""
    assert lyric.format_timestamp(0) == "00:00.00"
    assert lyric.format_timestamp(1234) == "00:01.23"
    assert lyric.format_timestamp(61_000) == "01:01.00"
    assert lyric.format_timestamp(3_723_450) == "62:03.45"


def test_to_enhanced_lrc_emits_word_tags() -> None:
    """增强 LRC (A2): 行首 [mm:ss.xx], 字间 <mm:ss.xx>."""
    result = lyric.to_enhanced_lrc(lyric.parse_qrc(QRC_XML))
    first = result.splitlines()[0]
    assert first.startswith("[00:01.00]")
    assert "<00:01.00>你" in first
    assert "<00:01.40>好" in first
    # 行尾补最后一个字的结束时刻
    assert first.endswith("<00:03.00>")


def test_to_line_lrc_downgrades_qrc() -> None:
    """逐行 LRC 是 QRC 的降级形态, 不含 <> 标记."""
    result = lyric.to_line_lrc(lyric.parse_qrc(QRC_XML))
    assert result.splitlines()[0] == "[00:01.00]你好世界"
    assert "<" not in result


def test_parse_lrc_skips_metadata_lines() -> None:
    """[ti:] 之类元信息行不算歌词."""
    lines = lyric.parse_lrc(LRC_TEXT)
    assert [line.text for line in lines] == ["第一行", "第二行"]


def test_parse_lrc_expands_multiple_timestamps() -> None:
    """一行多个时间戳展开成多行."""
    lines = lyric.parse_lrc("[00:01.00][00:10.00]重复句")
    assert len(lines) == 2
    assert lines[0].start_ms == 1000
    assert lines[1].start_ms == 10_000


def test_parse_any_autodetects() -> None:
    """自动识别 QRC 还是 LRC."""
    assert len(lyric.parse_any(QRC_XML)) == 2
    assert len(lyric.parse_any(LRC_TEXT)) == 2
    assert lyric.parse_any("") == []


def test_render_raw_qrc_returns_original() -> None:
    """原样保存 .qrc 时不做任何转换."""
    assert lyric.render([], "raw_qrc", raw=QRC_XML) == QRC_XML


def test_bilingual_separate_file_keeps_original_only() -> None:
    """译文单独存文件时, 主文件只有原文."""
    original = lyric.parse_qrc(QRC_XML)
    translation = lyric.parse_lrc("[00:01.00]Hello world\n[00:04.00]Goodbye")
    result = lyric.merge_bilingual(original, translation, "separate_file", "line_lrc")
    assert "Hello world" not in result
    assert "你好世界" in result


def test_bilingual_two_lines() -> None:
    """同时间戳双行."""
    original = lyric.parse_qrc(QRC_XML)
    translation = lyric.parse_lrc("[00:01.00]Hello world\n[00:04.00]Goodbye")
    result = lyric.merge_bilingual(original, translation, "two_lines", "line_lrc")
    lines = result.splitlines()
    assert lines[0] == "[00:01.00]你好世界"
    assert lines[1] == "[00:01.00]Hello world"
    assert lines[2] == "[00:04.00]再见"
    assert lines[3] == "[00:04.00]Goodbye"


def test_bilingual_inline() -> None:
    """一行内 原文 (译文)."""
    original = lyric.parse_qrc(QRC_XML)
    translation = lyric.parse_lrc("[00:01.00]Hello world\n[00:04.00]Goodbye")
    result = lyric.merge_bilingual(original, translation, "inline", "line_lrc")
    assert result.splitlines()[0] == "[00:01.00]你好世界 (Hello world)"


def test_bilingual_pairs_by_nearest_timestamp() -> None:
    """译文行数与原文不一致时按时间戳就近配对, 差太远就不配."""
    original = lyric.parse_qrc(QRC_XML)
    translation = lyric.parse_lrc("[00:01.10]Hello")  # 差 100ms, 应配上
    result = lyric.merge_bilingual(original, translation, "inline", "line_lrc")
    lines = result.splitlines()
    assert "(Hello)" in lines[0]
    assert "(" not in lines[1], "第二行离得太远, 不该硬配"


def test_empty_detection() -> None:
    """空歌词检测."""
    assert lyric.is_empty("")
    assert lyric.is_empty("   \n ")
    assert not lyric.is_empty("[00:01.00]x")
