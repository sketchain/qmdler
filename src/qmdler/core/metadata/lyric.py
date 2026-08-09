"""歌词处理.

``client.lyric.get_lyric`` 返回的 ``lyric`` / ``trans`` / ``roma`` /
``singing_annotations_lyric`` **已由库自动解密** (``GetLyricResponse`` 上有个
``_decrypt_lyrics`` 的 model_validator), 是明文, 不需要自己处理加密.

QRC 的原始形态是 XML 包裹的逐字时间轴::

    <?xml ...?><QrcInfos ...><LyricInfo ...><Lyric_1 ... LyricContent="
    [1000,3000]词(1000,500)词(1500,600)
    "/></LyricInfo></QrcInfos>

每行开头 ``[行起始ms,行时长ms]``, 每个字后跟 ``(字起始ms,字时长ms)``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Literal

#: QRC 外层 XML 里真正装歌词的属性.
_LYRIC_CONTENT_RE = re.compile(r'LyricContent="(.*?)"\s*/?>', re.DOTALL)
#: 行头 [起始, 时长]
_QRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
#: 逐字 词(起始,时长)
_QRC_WORD_RE = re.compile(r"(.*?)\((\d+),(\d+)\)")
#: 标准 LRC 行 [mm:ss.xx]
_LRC_LINE_RE = re.compile(r"^((?:\[\d+:\d+(?:[.:]\d+)?\])+)(.*)$")
_LRC_STAMP_RE = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")
#: LRC 元信息行 [ti:xxx] [ar:xxx] …
_LRC_META_RE = re.compile(r"^\[[a-zA-Z]+:.*\]$")

BilingualMode = Literal["separate_file", "two_lines", "inline"]
OutputFormat = Literal["enhanced_lrc", "line_lrc", "raw_qrc"]


@dataclass(slots=True)
class LyricWord:
    """逐字歌词的一个字/词."""

    text: str
    start_ms: int
    duration_ms: int


@dataclass(slots=True)
class LyricLine:
    """一行歌词."""

    start_ms: int
    duration_ms: int
    text: str
    words: list[LyricWord] = field(default_factory=list)


def format_timestamp(ms: int) -> str:
    """毫秒 → ``mm:ss.xx`` (LRC 标准的百分之一秒)."""
    ms = max(0, ms)
    minutes, remainder = divmod(ms, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


def unwrap_qrc(raw: str) -> str:
    """从 QRC 的 XML 外壳里取出真正的歌词正文.

    非 XML 包裹 (已经是裸时间轴) 时原样返回.
    """
    if not raw:
        return ""
    match = _LYRIC_CONTENT_RE.search(raw)
    if match:
        return html.unescape(match.group(1))
    return raw


def parse_qrc(raw: str) -> list[LyricLine]:
    """解析 QRC 逐字歌词."""
    content = unwrap_qrc(raw)
    lines: list[LyricLine] = []
    for row in content.splitlines():
        stripped = row.strip()
        if not stripped or _LRC_META_RE.match(stripped):
            continue
        match = _QRC_LINE_RE.match(stripped)
        if not match:
            continue
        start, duration, body = int(match.group(1)), int(match.group(2)), match.group(3)
        words = [
            LyricWord(text=word, start_ms=int(word_start), duration_ms=int(word_duration))
            for word, word_start, word_duration in _QRC_WORD_RE.findall(body)
        ]
        text = "".join(word.text for word in words) if words else re.sub(r"\(\d+,\d+\)", "", body)
        lines.append(LyricLine(start_ms=start, duration_ms=duration, text=text.strip(), words=words))
    return lines


def parse_lrc(raw: str) -> list[LyricLine]:
    """解析标准逐行 LRC. 一行多个时间戳会展开成多行."""
    lines: list[LyricLine] = []
    for row in (raw or "").splitlines():
        stripped = row.strip()
        if not stripped or _LRC_META_RE.match(stripped):
            continue
        match = _LRC_LINE_RE.match(stripped)
        if not match:
            continue
        text = match.group(2).strip()
        for minutes, seconds, fraction in _LRC_STAMP_RE.findall(match.group(1)):
            frac = (fraction or "0").ljust(3, "0")[:3]
            start = int(minutes) * 60_000 + int(seconds) * 1000 + int(frac)
            lines.append(LyricLine(start_ms=start, duration_ms=0, text=text))
    lines.sort(key=lambda line: line.start_ms)
    return lines


def parse_any(raw: str) -> list[LyricLine]:
    """自动识别 QRC 还是 LRC."""
    if not raw:
        return []
    if "LyricContent=" in raw or _QRC_LINE_RE.match(raw.strip().splitlines()[0] if raw.strip() else ""):
        parsed = parse_qrc(raw)
        if parsed:
            return parsed
    return parse_lrc(raw)


def to_enhanced_lrc(lines: list[LyricLine]) -> str:
    """输出增强 LRC (A2 格式): 行首 ``[mm:ss.xx]``, 字间 ``<mm:ss.xx>``.

    Salt Player / LX Music 等播放器支持.
    """
    out: list[str] = []
    for line in lines:
        prefix = f"[{format_timestamp(line.start_ms)}]"
        if line.words:
            body = "".join(f"<{format_timestamp(word.start_ms)}>{word.text}" for word in line.words)
            # 行尾再补一个时间戳, 标出最后一个字的结束时刻.
            last = line.words[-1]
            body += f"<{format_timestamp(last.start_ms + last.duration_ms)}>"
        else:
            body = line.text
        out.append(prefix + body)
    return "\n".join(out)


def to_line_lrc(lines: list[LyricLine]) -> str:
    """输出标准逐行 LRC (把 QRC 降级)."""
    return "\n".join(f"[{format_timestamp(line.start_ms)}]{line.text}" for line in lines)


def render(lines: list[LyricLine], output_format: OutputFormat, raw: str = "") -> str:
    """按输出格式渲染."""
    if output_format == "raw_qrc":
        return raw
    if output_format == "enhanced_lrc":
        return to_enhanced_lrc(lines)
    return to_line_lrc(lines)


def merge_bilingual(
    original: list[LyricLine],
    translation: list[LyricLine],
    mode: BilingualMode,
    output_format: OutputFormat,
) -> str:
    """按双语策略合并原文与译文.

    * ``two_lines``: 同一时间戳输出两行 (原文行 + 译文行);
    * ``inline``: 一行内 ``原文 (译文)``;
    * ``separate_file``: 这里只返回原文, 译文由调用方单独存 ``歌名.zh.lrc``.
    """
    if mode == "separate_file" or not translation:
        return render(original, output_format)

    # 按时间戳就近配对: 译文行数常与原文不完全一致.
    trans_map = {line.start_ms: line.text for line in translation}
    trans_starts = sorted(trans_map)

    def nearest(start_ms: int) -> str:
        if not trans_starts:
            return ""
        best = min(trans_starts, key=lambda value: abs(value - start_ms))
        return trans_map[best] if abs(best - start_ms) <= 1500 else ""

    out: list[str] = []
    for line in original:
        translated = nearest(line.start_ms)
        rendered = render([line], output_format)
        if mode == "inline":
            # 译文没有逐字时轴, 只能整体缀在行尾; 逐字部分保持原样.
            out.append(f"{rendered} ({translated})" if translated else rendered)
        else:  # two_lines: 同时间戳双行
            out.append(rendered)
            if translated:
                out.append(f"[{format_timestamp(line.start_ms)}]{translated}")
    return "\n".join(part for part in out if part)


def is_empty(raw: str) -> bool:
    """歌词是否为空 (该歌无歌词)."""
    return not (raw or "").strip()
