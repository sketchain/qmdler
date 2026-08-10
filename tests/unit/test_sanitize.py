# SPDX-License-Identifier: GPL-3.0-or-later
"""文件名清洗."""

from __future__ import annotations

import pytest

from qmdler.core.naming.sanitize import (
    RESERVED_NAMES,
    dedupe_path,
    sanitize_relative_path,
    sanitize_segment,
    truncate_bytes,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AC/DC", "AC／DC"),
        ("a:b", "a：b"),
        ("what?", "what？"),
        ('say "hi"', "say ＂hi＂"),
        ("a<b>c", "a＜b＞c"),
        ("a|b", "a｜b"),
        ("a*b", "a＊b"),
        ("back\\slash", "back＼slash"),
    ],
)
def test_illegal_chars_are_replaced_not_deleted(raw: str, expected: str) -> None:
    """非法字符替换成全角, 而不是删掉 —— 保留可读性."""
    assert sanitize_segment(raw) == expected


def test_control_chars_removed() -> None:
    """控制字符直接去掉."""
    assert sanitize_segment("a\x00b\x1fc\x7fd") == "abcd"


def test_trailing_dots_and_spaces_stripped() -> None:
    """Windows 不允许文件名以点或空格结尾."""
    assert sanitize_segment("name...") == "name"
    assert sanitize_segment("name   ") == "name"
    assert sanitize_segment("name. . ") == "name"


def test_whitespace_collapsed() -> None:
    """换行/制表符折成单个空格."""
    assert sanitize_segment("a\n\tb   c") == "a b c"


@pytest.mark.parametrize("reserved", ["CON", "con", "PRN", "NUL", "COM1", "LPT9", "aux"])
def test_reserved_names_get_suffix(reserved: str) -> None:
    """保留名加下划线后缀."""
    assert sanitize_segment(reserved) == f"{reserved}_"


def test_reserved_name_with_extension() -> None:
    """带扩展名的保留名同样要处理."""
    assert sanitize_segment("con.txt") == "con.txt_"


def test_all_reserved_names_covered() -> None:
    """保留名集合完整: CON/PRN/AUX/NUL/COM1-9/LPT1-9 共 22 个."""
    assert len(RESERVED_NAMES) == 4 + 9 + 9


def test_empty_falls_back() -> None:
    """清洗后为空时用兜底名, 绝不返回空串."""
    assert sanitize_segment("   ") == "未命名"
    assert sanitize_segment("...", fallback="未知") == "未知"


def test_truncate_counts_bytes_not_chars() -> None:
    """长度按 UTF-8 字节算: 一个中文 3 字节."""
    text = "中" * 50  # 150 字节
    assert len(truncate_bytes(text, 120).encode()) <= 120
    # 120 / 3 = 40 个字
    assert truncate_bytes(text, 120) == "中" * 40


def test_truncate_does_not_split_multibyte() -> None:
    """截断不能把多字节字符切一半."""
    result = truncate_bytes("中" * 10, 8)  # 8 字节 = 2 个完整字 + 2 字节
    assert result == "中中"
    result.encode()  # 不抛异常


def test_truncation_keeps_extension_intact(entry: object) -> None:
    """截断时扩展名完整保留, 不计入上限."""
    long_name = "歌" * 100
    result = sanitize_segment(long_name, max_bytes=120, extension=".flac")
    assert result.endswith(".flac")
    stem = result[: -len(".flac")]
    assert len(stem.encode()) <= 120


def test_relative_path_drops_empty_segments() -> None:
    """绝不生成空目录名或连续 //."""
    assert sanitize_relative_path("a//b///c") == "a/b/c"
    assert sanitize_relative_path("/a/b/") == "a/b"
    assert sanitize_relative_path("///") == ""
    assert "//" not in sanitize_relative_path("a/ /b")


def test_relative_path_sanitizes_each_segment() -> None:
    """逐段清洗."""
    assert sanitize_relative_path("AC/DC/Back?") == "AC/DC/Back？"


def test_dedupe_appends_numeric_suffix() -> None:
    """撞名时加 (1)(2) 后缀."""
    existing = {"/m/a.flac", "/m/a(1).flac"}
    result = dedupe_path("/m/a.flac", lambda path: str(path) in existing)
    assert result == "/m/a(2).flac"


def test_dedupe_returns_original_when_free() -> None:
    """不冲突时原样返回."""
    assert dedupe_path("/m/a.flac", lambda _: False) == "/m/a.flac"
