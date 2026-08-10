# SPDX-License-Identifier: GPL-3.0-or-later
"""状态机.

抽成纯函数, 不碰 I/O —— 这样状态流转可以单独做单元测试, 而不需要拉起整个引擎.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import PAUSED_TASK_STATUSES, TERMINAL_ITEM_STATUSES, ItemStatus, TaskStatus
from .errors import Action

#: 任务状态的合法迁移.
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PAUSED,
            TaskStatus.PAUSED_RATELIMITED,
            TaskStatus.PAUSED_AUTH,
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        },
    ),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.PAUSED_RATELIMITED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.PAUSED_AUTH: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset({TaskStatus.RUNNING}),  # 只重试失败项时会重新跑
    TaskStatus.CANCELLED: frozenset({TaskStatus.RUNNING}),
    TaskStatus.FAILED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
}

#: 条目状态的合法迁移.
ITEM_TRANSITIONS: dict[ItemStatus, frozenset[ItemStatus]] = {
    ItemStatus.PENDING: frozenset(
        {
            ItemStatus.DOWNLOADING,
            ItemStatus.SKIPPED,
            ItemStatus.UNAVAILABLE,
            ItemStatus.FAILED,
        },
    ),
    ItemStatus.DOWNLOADING: frozenset(
        {
            ItemStatus.SUCCESS,
            ItemStatus.FAILED,
            ItemStatus.TRIAL,
            ItemStatus.UNAVAILABLE,
            ItemStatus.PENDING,  # 暂停后回到待下载
        },
    ),
    # 终态只能靠「重试失败项」显式重置.
    ItemStatus.SUCCESS: frozenset({ItemStatus.PENDING}),
    ItemStatus.FAILED: frozenset({ItemStatus.PENDING}),
    ItemStatus.SKIPPED: frozenset({ItemStatus.PENDING}),
    ItemStatus.UNAVAILABLE: frozenset({ItemStatus.PENDING}),
    ItemStatus.TRIAL: frozenset({ItemStatus.PENDING}),
}


def can_transition_task(current: TaskStatus, target: TaskStatus) -> bool:
    """任务状态迁移是否合法."""
    if current is target:
        return True
    return target in TASK_TRANSITIONS.get(current, frozenset())


def can_transition_item(current: ItemStatus, target: ItemStatus) -> bool:
    """条目状态迁移是否合法."""
    if current is target:
        return True
    return target in ITEM_TRANSITIONS.get(current, frozenset())


def is_item_terminal(status: ItemStatus) -> bool:
    """条目是否已进入终态."""
    return status in TERMINAL_ITEM_STATUSES


def is_task_paused(status: TaskStatus) -> bool:
    """任务是否处于某种暂停态."""
    return status in PAUSED_TASK_STATUSES


@dataclass(slots=True)
class ActionOutcome:
    """一次异常处置对状态机的影响."""

    #: 条目要迁移到的状态; ``None`` 表示条目状态不变 (比如要重试).
    item_status: ItemStatus | None
    #: 任务要迁移到的状态; ``None`` 表示任务继续跑.
    task_status: TaskStatus | None
    #: 是否重试当前这一首.
    retry: bool = False


def resolve_action(action: Action) -> ActionOutcome:
    """把异常处置动作翻译成状态机迁移."""
    match action:
        case Action.RETRY:
            return ActionOutcome(item_status=None, task_status=None, retry=True)
        case Action.REFRESH_AND_RETRY:
            return ActionOutcome(item_status=None, task_status=None, retry=True)
        case Action.FAIL_ITEM:
            return ActionOutcome(item_status=ItemStatus.FAILED, task_status=None)
        case Action.UNAVAILABLE:
            return ActionOutcome(item_status=ItemStatus.UNAVAILABLE, task_status=None)
        case Action.PAUSE_TASK:
            return ActionOutcome(item_status=ItemStatus.PENDING, task_status=TaskStatus.PAUSED)
        case Action.PAUSE_AUTH:
            return ActionOutcome(item_status=ItemStatus.PENDING, task_status=TaskStatus.PAUSED_AUTH)
        case Action.PAUSE_RATELIMIT:
            return ActionOutcome(item_status=ItemStatus.PENDING, task_status=TaskStatus.PAUSED_RATELIMITED)
    raise ValueError(f"未处理的动作: {action}")  # pragma: no cover


def summarize(counts: dict[str, int]) -> dict[str, int]:
    """把状态计数整理成汇总报告用的结构."""
    return {
        "success": counts.get(ItemStatus.SUCCESS.value, 0),
        "failed": counts.get(ItemStatus.FAILED.value, 0),
        "skipped": counts.get(ItemStatus.SKIPPED.value, 0),
        "unavailable": counts.get(ItemStatus.UNAVAILABLE.value, 0),
        "trial": counts.get(ItemStatus.TRIAL.value, 0),
        "pending": counts.get(ItemStatus.PENDING.value, 0),
        "downloading": counts.get(ItemStatus.DOWNLOADING.value, 0),
    }


def is_complete(counts: dict[str, int]) -> bool:
    """队列是否已经跑完."""
    summary = summarize(counts)
    return summary["pending"] == 0 and summary["downloading"] == 0
