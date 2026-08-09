"""下载引擎端到端流程 (全 mock, 不触网).

重点验证几条硬约束:
* 每次 ``get_song_urls`` 只包含**一首**歌;
* 取一首 → 立即下载 → 落盘 → 等间隔 → 取下一首, 不是先批量取链;
* 试听检出记 ``trial``, 不混进 ``success``;
* 判重命中记 ``skipped``;
* 单曲失败不中断队列.
"""

from __future__ import annotations

import asyncio
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
        self.cdn_nodes = list(FakeSongApi.cdn_nodes)

    #: dispatch 返回的节点列表, 单个用例可覆盖.
    cdn_nodes: ClassVar[list[str]] = [CDN_BASE]

    async def get_cdn_dispatch(self) -> Any:
        nodes = self.cdn_nodes

        class Response:
            retcode = 0
            sip = nodes
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


def make_http(payloads: dict[str, bytes], serving_hosts: set[str] | None = None) -> httpx.AsyncClient:
    """按 URL 返回固定内容的假 CDN.

    Args:
        payloads: 路径 → 内容.
        serving_hosts: 只有这些 host 放行, 其余一律 403 —— 复刻实测中
            「同一条 purl 只有个别节点给下」的真实行为.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if serving_hosts is not None and request.url.host not in serving_hosts:
            return httpx.Response(403)
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


async def build_engine(
    repo: Repository,
    settings: Settings,
    payloads: dict[str, bytes],
    *,
    cdn_nodes: list[str] | None = None,
    serving_hosts: set[str] | None = None,
) -> tuple[Any, ...]:
    """组装引擎."""
    client = FakeClient()
    if cdn_nodes is not None:
        client.song.cdn_nodes = cdn_nodes
    bus = EventBus()
    http = make_http(payloads, serving_hosts)
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


# --------------------------------------------------------------------------- #
# CDN 节点轮换
#
# 实测 (2026-08, 未登录取试听档) 发现: dispatch 返回的 6 个节点里, 同一条 purl
# 只有 1 个放行, 其余全部 403 —— 但这 6 个节点的 keepalive 探针都返回 200。
# 所以 403 是「这个节点不给这条链接」, 不是链接失效, 更不是节点挂了。
# --------------------------------------------------------------------------- #

REJECTING_NODES = [
    "https://a.example.com/",
    "https://b.example.com/",
    "https://good.example.com/",
    "https://d.example.com/",
]


async def test_rotates_cdn_nodes_without_refetching_vkey(repo: Repository, settings: Settings) -> None:
    """多数节点 403 时换节点重试同一条 purl, 不重新取 vkey.

    为 403 去重新取 vkey 会把每首歌的接口请求数翻好几倍, 正是最该避免的
    风控特征。
    """
    entry = make_entry("mid1", "歌一")
    payloads = {"M500mid1.mp3": b"x" * FULL_SIZE}
    engine, client, _bus, http, _auth = await build_engine(
        repo,
        settings,
        payloads,
        cdn_nodes=REJECTING_NODES,
        serving_hosts={"good.example.com"},
    )
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 1, "只要有一个节点放行就该下成功"
    assert len(client.song.url_calls) == 1, "403 不该触发重新取 vkey"

    item = (await repo.list_items(task.id))[0]
    assert Path(item.target_path).exists()


async def test_prefers_known_good_node_on_subsequent_songs(repo: Repository, settings: Settings) -> None:
    """第一首摸清哪个节点能用之后, 后面的歌优先走它, 少打无谓的 403 请求."""
    entries = [make_entry(f"mid{i}", f"歌{i}") for i in range(1, 5)]
    payloads = {f"M500{e.songmid}.mp3": b"x" * FULL_SIZE for e in entries}
    engine, client, _bus, http, _auth = await build_engine(
        repo,
        settings,
        payloads,
        cdn_nodes=REJECTING_NODES,
        serving_hosts={"good.example.com"},
    )
    task = await make_task(repo, settings, entries)

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 4
    assert len(client.song.url_calls) == 4, "每首歌仍然只取一次链"

    snapshot = engine._cdn.snapshot
    good = next(node for node in snapshot["nodes"] if "good" in node["base"])
    assert good["successes"] >= 4
    assert good["tier"] == 0, "成功过的节点应当排在最前"
    assert good["rejections_since_success"] == 0, "成功要清零拒绝计数"

    # 摸清之后就不该再往被拒的节点上撞: 只有第一首可能撞, 上限是 节点数-1。
    total_rejections = sum(node["rejections_since_success"] for node in snapshot["nodes"])
    assert total_rejections <= len(REJECTING_NODES) - 1, f"仍在反复撞被拒节点: {snapshot['nodes']}"


async def test_all_nodes_rejecting_refetches_vkey_once(repo: Repository, settings: Settings) -> None:
    """全部节点都拒绝时才值得重新取一次 vkey."""
    entry = make_entry("mid1", "歌一")
    engine, client, _bus, http, _auth = await build_engine(
        repo,
        settings,
        {},
        cdn_nodes=REJECTING_NODES,
        serving_hosts=set(),  # 没有任何节点放行
    )
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    assert len(client.song.url_calls) == 2, "应当且只应当重新取一次 vkey"
    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 0
    assert counts[ItemStatus.UNAVAILABLE.value] == 1


# --------------------------------------------------------------------------- #
# --single 诊断模式
# --------------------------------------------------------------------------- #


async def test_diagnosis_records_every_layer(repo: Repository, settings: Settings) -> None:
    """四层校验每一层的实际判定值都要落进诊断记录."""
    entry = make_entry("mid1", "歌一")
    payloads = {"M500mid1.mp3": b"x" * FULL_SIZE}
    engine, _client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    diagnosis = engine.enable_diagnosis()
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    payload = diagnosis.as_dict()
    names = [layer["name"] for layer in payload["layers"]]
    assert any("第1层" in name for name in names)
    assert any("第2层" in name for name in names)
    assert any("第3层" in name and "下载前" in name for name in names)
    assert any("第3层" in name and "落盘后" in name for name in names)
    assert any("第4层" in name for name in names)

    assert payload["verdict"] == "success"
    assert payload["requested_start_code"] == "M500"
    assert payload["extracted_prefix"] == "M500"
    assert payload["probe_method"] in ("head", "range")
    assert payload["cdn_used"]
    assert payload["content_length"] == FULL_SIZE
    assert payload["downloaded_bytes"] == FULL_SIZE
    # sa 原样透传, 供实测标定
    assert payload["sa"] == entry.sa
    assert payload["trial_window_source"]


async def test_diagnosis_never_leaks_secrets(repo: Repository, settings: Settings) -> None:
    """诊断输出里绝不能出现 vkey / musickey / 完整 purl."""
    import json

    from qmdler.core.download.diagnose import contains_secret

    entry = make_entry("mid1", "歌一")
    payloads = {"M500mid1.mp3": b"x" * FULL_SIZE}
    engine, _client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    diagnosis = engine.enable_diagnosis()
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    text = json.dumps(diagnosis.as_dict(), ensure_ascii=False)
    assert contains_secret(text) == ""
    # purl 只留最后一个路径段
    assert diagnosis.purl_filename == "M500mid1.mp3"
    assert "?" not in diagnosis.purl_filename


async def test_diagnosis_records_result_code_when_unavailable(repo: Repository, settings: Settings) -> None:
    """拿不到 purl 时也要记下 result —— 「为什么下不了」全靠它."""
    entry = make_entry("mid1", "歌一")
    engine, client, _bus, http, _auth = await build_engine(repo, settings, {})
    client.song.behaviour["mid1"] = "denied"
    diagnosis = engine.enable_diagnosis()
    task = await make_task(repo, settings, [entry])

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    payload = diagnosis.as_dict()
    assert payload["result_code"] == 104003
    assert payload["attempted_quality"] == "MP3_128"
    assert payload["verdict"] == "unavailable"


async def test_diagnosis_skips_download_interval(repo: Repository, settings: Settings) -> None:
    """诊断模式跳过间隔, 不然一首歌要等 3 分钟."""
    settings.download.interval_seconds = 600.0
    entries = [make_entry("mid1", "歌一"), make_entry("mid2", "歌二")]
    payloads = {f"M500{e.songmid}.mp3": b"x" * FULL_SIZE for e in entries}
    engine, _client, _bus, http, _auth = await build_engine(repo, settings, payloads)
    engine.enable_diagnosis()
    task = await make_task(repo, settings, entries)

    await engine.start(task.id)
    await asyncio.wait_for(engine.wait_idle(), timeout=20)
    await http.aclose()

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 2


async def test_rejections_do_not_accumulate_across_songs(repo: Repository, settings: Settings) -> None:
    """「这条 purl 被谁拒过」是 per-purl 的, 换歌就丢.

    若把拒绝记录攒在节点上跨曲目累积, 跑一阵之后所有节点都会带着拒绝记录,
    分档退化成随机 —— 这轮的修复会在第 20 首左右悄悄失效.
    """
    entries = [make_entry(f"mid{i}", f"歌{i}") for i in range(1, 6)]
    payloads = {f"M500{e.songmid}.mp3": b"x" * FULL_SIZE for e in entries}
    engine, _client, _bus, http, _auth = await build_engine(
        repo,
        settings,
        payloads,
        cdn_nodes=REJECTING_NODES,
        serving_hosts={"good.example.com"},
    )
    task = await make_task(repo, settings, entries)

    await engine.start(task.id)
    await engine.wait_idle()
    await http.aclose()

    counts = await repo.status_counts(task.id)
    assert counts[ItemStatus.SUCCESS.value] == 5

    # per-purl 记录在每首歌开始时清空, 跑完不该留下一大堆
    assert len(engine._purl_rejections) <= 1, f"per-purl 拒绝记录在累积: {engine._purl_rejections}"

    # 放行节点始终是 tier 0，从未被降级
    snapshot = engine._cdn.snapshot
    good = next(node for node in snapshot["nodes"] if "good" in node["base"])
    assert good["tier"] == 0


async def test_success_clears_rejection_penalty(repo: Repository, settings: Settings) -> None:
    """一次成功就清零拒绝计数 —— 拒绝不是永久污点, 网络环境变了能跟着换."""
    from qmdler.core.download.cdn import CdnManager, CdnNode

    manager = CdnManager(client=None)  # type: ignore[arg-type]
    node = CdnNode(base="https://x/")

    manager.report_rejection(node)
    manager.report_rejection(node)
    assert node.tier == 2, "连续被拒应当降到最后一档"

    manager.report_success(node)
    assert node.rejections_since_success == 0
    assert node.tier == 0, "成功之后应当立刻回到首选"


async def test_recent_success_expires(repo: Repository, settings: Settings) -> None:
    """「最近成功过」会过期 —— 免得死守一个早就不放行的节点."""
    import time as time_module

    from qmdler.core.download.cdn import SUCCESS_TTL, CdnNode

    node = CdnNode(base="https://x/", last_success=time_module.time() - SUCCESS_TTL - 1)
    assert node.tier == 1, "成功记录过期后退回「情况未知」，重新参与试探"
