# SPDX-License-Identifier: GPL-3.0-or-later
"""日志脱敏.

凭证、``musickey``、``vkey``、完整 ``purl`` 绝不能出现在日志里 —— 日志会被
贴进 issue、被同步到网盘、被 CI 收走. 靠「写日志时记得别带上」是守不住的,
所以在 logging 层加一道过滤器兜底.

过滤器同时处理 ``record.msg`` 与 ``record.args`` —— 惰性格式化 (``%s``) 的参数
不在 msg 里, 只抹 msg 会漏.
"""

from __future__ import annotations

import logging
import re
from typing import Any

MASK = "<redacted>"

#: 按模式抹. 顺序无关, 逐条 sub.
PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # URL query 里的敏感参数
    ("vkey", re.compile(r"(?i)\bvkey=[^&\s\"']+"), f"vkey={MASK}"),
    ("ekey", re.compile(r"(?i)\bekey=[^&\s\"']+"), f"ekey={MASK}"),
    ("guid", re.compile(r"(?i)\bguid=[^&\s\"']+"), f"guid={MASK}"),
    ("uin", re.compile(r"(?i)\buin=[^&\s\"']+"), f"uin={MASK}"),
    ("authst", re.compile(r"(?i)\bauthst=[^&\s\"']+"), f"authst={MASK}"),
    ("g_tk", re.compile(r"(?i)\bg_tk=[^&\s\"']+"), f"g_tk={MASK}"),
    # 登录态 key 本体
    ("qq musickey", re.compile(r"Q_H_L_[0-9A-Za-z_\-]+"), f"Q_H_L_{MASK}"),
    ("wx musickey", re.compile(r"W_X_[0-9A-Za-z_\-]+"), f"W_X_{MASK}"),
    # JSON / dict 字面量里的敏感键
    (
        "json keys",
        re.compile(
            r"(?i)([\"']?(?:musickey|access_token|refresh_token|refresh_key|openid|unionid|encrypt_uin"
            r"|encryptUin|qrsig|p_skey|skey)[\"']?\s*[:=]\s*)[\"']?[^\s,}\"']+[\"']?",
        ),
        rf"\1{MASK}",
    ),
    # 裸的长十六进制串 (vkey / ekey 的典型形态)
    ("hex blob", re.compile(r"\b[0-9A-F]{32,}\b"), MASK),
)


def redact_text(text: str) -> str:
    """抹掉一段文本里的敏感信息."""
    for _label, pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_any(value: Any) -> Any:
    """递归抹 —— 字符串直接处理, 容器逐个进去."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, tuple):
        return tuple(redact_any(item) for item in value)
    if isinstance(value, list):
        return [redact_any(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_any(item) for key, item in value.items()}
    return value


class RedactingFilter(logging.Filter):
    """给根 logger 挂的脱敏过滤器.

    同时处理 ``msg`` 与 ``args``: ``logger.info("url=%s", url)`` 的敏感内容
    在 ``args`` 里, 只抹 ``msg`` 抓不到.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """就地脱敏, 永远放行."""
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = redact_any(record.args)
        # 异常信息里也可能带 URL
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True
