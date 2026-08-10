# SPDX-License-Identifier: GPL-3.0-or-later
"""音质选择.

分层判断, 一层都不能省:

0. ``Song.status`` —— 1 下架 / 2 待审核, 列表阶段直接判 ``unavailable``,
   不必浪费一次取链请求. (3 = 仅限部分区域播放, 只做标注, 仍然尝试.)
1. ``file.size_* == 0`` —— 该音质文件根本不存在, 从候选里排除.
2. ``track.pay`` —— **只作 UI 提示, 不参与过滤**. 本库走的是播放链路, 服务端按
   播放权益判定, ``pay_down == 1`` 的曲目仍可能正常返回完整播放文件.
3. ``UrlinfoItem.result`` + 试听检测 —— 最终裁决在 ``download`` 包里.

关于 ``Song.sa``: 它的 docstring 说是「64 位权益位掩码, 标识高级权益及试听状态」,
但**具体位布局在库源码和官方文档里都没有定义** (官方文档站就是 mkdocstrings 渲染
的同一份 docstring). 猜测位含义会造成静默误判 —— 明明能下的歌被跳过, 或者试听被
当成完整文件. 因此这里只把原值透传给 UI 展示, 不参与任何判断.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import QUALITY_TABLE, SongEntry, quality_extension, quality_label


@dataclass(slots=True)
class QualityCandidate:
    """一个候选档位."""

    code: str
    label: str
    size: int
    extension: str

    def as_dict(self) -> dict[str, Any]:
        """转为 REST 出参."""
        return {"code": self.code, "label": self.label, "size": self.size, "extension": self.extension}


@dataclass(slots=True)
class QualityPlan:
    """一首歌的选档计划."""

    #: 按用户优先级链排好序的候选档位.
    candidates: list[QualityCandidate]
    #: 判重键用的档位 = 链上最高的可用档位.
    requested: str
    #: UI 提示: 该曲目可能受限的原因.
    warnings: list[str]
    #: 列表阶段即可确定拿不到文件.
    unavailable_reason: str = ""

    @property
    def available(self) -> bool:
        """是否还有候选可以尝试."""
        return bool(self.candidates) and not self.unavailable_reason

    def as_dict(self) -> dict[str, Any]:
        """转为 REST 出参."""
        return {
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "requested": self.requested,
            "requested_label": quality_label(self.requested) if self.requested else "",
            "warnings": self.warnings,
            "unavailable_reason": self.unavailable_reason,
        }


def pay_warnings(entry: SongEntry) -> list[str]:
    """把 ``pay`` 字段翻译成 UI 提示文案.

    这些**只是提示**: 最终能不能拿到文件以 ``result`` 和试听检测为准.
    """
    pay = entry.pay
    notes: list[str] = []
    if pay.get("price_track", 0) > 0:
        notes.append(f"单曲付费 ¥{pay['price_track'] / 100:.2f}")
    if pay.get("pay_play", 0) == 1:
        notes.append("需付费播放")
    if pay.get("pay_down", 0) == 1:
        notes.append("需付费下载（走播放链路仍可能可用）")
    if pay.get("pay_month", 0) == 1:
        notes.append("需绿钻/付费包")
    if pay.get("time_free", 0) > 0:
        notes.append("限免中")
    if entry.is_region_limited:
        notes.append("仅限部分区域播放")
    return notes


def build_plan(entry: SongEntry, chain: list[str], *, on_all_unavailable: str = "skip") -> QualityPlan:
    """按优先级链为一首歌生成选档计划.

    Args:
        entry: 歌曲快照.
        chain: 用户拖拽排序后的优先级链, 从高到低.
        on_all_unavailable: 链上全部不可用时的行为 —— ``skip`` 跳过并记录,
            ``lowest`` 用最低可用档位兜底.
    """
    warnings = pay_warnings(entry)

    if entry.is_offline:
        reason = "已下架" if entry.status == 1 else "待审核"
        return QualityPlan(candidates=[], requested="", warnings=warnings, unavailable_reason=reason)

    sizes = entry.available_qualities
    candidates = [
        QualityCandidate(
            code=code,
            label=quality_label(code),
            size=sizes[code],
            extension=quality_extension(code),
        )
        for code in chain
        if code in sizes
    ]

    if not candidates and on_all_unavailable == "lowest":
        # 兜底: 在全部档位里挑一个体积最小的可用档位.
        # QUALITY_TABLE 是按音质由高到低排的, 所以倒序找第一个存在的.
        for code in reversed(list(QUALITY_TABLE)):
            if code in sizes:
                candidates = [
                    QualityCandidate(
                        code=code,
                        label=quality_label(code),
                        size=sizes[code],
                        extension=quality_extension(code),
                    ),
                ]
                warnings.append(f"链上档位均不可用，兜底使用最低可用档位 {quality_label(code)}")
                break

    if not candidates:
        return QualityPlan(
            candidates=[],
            requested="",
            warnings=warnings,
            unavailable_reason="没有任何可用音质（该曲目所有档位的文件大小均为 0）",
        )

    return QualityPlan(candidates=candidates, requested=candidates[0].code, warnings=warnings)


def annotate(entry: SongEntry, chain: list[str], *, on_all_unavailable: str = "skip") -> dict[str, Any]:
    """列表阶段的预先标注: 可用的最高音质 / 是否付费 / 是否无版权.

    别等下载时才发现 —— 这些信息在拉列表时就已经全部到手了.
    """
    plan = build_plan(entry, chain, on_all_unavailable=on_all_unavailable)
    sizes = entry.available_qualities
    # 「可用的最高音质」按 QUALITY_TABLE 的排序算, 与用户的优先级链无关.
    best = next((code for code in QUALITY_TABLE if code in sizes), "")
    return {
        "best_available": best,
        "best_available_label": quality_label(best) if best else "",
        "plan": plan.as_dict(),
        "available_qualities": sizes,
        "needs_payment": bool(
            entry.pay.get("pay_play") or entry.pay.get("price_track") or entry.pay.get("pay_month"),
        ),
        "offline": entry.is_offline,
        "region_limited": entry.is_region_limited,
        # 原值透传, 不做位解析 (布局未文档化).
        "sa": entry.sa,
        "estimated_size": sizes.get(plan.requested, 0),
    }
