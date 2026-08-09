"""异常 → 动作 的映射.

按库的异常体系分类处理, 不允许一个 ``except Exception`` 糊过去.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qqmusic_api.core.exceptions import (
    ApiDataError,
    CredentialExpiredError,
    CredentialInvalidError,
    CredentialRefreshError,
    GlobalApiError,
    HTTPError,
    LoginRateLimitError,
    NetworkError,
    RatelimitedError,
    SignatureRequiredError,
)


class Action(str, Enum):
    """遇到异常后该做什么."""

    #: 指数退避重试.
    RETRY = "retry"
    #: 刷新凭证后重试一次.
    REFRESH_AND_RETRY = "refresh_and_retry"
    #: 单曲失败, 记录后继续下一首.
    FAIL_ITEM = "fail_item"
    #: 标记为无版权 / 付费受限.
    UNAVAILABLE = "unavailable"
    #: 暂停整个任务.
    PAUSE_TASK = "pause_task"
    #: 暂停任务并要求重新登录.
    PAUSE_AUTH = "pause_auth"
    #: 暂停任务并提示触发风控.
    PAUSE_RATELIMIT = "pause_ratelimit"


@dataclass(slots=True)
class Decision:
    """对一次异常的处置决定."""

    action: Action
    message: str
    error_type: str
    result_code: int | None = None


def classify(exc: BaseException) -> Decision:
    """把异常映射成处置动作."""
    name = type(exc).__name__

    # 顺序要紧: 子类在前.
    if isinstance(exc, RatelimitedError):
        extra = f"，反馈链接：{exc.feedback_url}" if getattr(exc, "feedback_url", None) else ""
        return Decision(
            Action.PAUSE_RATELIMIT,
            f"触发风控或频率限制，已暂停整个任务，请稍后手动恢复{extra}",
            name,
            exc.code,
        )
    if isinstance(exc, LoginRateLimitError):
        return Decision(Action.PAUSE_AUTH, f"登录操作过于频繁，请稍后再试：{exc}", name, exc.code)
    if isinstance(exc, CredentialRefreshError):
        return Decision(Action.PAUSE_AUTH, f"凭证刷新失败，需要重新登录：{exc}", name, exc.code)
    if isinstance(exc, CredentialExpiredError):
        return Decision(Action.REFRESH_AND_RETRY, "凭证已过期，刷新后重试", name, exc.code)
    if isinstance(exc, CredentialInvalidError):
        return Decision(Action.PAUSE_AUTH, f"凭证缺失或格式损坏，请重新登录：{exc}", name)
    if isinstance(exc, SignatureRequiredError):
        return Decision(
            Action.PAUSE_TASK,
            f"接口要求签名（code={exc.code}），通常意味着库或接口有变动，已暂停任务",
            name,
            exc.code,
        )
    if isinstance(exc, GlobalApiError):
        return Decision(
            Action.PAUSE_TASK,
            f"请求被网关拒绝（code={exc.code}），通常意味着库或接口有变动，已暂停任务",
            name,
            exc.code,
        )
    if isinstance(exc, ApiDataError):
        return Decision(Action.FAIL_ITEM, f"响应解析失败：{exc}", name)
    if isinstance(exc, HTTPError):
        return Decision(Action.RETRY, f"HTTP {exc.status_code}", name, exc.status_code)
    if isinstance(exc, NetworkError):
        return Decision(Action.RETRY, f"网络错误：{exc}", name)
    if isinstance(exc, TimeoutError):
        return Decision(Action.RETRY, "请求超时", name)
    if isinstance(exc, OSError):
        return Decision(Action.FAIL_ITEM, f"本地文件系统错误：{exc}", name)
    return Decision(Action.FAIL_ITEM, f"未分类异常：{exc}", name)


#: ``UrlinfoItem.result`` 的业务码.
RESULT_OK = 0
RESULT_NO_PERMISSION = 104003
RESULT_VKEY_FAILED = 104004
RESULT_DEVICE_LIMITED = 104013


def classify_result_code(code: int) -> Decision:
    """把取链返回的 ``result`` 码映射成处置动作."""
    if code == RESULT_OK:
        return Decision(Action.RETRY, "成功", "", code)
    if code == RESULT_NO_PERMISSION:
        return Decision(Action.UNAVAILABLE, "无权限：地区版权限制、已下架或付费受限", "ResultCode", code)
    if code == RESULT_VKEY_FAILED:
        return Decision(Action.RETRY, "VKey 获取失败，重试", "ResultCode", code)
    if code == RESULT_DEVICE_LIMITED:
        return Decision(
            Action.PAUSE_TASK,
            "播放设备受限（与会员等级无关），已暂停任务",
            "ResultCode",
            code,
        )
    return Decision(Action.FAIL_ITEM, f"取链失败，result={code}", "ResultCode", code)
