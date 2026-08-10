# SPDX-License-Identifier: GPL-3.0-or-later
"""TUI 响应式布局与模态框.

不需要真后端: 指向一个不存在的地址, TUI 应该照常挂载并把连接失败写进日志.
重点验证 40 / 80 / 120 三种宽度都可用, 以及弹模态框不会炸.
"""

from __future__ import annotations

import asyncio

import pytest

from qmdler.tui.app import QmdlerApp
from qmdler.tui.bootstrap import Backend

# 指向一个不会有人监听的端口.
DEAD_BACKEND = Backend(base_url="http://127.0.0.1:1")


@pytest.mark.parametrize(
    ("width", "breakpoint", "columns"),
    [
        # Termux / 手机 SSH 常见宽度: 只留歌名 + 状态 + 进度
        (40, "-narrow", ["✓", "歌名", "状态", "进度"]),
        (80, "-medium", ["✓", "歌名", "歌手", "状态", "进度"]),
        (120, "-wide", ["✓", "歌名", "歌手", "专辑", "音质", "状态", "进度"]),
    ],
)
async def test_breakpoints_hide_secondary_columns(width: int, breakpoint: str, columns: list[str]) -> None:
    """窄宽度下自动隐藏次要列, 不靠横向滚动."""
    app = QmdlerApp(DEAD_BACKEND)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        assert app._breakpoint == breakpoint
        assert breakpoint in app.screen.classes
        table = app.query_one("#tracks")
        assert [str(column.label) for column in table.columns.values()] == columns


async def test_resize_rearranges_live() -> None:
    """监听 resize 事件实时重排, 不需要重启."""
    app = QmdlerApp(DEAD_BACKEND)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app._breakpoint == "-wide"

        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        assert app._breakpoint == "-narrow"
        assert "-narrow" in app.screen.classes
        assert "-wide" not in app.screen.classes

        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        assert app._breakpoint == "-medium"


async def test_narrow_view_switching() -> None:
    """窄屏下侧栏改为可切换的全屏视图."""
    app = QmdlerApp(DEAD_BACKEND)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()

        await pilot.press("f1")
        await pilot.pause()
        assert "-show-sources" in app.screen.classes

        await pilot.press("f3")
        await pilot.pause()
        assert "-show-tasks" in app.screen.classes
        assert "-show-sources" not in app.screen.classes

        await pilot.press("f2")
        await pilot.pause()
        assert "-show-sources" not in app.screen.classes
        assert "-show-tasks" not in app.screen.classes


@pytest.mark.parametrize("width", [40, 80, 120])
async def test_modal_screens_open_and_close(width: int) -> None:
    """设置 / 登录模态框能开能关.

    回归测试: ``push_screen_wait`` 必须在 worker 里调用, 否则 Textual 抛
    ``NoActiveWorker``. 这三个 action 都挂了 ``@work``.
    """
    app = QmdlerApp(DEAD_BACKEND)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()

        await pilot.press("s")
        await asyncio.sleep(0.5)
        await pilot.pause()
        assert type(app.screen).__name__ == "SettingsScreen"

        await pilot.press("escape")
        await asyncio.sleep(0.3)
        await pilot.pause()
        assert type(app.screen).__name__ == "Screen"

        await pilot.press("l")
        await asyncio.sleep(0.5)
        await pilot.pause()
        assert type(app.screen).__name__ == "LoginScreen"

        await pilot.press("escape")
        await asyncio.sleep(0.3)
        await pilot.pause()
        assert type(app.screen).__name__ == "Screen"


async def test_backend_unreachable_is_reported_not_crashed() -> None:
    """连不上后端时界面照常挂载, 错误写进日志."""
    app = QmdlerApp(DEAD_BACKEND)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.6)
        await pilot.pause()
        assert app.is_running
        assert app._auth == {}
