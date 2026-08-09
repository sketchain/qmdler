"""状态机流转."""

from __future__ import annotations

import pytest
from qqmusic_api.core.exceptions import (
    ApiDataError,
    CredentialExpiredError,
    CredentialRefreshError,
    GlobalApiError,
    HTTPError,
    LoginRateLimitError,
    NetworkError,
    RatelimitedError,
    SignatureRequiredError,
)

from qmdler.core.download.errors import (
    Action,
    classify,
    classify_result_code,
)
from qmdler.core.download.state import (
    can_transition_item,
    can_transition_task,
    is_complete,
    is_item_terminal,
    is_task_paused,
    resolve_action,
    summarize,
)
from qmdler.core.models import ItemStatus, TaskStatus

# --------------------------------------------------------------------------- #
# 迁移合法性
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ItemStatus.PENDING, ItemStatus.DOWNLOADING),
        (ItemStatus.PENDING, ItemStatus.SKIPPED),
        (ItemStatus.PENDING, ItemStatus.UNAVAILABLE),
        (ItemStatus.DOWNLOADING, ItemStatus.SUCCESS),
        (ItemStatus.DOWNLOADING, ItemStatus.TRIAL),
        (ItemStatus.DOWNLOADING, ItemStatus.FAILED),
        (ItemStatus.DOWNLOADING, ItemStatus.PENDING),
        (ItemStatus.FAILED, ItemStatus.PENDING),
        (ItemStatus.TRIAL, ItemStatus.PENDING),
    ],
)
def test_valid_item_transitions(current: ItemStatus, target: ItemStatus) -> None:
    """合法的条目迁移."""
    assert can_transition_item(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ItemStatus.SUCCESS, ItemStatus.DOWNLOADING),
        (ItemStatus.SUCCESS, ItemStatus.FAILED),
        (ItemStatus.SKIPPED, ItemStatus.SUCCESS),
        (ItemStatus.PENDING, ItemStatus.SUCCESS),
        (ItemStatus.PENDING, ItemStatus.TRIAL),
    ],
)
def test_invalid_item_transitions(current: ItemStatus, target: ItemStatus) -> None:
    """终态只能靠「重试失败项」显式回到 pending; pending 不能直接跳到 success."""
    assert not can_transition_item(current, target)


def test_item_self_transition_allowed() -> None:
    """同状态重复写入是幂等的."""
    assert can_transition_item(ItemStatus.DOWNLOADING, ItemStatus.DOWNLOADING)


def test_terminal_statuses() -> None:
    """五个终态."""
    terminal = [
        ItemStatus.SUCCESS,
        ItemStatus.FAILED,
        ItemStatus.SKIPPED,
        ItemStatus.UNAVAILABLE,
        ItemStatus.TRIAL,
    ]
    assert all(is_item_terminal(status) for status in terminal)
    assert not is_item_terminal(ItemStatus.PENDING)
    assert not is_item_terminal(ItemStatus.DOWNLOADING)


@pytest.mark.parametrize(
    ("current", "target", "ok"),
    [
        (TaskStatus.PENDING, TaskStatus.RUNNING, True),
        (TaskStatus.RUNNING, TaskStatus.PAUSED_RATELIMITED, True),
        (TaskStatus.RUNNING, TaskStatus.PAUSED_AUTH, True),
        (TaskStatus.RUNNING, TaskStatus.COMPLETED, True),
        (TaskStatus.PAUSED, TaskStatus.RUNNING, True),
        (TaskStatus.PENDING, TaskStatus.COMPLETED, False),
        (TaskStatus.COMPLETED, TaskStatus.PAUSED, False),
    ],
)
def test_task_transitions(current: TaskStatus, target: TaskStatus, ok: bool) -> None:
    """任务迁移."""
    assert can_transition_task(current, target) is ok


def test_paused_statuses() -> None:
    """三种暂停态都算暂停."""
    assert is_task_paused(TaskStatus.PAUSED)
    assert is_task_paused(TaskStatus.PAUSED_RATELIMITED)
    assert is_task_paused(TaskStatus.PAUSED_AUTH)
    assert not is_task_paused(TaskStatus.RUNNING)


# --------------------------------------------------------------------------- #
# 异常 → 动作
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("exc", "action"),
    [
        (NetworkError("断网"), Action.RETRY),
        (HTTPError("boom", status_code=500), Action.RETRY),
        (CredentialExpiredError(code=1000), Action.REFRESH_AND_RETRY),
        (CredentialRefreshError(code=1000), Action.PAUSE_AUTH),
        (RatelimitedError(code=2001), Action.PAUSE_RATELIMIT),
        (LoginRateLimitError(code=104604), Action.PAUSE_AUTH),
        (SignatureRequiredError(), Action.PAUSE_TASK),
        (GlobalApiError(code=500), Action.PAUSE_TASK),
        (ApiDataError("解析失败"), Action.FAIL_ITEM),
        (OSError("磁盘满"), Action.FAIL_ITEM),
        (ValueError("其它"), Action.FAIL_ITEM),
    ],
)
def test_exception_classification(exc: BaseException, action: Action) -> None:
    """规范里那张异常处理表, 逐条对齐."""
    assert classify(exc).action is action


def test_ratelimited_pauses_entire_task() -> None:
    """触发风控要立即暂停整个任务, 并给出明确提示."""
    decision = classify(RatelimitedError(code=2001, data={"feedbackURL": "https://x"}))
    assert decision.action is Action.PAUSE_RATELIMIT
    assert "风控" in decision.message
    assert "https://x" in decision.message


def test_api_data_error_does_not_stop_queue() -> None:
    """单曲失败不中断队列."""
    outcome = resolve_action(classify(ApiDataError("x")).action)
    assert outcome.task_status is None
    assert outcome.item_status is ItemStatus.FAILED


@pytest.mark.parametrize(
    ("code", "action"),
    [
        (104003, Action.UNAVAILABLE),
        (104004, Action.RETRY),
        (104013, Action.PAUSE_TASK),
        (999999, Action.FAIL_ITEM),
    ],
)
def test_result_code_classification(code: int, action: Action) -> None:
    """result 业务码单独处理."""
    assert classify_result_code(code).action is action


def test_device_limited_message_mentions_vip_irrelevance() -> None:
    """104013 设备受限与会员等级无关, 提示要说清楚."""
    assert "会员等级无关" in classify_result_code(104013).message


# --------------------------------------------------------------------------- #
# 动作 → 迁移
# --------------------------------------------------------------------------- #


def test_retry_keeps_item_status() -> None:
    """重试不改条目状态."""
    outcome = resolve_action(Action.RETRY)
    assert outcome.retry is True
    assert outcome.item_status is None
    assert outcome.task_status is None


@pytest.mark.parametrize(
    ("action", "task_status"),
    [
        (Action.PAUSE_TASK, TaskStatus.PAUSED),
        (Action.PAUSE_AUTH, TaskStatus.PAUSED_AUTH),
        (Action.PAUSE_RATELIMIT, TaskStatus.PAUSED_RATELIMITED),
    ],
)
def test_pause_actions_return_item_to_pending(action: Action, task_status: TaskStatus) -> None:
    """暂停时当前这一首回到 pending, 恢复后从它继续."""
    outcome = resolve_action(action)
    assert outcome.task_status is task_status
    assert outcome.item_status is ItemStatus.PENDING


def test_resolved_transitions_are_all_legal() -> None:
    """每个动作解析出的迁移都必须是状态机允许的."""
    for action in Action:
        outcome = resolve_action(action)
        if outcome.item_status is not None:
            assert can_transition_item(ItemStatus.DOWNLOADING, outcome.item_status)
        if outcome.task_status is not None:
            assert can_transition_task(TaskStatus.RUNNING, outcome.task_status)


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def test_summarize_counts_trial_separately() -> None:
    """试听单独计数, 绝不混进成功."""
    summary = summarize({"success": 10, "trial": 3, "failed": 1})
    assert summary["success"] == 10
    assert summary["trial"] == 3
    assert summary["success"] + summary["trial"] == 13


def test_is_complete() -> None:
    """队列跑完的判定."""
    assert is_complete({"success": 5, "failed": 1})
    assert not is_complete({"success": 5, "pending": 1})
    assert not is_complete({"downloading": 1})
