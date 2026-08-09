"""「声称具备」的行为逐条钉住.

这个文件的由来: 项目里连着出了三次同一类问题 —— **规范或 docstring 里写了,
实现没跟上, 而且只有真实压测才暴露**:

* ``_apply_metadata`` 的 docstring 写着「任何一项失败都不让整首歌算失败」,
  实际一次 ``get_producer`` 的 ``ValidationError`` 就把整首歌判成 failed;
* 「元数据请求走独立的轻量限速」—— 翻页、封面降档重试这两条路径漏了;
* 「任一端的操作对另一端立即可见」—— 拉取取消没广播.

所以把这些「声称」逐条变成用例. 判据是**可执行的**, 不是读代码读出来的.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from qmdler.core.config.schema import Settings
from qmdler.core.events import EventBus, EventKind
from qmdler.core.fsutil import check_path, check_space, probe_writable

# --------------------------------------------------------------------------- #
# 声称: 「任务开始前检查目录可写与磁盘空间」
# --------------------------------------------------------------------------- #


def test_writability_probe_really_writes(tmp_path: Path) -> None:
    """探针必须真写一次 —— ``os.access`` 会骗人."""
    ok, message = probe_writable(tmp_path)
    assert ok, message
    assert not list(tmp_path.iterdir()), "探针文件必须删干净"


@pytest.mark.skipif(os.geteuid() == 0, reason="root 无视权限位，测不出只读目录")
def test_readonly_dir_blocks_the_task(tmp_path: Path) -> None:
    """只读目录必须在**任务开始前**就被拦下, 不能跑到第 200 首才发现."""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        check = check_path(str(readonly))
        assert check.exists
        assert not check.writable
        assert not check.ok, "exists 为真但 writable 为假时，ok 必须是假"
        passed, message = check_space(str(readonly), 1, create=False)
        assert not passed, "只读目录必须让前置检查失败"
        assert message
    finally:
        readonly.chmod(stat.S_IRWXU)


def test_missing_dir_is_created(tmp_path: Path) -> None:
    """用户配了目录就是打算往里写, 首次下载时它当然还不存在."""
    target = tmp_path / "还没建" / "深一层"
    passed, message = check_space(str(target), 1)
    assert passed, message
    assert target.is_dir()


def test_not_enough_space_is_refused(tmp_path: Path) -> None:
    """空间不够要提前说, 别下到一半炸."""
    huge = 1 << 60  # 1 EiB
    passed, message = check_space(str(tmp_path), huge)
    assert not passed
    assert "磁盘剩余" in message


def test_file_path_is_rejected(tmp_path: Path) -> None:
    """路径是个文件而不是目录."""
    victim = tmp_path / "a.txt"
    victim.write_text("x", encoding="utf-8")
    check = check_path(str(victim))
    assert not check.ok
    assert "不是目录" in check.message


# --------------------------------------------------------------------------- #
# 声称: 「孤儿 .part 会在启动时清掉」
# --------------------------------------------------------------------------- #


def test_orphan_parts_are_removed_but_active_ones_kept(tmp_path: Path) -> None:
    """没主的删掉, 仍在进行中的留着 —— 删错了会让正在续传的任务从头再来."""
    from qmdler.core.download.fetcher import cleanup_orphan_parts

    orphan = tmp_path / "孤儿.flac.part"
    active = tmp_path / "进行中.flac.part"
    keeper = tmp_path / "正式文件.flac"
    for path in (orphan, active, keeper):
        path.write_bytes(b"x")

    removed = cleanup_orphan_parts([tmp_path], keep={active})

    assert removed == [orphan]
    assert not orphan.exists()
    assert active.exists(), "还在下的 .part 不能删"
    assert keeper.exists(), "正式文件当然不能删"


# --------------------------------------------------------------------------- #
# 声称: 「元数据请求走独立的轻量限速」—— 每一条发请求的路径都要走到
# --------------------------------------------------------------------------- #


async def test_cover_size_fallback_is_rate_limited(tmp_path: Path) -> None:
    """封面降档重试之间要等.

    一张封面最多试 6 个尺寸, 全 404 时就是 6 次背靠背请求 —— 一首歌一次、
    二十首歌一百多次. 回归: 这条路径原先完全没有限速.
    """
    import httpx

    from qmdler.core.metadata.cover import CoverFetcher
    from qmdler.core.models import SongEntry

    calls = {"http": 0, "delay": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["http"] += 1
        return httpx.Response(404)

    async def delay() -> None:
        calls["delay"] += 1

    entry = SongEntry.from_dict(
        {
            "songmid": "mid1", "songid": 1, "title": "t", "singers": [],
            "album_name": "a", "album_mid": "amid", "media_mid": "mid1", "song_type": 0,
            "interval": 1, "track_no": 1, "disc_no": 1, "year": "", "status": 0, "sa": 0,
            "sizes": {}, "pay": {}, "size_try": 0, "try_begin_ms": 0, "try_end_ms": 0,
            "vs": [], "vi": [], "vf": [],
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await CoverFetcher(http, delay=delay).fetch(entry, 500)

    assert not result.ok
    assert calls["http"] > 1, "素材要能触发降档重试，否则这条用例没测到东西"
    assert calls["delay"] == calls["http"] - 1, (
        f"降档之间少等了：请求 {calls['http']} 次，只等了 {calls['delay']} 次"
    )


def test_every_request_site_is_accounted_for() -> None:
    """把「哪些地方发请求、各自归哪类限速」钉死在用例里.

    新增一处请求而忘了考虑限速时，这条会红。改动它之前先问一句：
    这个新路径属于哪一类？
    """
    import inspect
    import re

    from qmdler.core.metadata import service as metadata_service
    from qmdler.core.sources import service as sources_service

    # 逐首/逐页的请求 —— 必须限速。只看真正的调用处（`self._client.…`），
    # 不看 docstring 里对同名方法的引用。
    for module, names in (
        (metadata_service, ("get_lyric", "get_multi_style_trans_lyric", "get_producer", "get_detail")),
        (sources_service, ("query_song",)),
    ):
        source = inspect.getsource(module)
        for name in names:
            # 只看真正的调用处，docstring 里提到同名方法不算。
            hits = [m.start() for m in re.finditer(rf"self\._client\.\w+\.{name}\(", source)]
            assert hits, f"{module.__name__} 里找不到 {name} 的调用处"
            for index in hits:
                before = source[max(0, index - 500):index]
                assert "_delay()" in before or "_metadata_delay()" in before, (
                    f"{module.__name__}.{name} 的调用处前面没有限速"
                )

    # 翻页统一走 _paginate（它内部限速）；不允许再出现裸的 collect_items 调用。
    # 只看调用，不看文档里对它的引用。
    naked = re.findall(r"\.collect_items\(", inspect.getsource(sources_service))
    assert not naked, "翻页要走 _paginate，collect_items 会把几页背靠背打完"


# --------------------------------------------------------------------------- #
# 声称: 「任一端的操作对另一端立即可见」
# --------------------------------------------------------------------------- #


def collect_events(bus: EventBus) -> list[Any]:
    """把 emit 拦下来看. 比起接 WS，这样能精确到「有没有推」。"""
    seen: list[Any] = []
    original = bus.emit

    def spy(kind: Any, payload: dict[str, Any], **kwargs: Any) -> Any:
        seen.append((kind, payload))
        return original(kind, payload, **kwargs)

    bus.emit = spy  # type: ignore[method-assign]
    return seen


def test_fetch_cancel_is_broadcast() -> None:
    """取消拉取要推事件 —— 不推的话另一端还停在「正在拉取…」上干等.

    回归: 这条原先只改了个本地标志位, 什么都没推.
    """
    from qmdler.core.sources.service import SourceService

    bus = EventBus()
    seen = collect_events(bus)
    service = SourceService(object(), object(), Settings().download, bus)  # type: ignore[arg-type]

    service.cancel_fetch()

    kinds = [kind for kind, _payload in seen]
    assert EventKind.SOURCE_PROGRESS in kinds, "取消没有广播"
    payload = next(p for k, p in seen if k is EventKind.SOURCE_PROGRESS)
    assert payload["cancelled"] is True


async def test_config_change_is_broadcast(tmp_path: Path) -> None:
    """设置变更要推给两端, 否则另一端还按旧配置显示."""
    from qmdler.server.context import AppContext

    bus = EventBus()
    seen = collect_events(bus)
    settings = Settings()

    applied: list[Any] = []

    class FakeEngine:
        def apply_settings(self, value: Any) -> None:
            applied.append(value)

    context = AppContext(
        settings=settings, config_store=None, bus=bus, repo=None,  # type: ignore[arg-type]
        client=None, http=None, auth=None, login=None, sources=None,  # type: ignore[arg-type]
        metadata=None, engine=FakeEngine(), _holder=None,  # type: ignore[arg-type]
    )
    new_settings = Settings()
    new_settings.download.interval_seconds = 42.0
    await context.reload_settings(new_settings)

    assert applied, "新配置没有下发给引擎"
    assert any(kind is EventKind.CONFIG_CHANGED for kind, _ in seen), "设置变更没有广播"


# --------------------------------------------------------------------------- #
# 声称: 「环境变量能覆盖任意配置项」「预设可一键切换」
# --------------------------------------------------------------------------- #


def test_env_override_reaches_nested_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """``QMDLER_`` 前缀 + ``__`` 分隔, 嵌套字段也要能覆盖."""
    monkeypatch.setenv("QMDLER_DOWNLOAD__INTERVAL_SECONDS", "42")
    monkeypatch.setenv("QMDLER_QUALITY__REJECT_TRIAL", "false")
    monkeypatch.setenv("QMDLER_SERVER__PORT", "9999")

    settings = Settings()

    assert settings.download.interval_seconds == 42.0
    assert settings.quality.reject_trial is False
    assert settings.server.port == 9999


def test_env_override_beats_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """优先级: 文件 < 环境变量."""
    from qmdler.core.config.store import ConfigStore

    config = tmp_path / "config.toml"
    config.write_text("[download]\ninterval_seconds = 7\n", encoding="utf-8")
    monkeypatch.setenv("QMDLER_DOWNLOAD__INTERVAL_SECONDS", "99")

    settings = ConfigStore(config).load()

    assert settings.download.interval_seconds == 99.0, "环境变量必须压过文件"


def test_preset_switch_changes_effective_settings(tmp_path: Path) -> None:
    """预设切换要真的改变生效配置, 不能只改一个名字."""
    from qmdler.core.config.store import ConfigStore

    store = ConfigStore(tmp_path / "config.toml")
    baseline = store.load()
    presets = store.all_presets()
    assert presets, "内置预设一个都没有"

    name = next(iter(presets))
    updated = store.activate_preset(name)

    assert updated.preset == name
    differs = (
        updated.quality.chain != baseline.quality.chain
        or updated.download.interval_seconds != baseline.download.interval_seconds
        or updated.naming.dir_template != baseline.naming.dir_template
        or updated.lyric.enabled != baseline.lyric.enabled
        or updated.cover.enabled != baseline.cover.enabled
    )
    assert differs, f"切到预设 {name} 之后没有任何生效项发生变化 —— 预设等于没起作用"

    # 一次性写入而非运行时叠加：重新 load 也要拿到同样的值，且此后普通编辑能盖过它。
    assert store.load().quality.chain == updated.quality.chain, "预设的值没有落盘"
    after_edit = store.update({"quality": {"chain": ["MP3_128"]}})
    assert after_edit.quality.chain == ["MP3_128"], "启用预设后，用户的修改必须能生效"
    assert store.load().quality.chain == ["MP3_128"], "改完重新加载不能弹回预设的值"


def test_preset_does_not_mask_later_edits_across_restart(tmp_path: Path) -> None:
    """启用预设后改动被预设覆盖的字段, **重启后也要还在**.

    回归: 旧实现里预设是**运行时叠加层** —— ``load()`` 每次都把预设的值盖回去。
    用户改音质链、保存、当次生效，重启后悄悄弹回预设的值，而且完全看不出为什么。
    现在预设是一次性写入文件，文件就是唯一事实。
    """
    from qmdler.core.config.store import ConfigStore

    config = tmp_path / "config.toml"
    store = ConfigStore(config)
    store.activate_preset("无损归档")
    store.update({"quality": {"chain": ["MP3_128"]}})

    # 换一个 store 实例 = 重启
    assert ConfigStore(config).load().quality.chain == ["MP3_128"], "重启后被预设盖回去了"


# --------------------------------------------------------------------------- #
# 声称: 「单曲失败不中断队列」—— 对所有失败类型都要成立
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("上游模型吃不下这个结构"),
        TypeError("服务端回了 None"),
        KeyError("缺字段"),
        RuntimeError("说不清的错"),
        OSError("磁盘抽风"),
    ],
    ids=["ValueError", "TypeError", "KeyError", "RuntimeError", "OSError"],
)
async def test_metadata_failure_of_any_type_keeps_the_song(exc: Exception) -> None:
    """附加内容不管抛什么类型, 歌都不能算失败.

    回归: 原先只 catch ``BaseApiException``, 一个 pydantic ``ValidationError``
    就把整首歌判成 failed —— 而音频早就完整落盘了。这里把「任意异常类型」
    钉死, 而不是把撞到过的那一个补进 except 元组。
    """
    from qmdler.core.download.engine import DownloadEngine

    engine = DownloadEngine.__new__(DownloadEngine)
    engine._bus = EventBus()

    async def boom() -> str:
        raise exc

    sentinel = "兜底值"
    result = await DownloadEngine._best_effort(
        engine, boom(), what="测试项", entry=object(), task=_FakeTask(), fallback=sentinel,
    )
    assert result == sentinel, f"{type(exc).__name__} 没有被兜住"


async def test_cancellation_is_not_swallowed() -> None:
    """``CancelledError`` 必须照常传上去 —— 它不是「失败」, 是用户按了取消."""
    import asyncio

    from qmdler.core.download.engine import DownloadEngine

    engine = DownloadEngine.__new__(DownloadEngine)
    engine._bus = EventBus()

    async def cancelled() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await DownloadEngine._best_effort(
            engine, cancelled(), what="测试项", entry=object(), task=_FakeTask(), fallback="兜底",
        )


class _FakeTask:
    id = "t1"
