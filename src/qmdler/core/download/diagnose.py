# SPDX-License-Identifier: GPL-3.0-or-later
"""``--single`` 诊断模式的记录结构.

把四层校验每一层的**实际判定值**摊开, 而不是只给一个 success/trial 结论 ——
静默失效的检验层是这个项目最怕的东西, 唯一的办法是让每层的输入输出都能被人看到.

**输出脱敏是硬约束**: 凭证、``musickey``、``vkey``、完整 ``purl`` 一律不得出现.
``purl`` 只保留最后一个路径段, 且去掉 query。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 输出里一旦出现这些字样就是泄漏, 单测会守住.
FORBIDDEN_TOKENS: tuple[str, ...] = ("vkey=", "musickey", "guid=", "uin=", "&fromtag", "Q_H_L_", "W_X_")


def safe_purl(purl: str) -> str:
    """只保留 purl 的最后一个路径段, 丢掉 query.

    ``M500abc.mp3?guid=…&vkey=…&uin=…`` → ``M500abc.mp3``
    """
    if not purl:
        return "(空)"
    return purl.split("?", 1)[0].rsplit("/", 1)[-1]


@dataclass
class LayerResult:
    """一层校验的记录."""

    name: str
    #: 这层看的是什么值.
    observed: str
    #: 期望是什么.
    expected: str = ""
    #: 判定结论.
    verdict: str = "ok"
    #: 补充说明.
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        """转为字典."""
        return {
            "name": self.name,
            "observed": self.observed,
            "expected": self.expected,
            "verdict": self.verdict,
            "detail": self.detail,
        }


@dataclass
class SingleDiagnosis:
    """一首歌的完整诊断记录."""

    songmid: str = ""
    title: str = ""
    singers: str = ""
    interval: int = 0
    #: 权益位掩码原值. 位布局无文档, 只记录不解读 —— 攒实测标定素材用.
    sa: int = 0
    status_code: int = 0
    pay: dict[str, int] = field(default_factory=dict)
    available_qualities: dict[str, int] = field(default_factory=dict)
    quality_chain: list[str] = field(default_factory=list)
    requested_quality: str = ""
    requested_start_code: str = ""
    attempted_quality: str = ""
    result_code: int | None = None
    #: 只保留文件名段.
    purl_filename: str = "(空)"
    #: 前缀是从 purl 还是 filename 抠出来的.
    prefix_source: str = ""
    extracted_prefix: str = ""
    #: 逐节点探测记录: ``host→分支/状态码``.
    cdn_attempts: list[str] = field(default_factory=list)
    cdn_used: str = ""
    #: ``head`` / ``range`` / ``rejected`` / ``failed``
    probe_method: str = ""
    probe_status: int = 0
    content_length: int = 0
    expected_size: int = 0
    size_delta_pct: float | None = None
    downloaded_bytes: int = 0
    actual_duration: float = 0.0
    #: 展示用的默认窗口来源 (``file`` 优先).
    trial_window_source: str = ""
    trial_window_ms: tuple[int, int] = (0, 0)
    #: **全部**已知试听窗口 ``[(开始ms, 结束ms, 来源)]`` —— 两者是并列候选,
    #: 第 4 层会逐个比对, 不只看被选中的那个.
    trial_windows: list[tuple[int, int, str]] = field(default_factory=list)
    size_try: int = 0
    layers: list[LayerResult] = field(default_factory=list)
    verdict: str = ""
    reason: str = ""

    def add(self, layer: LayerResult) -> None:
        """记一层."""
        self.layers.append(layer)

    def as_dict(self) -> dict[str, Any]:
        """转为字典 (REST/JSON 输出用)."""
        return {
            "songmid": self.songmid,
            "title": self.title,
            "singers": self.singers,
            "interval": self.interval,
            "sa": self.sa,
            "status": self.status_code,
            "pay": self.pay,
            "available_qualities": self.available_qualities,
            "quality_chain": self.quality_chain,
            "requested_quality": self.requested_quality,
            "requested_start_code": self.requested_start_code,
            "attempted_quality": self.attempted_quality,
            "result_code": self.result_code,
            "purl_filename": self.purl_filename,
            "prefix_source": self.prefix_source,
            "extracted_prefix": self.extracted_prefix,
            "cdn_attempts": self.cdn_attempts,
            "cdn_used": self.cdn_used,
            "probe_method": self.probe_method,
            "probe_status": self.probe_status,
            "content_length": self.content_length,
            "expected_size": self.expected_size,
            "size_delta_pct": self.size_delta_pct,
            "downloaded_bytes": self.downloaded_bytes,
            "actual_duration": self.actual_duration,
            "trial_window_source": self.trial_window_source,
            "trial_window_ms": list(self.trial_window_ms),
            "trial_windows": [
                {"begin_ms": begin, "end_ms": end, "seconds": round((end - begin) / 1000, 1), "source": source}
                for begin, end, source in self.trial_windows
            ],
            "size_try": self.size_try,
            "layers": [layer.as_dict() for layer in self.layers],
            "verdict": self.verdict,
            "reason": self.reason,
        }


def contains_secret(text: str) -> str:
    """检查一段输出里有没有敏感字样, 返回命中的那个 (没有则空串)."""
    lowered = text.lower()
    for token in FORBIDDEN_TOKENS:
        if token.lower() in lowered:
            return token
    return ""
