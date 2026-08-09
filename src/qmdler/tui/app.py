"""Textual 应用.

纯客户端: 全部动作走 REST, 状态走 WebSocket, 与 WebUI 连的是同一个后端.

响应式: 监听 resize, 按终端宽度在 Screen 上打 ``-narrow`` / ``-medium`` /
``-wide`` 类, TCSS 据此切换布局; 曲目表在窄宽度下只留「歌名 + 状态 + 进度」,
侧栏改为可切换的全屏视图 —— 不出现横向滚动或折行错乱.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, Label, ListItem, ListView, RichLog, Static

from .bootstrap import Backend
from .client import ApiClient, EventStream
from .screens import LoginScreen, ReportScreen, SettingsScreen

#: 断点 (终端列数).
NARROW_MAX = 59
MEDIUM_MAX = 99

STATUS_TEXT = {
    "pending": "待下载",
    "downloading": "下载中",
    "success": "成功",
    "failed": "失败",
    "skipped": "跳过",
    "unavailable": "受限",
    "trial": "试听",
}


def _bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


class QmdlerApp(App[None]):
    """qmdler TUI."""

    CSS_PATH = "app.tcss"
    TITLE = "qmdler"

    BINDINGS: ClassVar[list[BindingType]] = [
        ("q", "quit", "退出"),
        ("l", "login", "登录"),
        ("s", "settings", "设置"),
        ("f1", "view_sources", "来源"),
        ("f2", "view_tracks", "曲目"),
        ("f3", "view_tasks", "任务"),
        ("f5", "refresh", "刷新"),
        ("p", "pause", "暂停"),
        ("g", "start", "开始"),
        ("n", "skip_wait", "立即下一首"),
        ("r", "report", "报告"),
        ("space", "toggle_item", "勾选"),
        ("a", "select_all", "全选"),
        ("i", "invert", "反选"),
    ]

    def __init__(self, backend: Backend) -> None:
        """初始化."""
        super().__init__()
        self._backend = backend
        self.api = ApiClient(backend.base_url)
        self._stream: EventStream | None = None
        self._config: dict[str, Any] = {}
        self._catalog: list[dict[str, Any]] = []
        self._auth: dict[str, Any] = {}
        self._engine: dict[str, Any] = {}
        self._items: list[dict[str, Any]] = []
        self._source_items: list[dict[str, Any]] = []
        self._source_meta: dict[str, Any] = {}
        self._task_id = ""
        self._breakpoint = ""
        self._view = "tracks"

    # -- 布局 ---------------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        """构造界面."""
        yield Static("qmdler — 正在连接…", id="topbar")
        with Horizontal(id="body"):
            with VerticalScroll(id="sidebar"):
                yield Label("歌单来源", classes="pane-title")
                yield Input(placeholder="歌单链接 / ID / 关键词 / mid", id="source-input")
                with Horizontal(classes="button-row"):
                    yield Button("解析", id="btn-resolve", variant="primary")
                    yield Button("搜索", id="btn-search")
                with Horizontal(classes="button-row"):
                    yield Button("我喜欢", id="btn-fav-song")
                    yield Button("自建", id="btn-created")
                yield ListView(id="songlists")
            with Vertical(id="center"):
                yield Label("曲目", classes="pane-title", id="center-title")
                yield DataTable(id="tracks", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="button-row"):
                    yield Button("建任务", id="btn-create", variant="primary")
                    yield Button("开始", id="btn-start")
                    yield Button("暂停", id="btn-pause")
            with VerticalScroll(id="taskpane"):
                yield Label("任务", classes="pane-title")
                yield Static("空闲", id="engine-status")
                yield Static("", id="countdown")
                yield Static("", id="counts", classes="counts")
                with Horizontal(classes="button-row"):
                    yield Button("立即下一首", id="btn-skip")
                    yield Button("重试失败", id="btn-retry")
                yield RichLog(id="log", markup=True, wrap=True, max_lines=500)
        yield Footer()

    async def on_mount(self) -> None:
        """挂载: 建表头、连 WS、拉快照."""
        self._apply_breakpoint(self.size.width)
        self._rebuild_table()

        self._stream = EventStream(self._backend.ws_url, self._on_event)
        self._stream.start()

        await self._load_snapshot()

    def on_resize(self, event: Any) -> None:
        """终端尺寸变化时实时重排."""
        self._apply_breakpoint(event.size.width)

    def _apply_breakpoint(self, width: int) -> None:
        if width <= NARROW_MAX:
            name = "-narrow"
        elif width <= MEDIUM_MAX:
            name = "-medium"
        else:
            name = "-wide"
        if name == self._breakpoint:
            return
        self._breakpoint = name
        screen = self.screen
        screen.remove_class("-narrow", "-medium", "-wide")
        screen.add_class(name)
        self._sync_view_classes()
        self._rebuild_table()

    def _sync_view_classes(self) -> None:
        screen = self.screen
        screen.remove_class("-show-sources", "-show-tasks")
        if self._view == "sources":
            screen.add_class("-show-sources")
        elif self._view == "tasks":
            screen.add_class("-show-tasks")

    # -- 曲目表 -------------------------------------------------------------- #

    @property
    def _narrow(self) -> bool:
        return self._breakpoint == "-narrow"

    @property
    def _medium(self) -> bool:
        return self._breakpoint == "-medium"

    def _rebuild_table(self) -> None:
        """按当前宽度决定保留哪些列. 窄宽度只留歌名 + 状态 + 进度."""
        try:
            table = self.query_one("#tracks", DataTable)
        except Exception:
            return
        table.clear(columns=True)
        if self._narrow:
            table.add_columns("✓", "歌名", "状态", "进度")
        elif self._medium:
            table.add_columns("✓", "歌名", "歌手", "状态", "进度")
        else:
            table.add_columns("✓", "歌名", "歌手", "专辑", "音质", "状态", "进度")
        self._fill_table()

    def _fill_table(self) -> None:
        table = self.query_one("#tracks", DataTable)
        table.clear()
        rows = self._items or self._source_rows()
        # 窄宽度下把歌名截断, 避免折行错乱.
        name_width = max(10, self.size.width - 22) if self._narrow else 40
        for row in rows:
            check = "●" if row.get("selected", True) else "○"
            title = str(row.get("title", ""))
            if len(title) > name_width:
                title = title[: name_width - 1] + "…"
            status = STATUS_TEXT.get(str(row.get("status", "pending")), str(row.get("status")))
            progress = _bar(float(row.get("progress", 0) or 0), 6 if self._narrow else 10)
            if self._narrow:
                table.add_row(check, title, status, progress)
            elif self._medium:
                table.add_row(check, title, str(row.get("singers", "")), status, progress)
            else:
                quality = row.get("actual_quality_label") or row.get("best_quality") or ""
                table.add_row(
                    check,
                    title,
                    str(row.get("singers", "")),
                    str(row.get("album", "")),
                    str(quality),
                    status,
                    progress,
                )

    def _source_rows(self) -> list[dict[str, Any]]:
        rows = []
        for item in self._source_items:
            annotation = item.get("annotation") or {}
            rows.append(
                {
                    "songmid": item["songmid"],
                    "title": item.get("title", ""),
                    "singers": "、".join(s.get("name", "") for s in item.get("singers", [])),
                    "album": item.get("album_name", ""),
                    "best_quality": annotation.get("best_available_label", ""),
                    "status": "pending",
                    "selected": True,
                    "progress": 0,
                },
            )
        return rows

    # -- 数据加载 ------------------------------------------------------------- #

    async def _load_snapshot(self) -> None:
        try:
            snapshot = await self.api.snapshot()
        except Exception as exc:
            self._write_log(f"[red]连接后端失败：{exc}[/red]")
            return
        self._auth = snapshot.get("auth", {})
        self._engine = snapshot.get("engine", {})
        self._config = snapshot.get("settings", {})
        with contextlib.suppress(Exception):
            self._catalog = (await self.api.qualities())["items"]
        self._refresh_header()
        self._refresh_task_panel()

    def _refresh_header(self) -> None:
        bar = self.query_one("#topbar", Static)
        connected = self._stream.connected if self._stream else False
        profile = self._auth.get("profile") or {}
        fallback = f"musicid {self._auth.get('musicid')}" if self._auth.get("logged_in") else "未登录"
        who = profile.get("nickname") or fallback
        vip = profile.get("vip") or {}
        if vip.get("svip"):
            badge = "SVIP"
        elif vip.get("huge_vip"):
            badge = "豪华绿钻"
        elif vip.get("vip"):
            badge = "绿钻"
        else:
            badge = ""

        if self._narrow:
            text = f"{'●' if connected else '○'} {who}"
        else:
            text = f"{'●' if connected else '○'} {'已连接' if connected else '重连中'} │ {who}"
            if badge:
                text += f" [{badge}]"
            if self._engine.get("running"):
                text += f" │ {self._engine.get('item_title', '')}"
        bar.update(text)
        bar.set_class(bool(self._auth.get("needs_login")), "-alert")

    def _refresh_task_panel(self) -> None:
        engine = self._engine
        status = self.query_one("#engine-status", Static)
        phase = engine.get("phase", "idle")
        name = engine.get("task_name") or "空闲"
        status.update(f"[b]{name}[/b]\n阶段：{phase}\n{engine.get('pause_reason', '')}")

        counts = engine.get("counts") or {}
        self.query_one("#counts", Static).update(
            f"成功 {counts.get('success', 0)} · 失败 {counts.get('failed', 0)} · "
            f"跳过 {counts.get('skipped', 0)} · 受限 {counts.get('unavailable', 0)} · "
            f"试听 {counts.get('trial', 0)}",
        )

        countdown = self.query_one("#countdown", Static)
        if engine.get("waiting"):
            remaining = float(engine.get("countdown", 0))
            total = float(engine.get("countdown_total", 1)) or 1
            countdown.update(f"下一首 {int(remaining)}s  {_bar(1 - remaining / total, 12)}  [N] 立即下载")
        else:
            countdown.update("")

    def _write_log(self, message: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#log", RichLog).write(message)

    # -- WS 事件 ------------------------------------------------------------- #

    def _on_event(self, frame: dict[str, Any]) -> None:
        kind = frame.get("kind")
        payload = frame.get("payload") or {}

        if kind == "snapshot":
            self._auth = payload.get("auth", self._auth)
            self._engine = payload.get("engine", self._engine)
            self._config = payload.get("settings", self._config)
        elif kind == "auth_state":
            self._auth = payload
        elif kind == "config_changed":
            self._config = payload
        elif kind == "task_state":
            if "engine" in payload:
                self._engine = payload["engine"]
        elif kind == "countdown":
            self._engine["waiting"] = True
            self._engine["countdown"] = payload.get("remaining", 0)
            self._engine["countdown_total"] = payload.get("total", 1)
        elif kind == "item_state":
            self._merge_item(payload)
        elif kind == "progress":
            self._merge_progress(frame.get("item_id"), payload)
        elif kind == "log":
            level = frame.get("level", "info")
            color = {"error": "red", "warning": "yellow"}.get(level, "")
            message = payload.get("message", "")
            self._write_log(f"[{color}]{message}[/{color}]" if color else message)
        elif kind == "login_event":
            screen = self.screen
            if isinstance(screen, LoginScreen):
                screen.on_login_event(payload)

        self._refresh_header()
        self._refresh_task_panel()

    def _merge_item(self, payload: dict[str, Any]) -> None:
        for index, item in enumerate(self._items):
            if item.get("id") == payload.get("id"):
                self._items[index] = payload
                break
        else:
            return
        self._fill_table()

    def _merge_progress(self, item_id: int | None, payload: dict[str, Any]) -> None:
        if item_id is None:
            return
        for item in self._items:
            if item.get("id") == item_id:
                total = payload.get("total") or item.get("bytes_expected") or 0
                item["bytes_downloaded"] = payload.get("written", 0)
                item["progress"] = (payload.get("written", 0) / total) if total else 0
                break
        self._fill_table()

    # -- 动作 ---------------------------------------------------------------- #

    # push_screen_wait 只能在 worker 里调用 (Textual 会用 NoActiveWorker 拒绝),
    # 所以这三个会弹模态框的 action 都要挂 @work.
    @work
    async def action_login(self) -> None:
        """打开登录界面."""
        await self.push_screen_wait(LoginScreen(self.api))
        with contextlib.suppress(Exception):
            self._auth = await self.api.auth_state()
            if self._auth.get("logged_in"):
                await self.api.profile()
        self._refresh_header()

    @work
    async def action_settings(self) -> None:
        """打开设置界面."""
        if not self._config:
            with contextlib.suppress(Exception):
                self._config = await self.api.get_config()
        changed = await self.push_screen_wait(SettingsScreen(self.api, self._config, self._catalog))
        if changed:
            with contextlib.suppress(Exception):
                self._config = await self.api.get_config()
            self._write_log("配置已保存")

    @work
    async def action_report(self) -> None:
        """打开汇总报告."""
        if not self._task_id:
            self._write_log("[yellow]还没有选中任务[/yellow]")
            return
        try:
            report = await self.api.report(self._task_id)
        except Exception as exc:
            self._write_log(f"[red]{exc}[/red]")
            return
        await self.push_screen_wait(ReportScreen(report))

    def action_view_sources(self) -> None:
        """切到来源视图 (窄屏)."""
        self._view = "sources"
        self._sync_view_classes()

    def action_view_tracks(self) -> None:
        """切到曲目视图."""
        self._view = "tracks"
        self._sync_view_classes()

    def action_view_tasks(self) -> None:
        """切到任务视图."""
        self._view = "tasks"
        self._sync_view_classes()

    async def action_refresh(self) -> None:
        """刷新数据."""
        await self._load_snapshot()
        if self._task_id:
            self._items = await self.api.list_items(self._task_id)
            self._fill_table()

    async def action_start(self) -> None:
        """开始任务."""
        await self._task_action(self.api.start_task, "已开始")

    async def action_pause(self) -> None:
        """暂停任务."""
        await self._task_action(self.api.pause_task, "已请求暂停")

    async def action_skip_wait(self) -> None:
        """立即下载下一首."""
        await self._task_action(self.api.skip_wait, "跳过等待")

    async def _task_action(self, action: Any, message: str) -> None:
        if not self._task_id:
            self._write_log("[yellow]还没有任务[/yellow]")
            return
        try:
            await action(self._task_id)
        except Exception as exc:
            self._write_log(f"[red]{exc}[/red]")
            return
        self._write_log(message)

    async def action_toggle_item(self) -> None:
        """勾选/取消勾选当前行."""
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if row is None or not self._items or row >= len(self._items):
            return
        item = self._items[row]
        target = not item.get("selected", True)
        with contextlib.suppress(Exception):
            await self.api.set_selection(self._task_id, [item["id"]], target)
        item["selected"] = target
        self._fill_table()

    async def action_select_all(self) -> None:
        """全选."""
        if not self._task_id:
            return
        with contextlib.suppress(Exception):
            await self.api.set_selection(self._task_id, None, True)
            self._items = await self.api.list_items(self._task_id)
        self._fill_table()

    async def action_invert(self) -> None:
        """反选."""
        if not self._task_id:
            return
        with contextlib.suppress(Exception):
            await self.api.invert_selection(self._task_id)
            self._items = await self.api.list_items(self._task_id)
        self._fill_table()

    # -- 按钮 ---------------------------------------------------------------- #

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """侧栏与中栏的按钮."""
        button_id = event.button.id or ""
        try:
            if button_id == "btn-resolve":
                await self._resolve_and_fetch()
            elif button_id == "btn-search":
                await self._fetch("search", self.query_one("#source-input", Input).value.strip())
            elif button_id == "btn-fav-song":
                await self._fetch("fav_song", "")
            elif button_id == "btn-created":
                await self._load_created()
            elif button_id == "btn-create":
                await self._create_task()
            elif button_id == "btn-start":
                await self.action_start()
            elif button_id == "btn-pause":
                await self.action_pause()
            elif button_id == "btn-skip":
                await self.action_skip_wait()
            elif button_id == "btn-retry":
                await self._task_action(self.api.retry_failed, "已重置失败项")
                if self._task_id:
                    self._items = await self.api.list_items(self._task_id)
                    self._fill_table()
        except Exception as exc:
            self._write_log(f"[red]{exc}[/red]")

    async def _resolve_and_fetch(self) -> None:
        text = self.query_one("#source-input", Input).value.strip()
        if not text:
            return
        resolved = await self.api.resolve(text)
        if resolved.get("source_type") == "manual" and resolved.get("mids"):
            await self._fetch("manual", "\n".join(resolved["mids"]))
        else:
            await self._fetch(resolved["source_type"], resolved["identifier"])

    async def _load_created(self) -> None:
        lists = await self.api.created_songlists()
        view = self.query_one("#songlists", ListView)
        view.clear()
        self._songlists = lists
        for entry in lists:
            view.append(ListItem(Label(f"{entry['title']}  ({entry['songnum']})")))
        self._write_log(f"自建歌单 {len(lists)} 个，回车选择")

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """选中一个歌单."""
        index = event.list_view.index
        lists = getattr(self, "_songlists", [])
        if index is None or index >= len(lists):
            return
        await self._fetch("songlist", str(lists[index]["id"]))

    async def _fetch(self, source_type: str, identifier: str) -> None:
        self._write_log(f"正在拉取 {source_type} …")
        result = await self.api.fetch_source(
            {"source_type": source_type, "identifier": identifier, "limit": 2000},
        )
        self._source_items = result["items"]
        self._source_meta = result
        self._items = []
        self._task_id = ""
        self._view = "tracks"
        self._sync_view_classes()
        self._fill_table()
        self.query_one("#center-title", Label).update(f"{result['name']}（{result['count']} 首）")
        self._write_log(f"已拉取 {result['count']} 首" + ("（已达上限）" if result.get("truncated") else ""))

    async def _create_task(self) -> None:
        if not self._source_items:
            self._write_log("[yellow]先拉取一个来源[/yellow]")
            return
        task = await self.api.create_task(
            {
                "name": self._source_meta.get("name", "任务"),
                "source_type": self._source_meta.get("source_type", "manual"),
                "source_id": str(self._source_meta.get("identifier", "")),
                "items": self._source_items,
                "quality_chain": (self._config.get("quality") or {}).get("chain", []),
            },
        )
        self._task_id = task["id"]
        self._items = await self.api.list_items(self._task_id)
        self._source_items = []
        self._fill_table()
        self._write_log(f"任务已创建：{task['name']}（按 G 开始）")

    # -- 收尾 ---------------------------------------------------------------- #

    async def on_unmount(self) -> None:
        """退出时收掉 WS 与自己拉起的后端."""
        if self._stream is not None:
            await self._stream.stop()
        await self.api.close()
        await self._backend.shutdown()


async def run_tui(backend: Backend) -> None:
    """运行 TUI."""
    app = QmdlerApp(backend)
    await app.run_async()


def main(backend: Backend) -> None:
    """同步入口."""
    asyncio.run(run_tui(backend))
