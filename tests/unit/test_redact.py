# SPDX-License-Identifier: GPL-3.0-or-later
"""日志脱敏."""

from __future__ import annotations

import logging

import pytest

from qmdler.core.redact import MASK, RedactingFilter, redact_any, redact_text


@pytest.mark.parametrize(
    "raw",
    [
        "https://sjy6.stream.qqmusic.qq.com/M500abc.mp3?guid=012e7f69a20845159c88&vkey=A4439DFB3D855A9A&uin=123456",
        "取链失败 ekey=8F2C1A9B0D4E6F7A8B9C0D1E2F3A4B5C",
        "credential musickey=Q_H_L_pP1qR2sT3uV4wX5yZ6",
        '{"access_token": "abcdef123456", "refresh_token": "ghijkl789012"}',
        "authst=xyz789 g_tk=1234567",
    ],
)
def test_sensitive_values_are_masked(raw: str) -> None:
    """常见形态都要被抹掉."""
    cleaned = redact_text(raw)
    for token in ("A4439DFB3D855A9A", "8F2C1A9B0D4E6F7A8B9C0D1E2F3A4B5C", "pP1qR2sT3uV4wX5yZ6", "abcdef123456", "xyz789"):
        assert token not in cleaned, f"{token} 没被抹掉: {cleaned}"


def test_purl_path_survives_so_logs_stay_useful() -> None:
    """抹掉 query 但保留档位前缀 —— 不然日志里看不出请求的是哪个档位."""
    cleaned = redact_text("下载 https://cdn/M500abc.mp3?vkey=DEADBEEFDEADBEEFDEADBEEFDEADBEEF")
    assert "M500abc.mp3" in cleaned
    assert "DEADBEEF" not in cleaned


def test_non_sensitive_text_untouched() -> None:
    """正常日志不能被误伤."""
    message = "完成 晴天 [SQ 无损 FLAC]（已降级：臻品母带 → SQ 无损 FLAC）"
    assert redact_text(message) == message


def test_hex_blob_masked_but_short_hex_kept() -> None:
    """长十六进制串是 vkey 的典型形态; 短的 (比如 task id 前缀) 不该误伤."""
    assert MASK in redact_text("A4439DFB3D855A9A93440F6A59061668937AA45152FCF2D723C13DF7")
    assert redact_text("状态码 104003") == "状态码 104003"
    assert redact_text("DEADBEEF") == "DEADBEEF"


def test_redact_any_walks_containers() -> None:
    """容器要递归进去."""
    result = redact_any({"a": ["vkey=SECRETVALUE1"], "b": ("Q_H_L_xyz",)})
    assert "SECRETVALUE1" not in str(result)
    assert "Q_H_L_xyz" not in str(result)


def test_filter_masks_lazy_format_args() -> None:
    """``logger.info("%s", url)`` 的敏感内容在 args 里, 只抹 msg 会漏."""
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="下载 %s",
        args=("https://cdn/x.mp3?vkey=A4439DFB3D855A9A",),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert "A4439DFB3D855A9A" not in record.getMessage()


def test_filter_masks_exception_text() -> None:
    """异常信息里也可能带 URL."""
    record = logging.LogRecord(
        name="t", level=logging.ERROR, pathname=__file__, lineno=1, msg="boom", args=(), exc_info=None,
    )
    record.exc_text = "HTTPError for https://cdn/x?vkey=A4439DFB3D855A9A"
    RedactingFilter().filter(record)
    assert "A4439DFB3D855A9A" not in (record.exc_text or "")


def test_event_bus_log_is_redacted() -> None:
    """事件也要脱敏 —— 它会推到前端并落进 task_events 表."""
    from qmdler.core.events import EventBus, EventKind

    bus = EventBus()
    bus.log("下载失败 https://cdn/x.mp3?vkey=A4439DFB3D855A9A93440F6A5906")
    events = [event for event in bus.history() if event.kind is EventKind.LOG]
    assert events
    assert "A4439DFB3D855A9A" not in events[-1].payload["message"]
