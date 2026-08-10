# SPDX-License-Identifier: GPL-3.0-or-later
"""文件名清洗.

统一按 **Windows 最严格标准** 执行, Linux/macOS 也一样 —— 否则用同步盘把库
同步到 Windows 时会整片炸掉.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

#: Windows 非法字符 → 全角等价物. 替换而不是删除, 保留可读性.
ILLEGAL_MAP: dict[str, str] = {
    "\\": "＼",
    "/": "／",
    ":": "：",
    "*": "＊",
    "?": "？",
    '"': "＂",
    "<": "＜",
    ">": "＞",
    "|": "｜",
}

_ILLEGAL_RE = re.compile("[" + re.escape("".join(ILLEGAL_MAP)) + "]")
#: 换行/制表这类空白控制符换成空格, 直接删会把 "a\nb" 粘成 "ab".
_WHITESPACE_CONTROL_RE = re.compile(r"[\t\n\v\f\r]")
#: 其余控制符 (NUL / DEL 等) 没有可读含义, 直接删.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Windows 保留名 (不区分大小写, 且带扩展名也算).
RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)},
)

DEFAULT_MAX_BYTES = 120


def truncate_bytes(text: str, max_bytes: int, encoding: str = "utf-8") -> str:
    """按**字节**截断字符串, 不切碎多字节字符.

    UTF-8 下一个中文占 3 字节, 按字符数截断会在 Windows 的 255 字节路径段限制上翻车.
    """
    if max_bytes <= 0:
        return ""
    raw = text.encode(encoding)
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode(encoding, errors="ignore")


def sanitize_segment(
    name: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    extension: str = "",
    fallback: str = "未命名",
) -> str:
    """清洗单个路径段 (一层目录名, 或不含目录的文件名).

    Args:
        name: 原始名字.
        max_bytes: 截断上限 (UTF-8 字节). 扩展名不计入, 且保证完整保留.
        extension: 扩展名 (含点). 传入时会拼在结果末尾, 并参与保留名判定.
        fallback: 清洗后为空时使用的兜底名.

    Returns:
        清洗后的路径段.
    """
    text = unicodedata.normalize("NFC", name or "")
    text = _WHITESPACE_CONTROL_RE.sub(" ", text)
    text = _CONTROL_RE.sub("", text)
    text = _ILLEGAL_RE.sub(lambda match: ILLEGAL_MAP[match.group()], text)
    # 折叠空白, 顺带干掉换行/制表符留下的空洞.
    text = re.sub(r"\s+", " ", text).strip()
    # Windows 不允许文件名以点或空格结尾.
    text = text.rstrip(". ")

    if not text:
        text = fallback

    # 保留名: CON / con.txt / COM1 都不行, 加下划线后缀规避.
    if text.upper() in RESERVED_NAMES or text.upper().split(".")[0] in RESERVED_NAMES:
        text = f"{text}_"

    text = truncate_bytes(text, max_bytes).rstrip(". ")
    if not text:
        text = fallback

    return f"{text}{extension}"


def sanitize_relative_path(
    path: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    fallback: str = "未命名",
) -> str:
    """清洗一个相对路径, 逐段处理.

    模板里的 ``/`` 就是目录层级; 空段 (连续 ``//``、开头/结尾的 ``/``) 会被丢弃,
    绝不生成空目录名或连续分隔符.
    """
    segments = [segment for segment in re.split(r"[\\/]+", path) if segment.strip(". ")]
    cleaned = [sanitize_segment(segment, max_bytes=max_bytes, fallback=fallback) for segment in segments]
    cleaned = [segment for segment in cleaned if segment]
    return "/".join(cleaned)


def dedupe_path(target: str, exists: Callable[[Path], bool]) -> str:
    """文件名撞车时追加 ``(1)`` ``(2)`` 后缀.

    Args:
        target: 目标路径 (posix 风格或本地风格均可).
        exists: 判定路径是否已存在的可调用对象, 便于测试注入.

    Returns:
        一个不存在的路径.
    """
    path = Path(target)
    if not exists(path):
        return str(path)

    stem, suffix, parent = path.stem, path.suffix, path.parent
    index = 1
    while True:
        candidate = parent / f"{stem}({index}){suffix}"
        if not exists(candidate):
            return str(candidate)
        index += 1
