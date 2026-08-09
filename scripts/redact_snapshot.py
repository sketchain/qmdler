#!/usr/bin/env python3
"""把真实 API 响应脱敏成可入库的快照.

用法::

    # 单个文件
    python scripts/redact_snapshot.py raw.json -o tests/fixtures/api/query_song.json

    # 从 stdin
    cat raw.json | python scripts/redact_snapshot.py - -o tests/fixtures/api/song_urls.json

    # 只看结果不落盘
    python scripts/redact_snapshot.py raw.json --dry-run

**保留结构与字段类型, 只抹内容** —— 快照的价值在于「真实响应长什么样」,
把 ``vkey`` 换成 ``"<redacted:vkey>"`` 不影响这一点, 但换成 ``null`` 就会让
解析代码走上与线上不同的分支, 所以字符串一律换成同类型的占位串, 数字换成 0.

脱敏后会再跑一遍自检: 输出里若还残留任何敏感样式 (长十六进制串、``Q_H_L_``
开头的 key 等) 就直接失败, 不落盘。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: 按**键名**脱敏. 命中即替换, 不看值长什么样.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "vkey",
        "ekey",
        "purl",
        "musickey",
        "uin",
        "encrypt_uin",
        "encryptuin",
        "encryptedUin".lower(),
        "openid",
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "refresh_key",
        "refreshkey",
        "authst",
        "qqmusic_key",
        "str_musicid",
        "musicid",
        "guid",
        "unionid",
        "qrsig",
        "p_skey",
        "skey",
        "sig",
        "g_tk",
        "loginuin",
        "hostuin",
        "userid",
        # CDN dispatch 的 keepalive 探针文件是一条**完整带 vkey/guid 的 URL**,
        # 不是路径 —— 落盘自检抓到过, 别漏.
        "test_file",
        "keepalivefile",
        # 第三方的身份信息. 不是凭证, 但没有理由把别人的昵称和头像地址
        # 提交进公开仓库 —— 歌单详情里就带着歌单作者的这两项.
        # 头像 URL 里还有 32 位十六进制哈希, 会直接把落盘自检打红.
        "headurl",
        "avatar",
        "portrait",
        "nick",
        "nickname",
    },
)

#: ``purl`` 特殊: 保留档位前缀 (试听检测就靠它), 抹掉 query 与 mid.
_PURL_RE = re.compile(r"^([0-9A-Za-z]{4})[0-9A-Za-z]+(\.[0-9A-Za-z]+)")

#: 落盘前的自检: 命中任何一条就认为没脱干净.
LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 大小写都要认: 头像 URL 里的哈希是小写的, 只认大写就会漏.
    ("疑似 vkey/ekey（32 位以上十六进制）", re.compile(r"\b[0-9A-Fa-f]{32,}\b")),
    ("QQ musickey", re.compile(r"Q_H_L_[0-9A-Za-z_-]+")),
    ("微信 musickey", re.compile(r"W_X_[0-9A-Za-z_-]+")),
    ("URL 里的 vkey 参数", re.compile(r"vkey=[0-9A-Za-z]+")),
    ("URL 里的 guid 参数", re.compile(r"guid=[0-9A-Za-z]{8,}")),
)


def redact_value(key: str, value: Any) -> Any:
    """按键名脱敏单个值, 保持类型不变."""
    lowered = key.lower()
    if lowered not in SENSITIVE_KEYS:
        return value

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        return 0.0
    if isinstance(value, str):
        if not value:
            return value
        if lowered == "purl":
            # 保留四字符档位编码 —— 第 1 层校验就是比对它。
            match = _PURL_RE.match(value.split("?", 1)[0].rsplit("/", 1)[-1])
            if match:
                return f"{match.group(1)}<redacted>{match.group(2)}"
            return "<redacted:purl>"
        return f"<redacted:{lowered}>"
    if isinstance(value, list):
        return [redact_value(key, item) for item in value]
    return value


def redact(node: Any, key: str = "") -> Any:
    """递归脱敏."""
    if isinstance(node, dict):
        return {name: redact(child, name) for name, child in node.items()}
    if isinstance(node, list):
        return [redact(item, key) for item in node]
    return redact_value(key, node)


def find_leaks(text: str) -> list[str]:
    """自检: 返回命中的泄漏说明."""
    leaks: list[str] = []
    for label, pattern in LEAK_PATTERNS:
        match = pattern.search(text)
        if match:
            leaks.append(f"{label}: {match.group()[:24]}…")
    return leaks


def main() -> int:
    """入口."""
    parser = argparse.ArgumentParser(description="把真实 API 响应脱敏成可入库的快照")
    parser.add_argument("source", help="原始 JSON 文件路径，或 - 表示 stdin")
    parser.add_argument("-o", "--output", help="输出路径（不给就打到 stdout）")
    parser.add_argument("--dry-run", action="store_true", help="只检查不落盘")
    args = parser.parse_args()

    raw = sys.stdin.read() if args.source == "-" else Path(args.source).read_text("utf-8")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        print(f"输入不是合法 JSON: {exc}", file=sys.stderr)
        return 2

    cleaned = redact(data)
    text = json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True)

    leaks = find_leaks(text)
    if leaks:
        print("脱敏自检未通过，拒绝落盘：", file=sys.stderr)
        for leak in leaks:
            print(f"  · {leak}", file=sys.stderr)
        print("请把对应字段加进 SENSITIVE_KEYS 后重跑。", file=sys.stderr)
        return 1

    if args.dry_run or not args.output:
        print(text)
        return 0

    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", "utf-8")
    print(f"已写入 {target}（脱敏自检通过）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
