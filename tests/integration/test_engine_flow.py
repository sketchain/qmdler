"""下载引擎端到端流程 (全 mock, 不触网).

重点验证几条硬约束:
* 每次 ``get_song_urls`` 只包含**一首**歌;
* 取一首 → 立即下载 → 落盘 → 等间隔 → 取下一首, 不是先批量取链;
* 试听检出记 ``trial``, 不混进 ``success``;
* 判重命中记 ``skipped``;
* 单曲失败不中断队列.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from qmdler.core.config.schema import Settings
from qmdler.core.download.engine import DownloadEngine, new_task_id
from qmdler.core.events import EventBus
from qmdler.core.models import ItemStatus, SongEntry, TaskRecord, TaskStatus
from qmdler.core.storage.repository import Repository

CDN_BASE = "https://cdn.example.com/"
FULL_SIZE = 40_000
TRIAL_SIZE = 4_000


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #


class FakeUrlInfo:
    """UrlinfoItem 替身."""

    def __init__(self, mid: str, filename: str, purl: str, result: int = 0) -> None:
        self.mid = mid
        self.filename = filename
        self.purl = purl
        self.vkey = "VKEY"
        self.ekey = ""
        self.result = result


class FakeSongApi:
    """SongApi 替身, 记录每次调用."""

    def __init__(self) -> None:
        self.url_calls: list[list[Any]] = []
        self.behaviour: dict[str, str] = {}

    async def get_cdn_dispatch(self) -> Any:
        class Response:
            retcode = 0
            sip: ClassVar[list[str]] = [CDN_BASE]
            expiration = 3600

        return Response()

    def get_song_urls(self, file_info: list[Any], credential: Any = None) -> Any:
        self.url_calls.append(list(file_info))

        class Awaitable:
            def __init__(self, items: list[FakeUrlInfo]) -> None:
                self.data = items

            def __await__(self):
                async def inner() -> Any:
                    return self

                return inner().__await__()

        items = []
        for info in file_info:
            mode = self.behaviour.get(info.mid, "ok")
            prefix = info.file_type.s
            if mode == "trial":
                prefix = "RS02"
            if mode == "denied":
                items.append(FakeUrlInfo(info.mid, "", "", result=104003))
                continue
            items.append(
                FakeUrlInfo(info.mid, f"{prefix}{info.mid}{info.file_type.e}", f"{prefix}{info.mid}{info.file_type.e}"),
            )
        return Awaitable(items)

    def get_producer(self, mid: str) -> Any:  # pragma: no cover - tag 关闭时不会调
        raise AssertionError("不该被调用")


class FakeClient:
    """Client 替身."""

    def __init__(self) -> None:
        self.song = FakeSongApi()


class FakeAuth:
    """AuthManager 替身."""

    def __init__(self) -> None:
        self.checks = 0

    async def ensure_before_task(self) -> Any:
        self.checks += 1
        return object()

    async def get_valid_credential(self) -> Any:
        return object()

    async def refresh(self, *, force: bool = False) -> Any:  # pragma: no cover
        return object()


class FakeMetadata:
    """MetadataService 替身: 关掉全部附加内容."""

    async def fetch_lyric(self, entry: Any, config: Any) -> Any:
        from qmdler.core.metadata.service import LyricBundle
        from qmdler.core.models import SubStatus

        return LyricBundle(status=SubStatus.SKIPPED)

    def write_lyric_files(self, bundle: Any, path: Path, config: Any) -> list[Path]:
        return []

    async def fetch_cover(self, entry: Any, config: Any, *, for_embed: bool) -> Any:
        from qmdler.core.metadata.cover import EMPTY_COVER

        return EMPTY_COVER

    def write_cover_file(self, cover: Any, path: Path) -> None:
        return None

    async def fetch_extra_tags(self, entry: Any, config: Any) -> Any:
        from qmdler.core.metadata.service import ExtraTags

        return ExtraTags()

    def clear_caches(self) -> None:
        return None


class FakeSources:
    """SourceService 替身."""

    async def ensure_media_mid(self, entry: SongEntry) -> SongEntry:
        return entry


def make_entry(mid: str, title: str, *, size: int = FULL_SIZE) -> SongEntry:
    """造一首歌."""
    return SongEntry.from_dict(
        {
            "songmid": mid,
            "songid": 1,
            "title": title,
            "singers": [{"mid": "s1", "name": "歌手"}],
            "album_name": "专辑",
            "album_mid": "a1",
            "media_mid": mid,
            "song_type": 0,
            "interval": 200,
            "track_no": 1,
            "disc_no": 1,
            "year": "2020-01-01",
            "status": 0,
            "sa": 0,
            "sizes": {"MP3_128": size},
            "pay": {},
            "size_try": TRIAL_SIZE,
            "try_begin_ms": 0,
            "try_end_ms": 30000,
            "vs": [],
            "vi": [],
            "vf": [],
        },
    )


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """测试用配置: 无间隔、无附加内容、无 tag."""
    config = Settings()
    config.paths.save_root = str(tmp_path / "music")
    config.paths.disk_headroom_bytes = 0
    config.quality.chain = ["MP3_128"]
    # 关掉目录层级，让断言里的路径固定下来（默认模板会按 {歌单名} 建一层目录）。
    config.naming.dir_template = ""
    config.download.interval_seconds = 0.0
    config.download.metadata_delay_min = 0.0
    config.download.metadata_delay_max = 0.0
    config.download.max_retries = 0
    config.lyric.enabled = False
    config.cover.enabled = False
    config.tag.enabled = False
    return config


@pytest.fixture
async def repo(tmp_path: Path) -> Any:
    """临时数据库."""
    repository = Repository(tmp_path / "test.db")
    await repository.connect()
    yield repository
    await repository.close()


def make_http(payloads: dict[str, bytes]) -> httpx.AsyncClient:
    """按 URL 返回固定内容的假 CDN."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.lstrip("/")
        body = payloads.get(name)
        if body is None:
            return httpx.Response(404)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(body))})
        start = 0
        range_header = request.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header.removeprefix("bytes=")
            if spec.endswith("-"):
                start = int(spec[:-1])
            elif spec == "0-0":
                return httpx.Response(
                    206,
                    content=body[:1],
                    headers={"Content-Range": f"bytes 0-0/{len(body)}"},
                )
        chunk = body[start:]
        status = 206 if start else 200
        return httpx.Response(status, content=chunk, headers={"Content-Length": str(len(chunk))})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=CDN_BASE)


async def build_engine(repo: Repository, settings: Settings, payloads: dict[str, bytes]) -> tuple[Any, ...]:
    """组装引擎."""
    client = FakeClient()
    bus = EventBus()
    http = make_http(payloads)
    auth = FakeAuth()
    engine = DownloadEngine(
        client,  # type: ignore[arg-type]
        auth,  # type: ignore[arg-type]
        repo,
        bus,
        settings,
        FakeMetadata(),  # type: ignore[arg-type]
        FakeSources(),  # type: ignore[arg-type]
        http,
    )
    return engine, client, bus, http, auth


async def make_task(repo: Repository, settings: Settings, entries: list[SongEntry]) -> TaskRecord:
    """建任务并入队."""
    now = int(time.time())
    task = TaskRecord(
        id=new_task_id(),
        name="测试任务",
        source_type="manual",
        source_id="",
        source_ref="",
        save_root=settings.paths.save_root,
        quality_chain=settings.quality.chain,
        options={},
        status=TaskStatus.PENDING,
        pause_reason="",
        total_items=len(entries),
        est_total_bytes=0,
        created_at=now,
        updated_at=now,
    )
    await repo.create_task(task)
    await repo.add_items(task.id, [(entry, "MP3_128", True) for entry in entries])
    return task


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #


async def test_downloads_serially_one_song_per_url_call(repo: Repository, settings: Settings) -> None:
    """每次取链只含一首歌; 取一首下一首, 不批量预取."""
    entries = [make_entry("mid1", "歌一"), make_entry("mid2", "歌二"), make_entry("mid3", "歌三")]
    payloads = {f"M500{entry.songmid}.mp3": b"x" * FULL_SIZE for entry in entries}
    engine, client, _bus, http, auth = await build_engine(repo, settings, payloads)
    task = await make_task(repo, settings, entries)

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    assert auth.checks == 1, "任务开始前要做一次服务端凭证校验"
    assert len(client.song.url_calls) == 3
    for call in client.song.url_calls:
        mids = {info.mid for info in call}
        assert len(mids) == 1, "一次 get_song_urls 只能包含一首歌"

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 3

    for entry in entries:
        path = Path(settings.paths.save_root) / f"{entry.title} - 歌手.mp3"
        assert path.exists()
        assert path.stat().st_size == FULL_SIZE
        assert not path.with_name(path.name + ".part").exists(), ".part 应已改名"


async def test_multiple_qualities_in_single_call(repo: Repository, settings: Settings) -> None:
    """同一首歌的多个档位可以合并进一次请求."""
    settings.quality.chain = ["FLAC", "MP3_320", "MP3_128"]
    entry = make_entry("mid1", "歌一")
    entry.sizes = {"FLAC": FULL_SIZE, "MP3_320": FULL_SIZE, "MP3_128": FULL_SIZE}
    payloads = {"F000mid1.flac": b"y" * FULL_SIZE}
    engine, client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    task = await make_task(repo, settings, [entry])
    await repo.update_item((await repo.list_items(task.id))[0].id, requested_quality="FLAC")

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    assert len(client.song.url_calls) == 1
    call = client.song.url_calls[0]
    assert len(call) == 3, "该首歌的三个档位应在同一次请求里"
    assert {info.mid for info in call} == {"mid1"}


async def test_trial_is_not_counted_as_success(repo: Repository, settings: Settings) -> None:
    """服务端返回试听片段时不能记成功."""
    settings.quality.reject_trial = False  # 关掉拒绝, 才会落到 trial 状态
    entry = make_entry("mid1", "歌一")
    payloads = {"RS02mid1.mp3": b"z" * TRIAL_SIZE}
    engine, client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    client.song.behaviour["mid1"] = "trial"
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 0
    assert counts[ItemStatus.TRIAL.value] == 1

    item = (await repo.list_items(task.id))[0]
    assert item.trial_reason
    assert item.status is ItemStatus.TRIAL


async def test_reject_trial_marks_unavailable_when_no_other_quality(
    repo: Repository,
    settings: Settings,
) -> None:
    """开启「拒绝试听」且没有别的档位可试时, 记为受限而不是成功."""
    settings.quality.reject_trial = True
    entry = make_entry("mid1", "歌一")
    engine, client, _bus, http, _auth = await build_engine(repo, settings, {})
    client.song.behaviour["mid1"] = "trial"
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 0
    assert counts[ItemStatus.UNAVAILABLE.value] == 1


async def test_no_permission_is_unavailable_and_queue_continues(
    repo: Repository,
    settings: Settings,
) -> None:
    """104003 记受限, 队列继续跑下一首."""
    entries = [make_entry("mid1", "歌一"), make_entry("mid2", "歌二")]
    payloads = {"M500mid2.mp3": b"x" * FULL_SIZE}
    engine, client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    client.song.behaviour["mid1"] = "denied"
    task = await make_task(repo, settings, entries)

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.UNAVAILABLE.value] == 1
    assert counts[ItemStatus.SUCCESS.value] == 1, "单曲失败不该中断队列"


async def test_offline_song_never_requests_url(repo: Repository, settings: Settings) -> None:
    """已下架的歌在列表阶段就判定, 不浪费取链请求."""
    entry = make_entry("mid1", "歌一")
    entry.status = 1
    engine, client, _bus, http, _auth = await build_engine(repo, settings, {})
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    assert client.song.url_calls == []
    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.UNAVAILABLE.value] == 1


async def test_dedupe_by_songmid_and_quality(repo: Repository, settings: Settings, tmp_path: Path) -> None:
    """判重依据是 songmid + 目标音质, 命中直接 skipped."""
    entry = make_entry("mid1", "歌一")
    existing = tmp_path / "existing.mp3"
    existing.write_bytes(b"old")
    await repo.record_download("mid1", "MP3_128", str(existing), 3)

    engine, client, _bus, http, _auth = await build_engine(repo, settings, {})
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    assert client.song.url_calls == [], "命中去重就不该再取链"
    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SKIPPED.value] == 1


async def test_resume_from_part_file(repo: Repository, settings: Settings) -> None:
    """已有 .part 残片时走 Range 续传."""
    entry = make_entry("mid1", "歌一")
    body = bytes(range(256)) * (FULL_SIZE // 256)
    payloads = {"M500mid1.mp3": body}

    target_dir = Path(settings.paths.save_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    part = target_dir / "歌一 - 歌手.mp3.part"
    part.write_bytes(body[:10_000])

    engine, _client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    final = target_dir / "歌一 - 歌手.mp3"
    assert final.exists()
    assert final.read_bytes() == body, "续传拼出来的内容必须与完整文件一致"
    assert not part.exists()


async def test_records_download_index_for_dedupe(repo: Repository, settings: Settings) -> None:
    """成功后写入全局去重索引."""
    entry = make_entry("mid1", "歌一")
    payloads = {"M500mid1.mp3": b"x" * FULL_SIZE}
    engine, _client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    record = await repo.find_download("mid1", "MP3_128")
    assert record is not None
    assert record["file_size"] == FULL_SIZE


async def test_short_file_detected_before_download_completes(repo: Repository, settings: Settings) -> None:
    """HEAD 探到的大小与 size_* 不符时, 下载前就判为试听, 不浪费时间."""
    settings.quality.reject_trial = True
    entry = make_entry("mid1", "歌一")
    payloads = {"M500mid1.mp3": b"x" * TRIAL_SIZE}  # 只有 4000, 预期 40000
    engine, _client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 0
    assert counts[ItemStatus.UNAVAILABLE.value] == 1
    assert not (Path(settings.paths.save_root) / "歌一 - 歌手.mp3").exists()


async def test_report_separates_trial_from_success(repo: Repository, settings: Settings) -> None:
    """汇总报告里试听独立成栏."""
    from qmdler.core.report import build_report

    settings.quality.reject_trial = False
    entries = [make_entry("mid1", "歌一"), make_entry("mid2", "歌二")]
    payloads = {"M500mid2.mp3": b"x" * FULL_SIZE, "RS02mid1.mp3": b"z" * TRIAL_SIZE}
    engine, client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    client.song.behaviour["mid1"] = "trial"
    task = await make_task(repo, settings, entries)

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    report = await build_report(repo, task.id)
    assert report is not None
    payload = report.as_dict()
    assert payload["counts"]["success"] == 1
    assert payload["counts"]["trial"] == 1
    assert len(payload["trials"]) == 1
    assert payload["trials"][0]["title"] == "歌一"
