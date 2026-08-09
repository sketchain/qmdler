"""领域模型.

这里定义的是 qmdler 自己的内部模型, 与 ``qqmusic_api`` 的响应模型解耦:
库的模型随版本变动, 而这些模型要落库、要通过 REST/WS 传给两个前端, 必须稳定.
``from_api_song`` 是唯一的转换入口.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from qqmusic_api.models.base import Song as ApiSong
from qqmusic_api.modules.song import SongFileType

# --------------------------------------------------------------------------- #
# 音质
# --------------------------------------------------------------------------- #

#: 音质档位 -> (展示名, 取大小的字段路径)
#:
#: ``size_field`` 为 ``("size_new", i)`` 表示取 ``file.size_new[i]``, 其余为
#: ``File`` 上的直接属性名. 索引含义见 ``qqmusic_api.models.base.File`` 的 docstring.
QUALITY_TABLE: dict[str, tuple[str, tuple[str, int] | str]] = {
    "DTS_X": ("DTS:X 5.1", ("size_new", 9)),
    "MASTER": ("臻品母带 24Bit/192kHz", ("size_new", 0)),
    "ATMOS_2": ("臻品音质 2.0 (银河音效)", ("size_new", 1)),
    "ATMOS_51": ("臻品全景声 5.1", ("size_new", 2)),
    "ATMOS_71": ("臻品全景声 7.1.4", ("size_new", 6)),
    "ATMOS_DB": ("杜比全景声", "size_dolby"),
    "NAC": ("TQ 标准音质 (NAC)", ("size_new", 7)),
    "FLAC": ("SQ 无损 FLAC", "size_flac"),
    "OGG_640": ("SQ 无损 OGG 640k", ("size_new", 5)),
    "OGG_320": ("HQ 高品质 OGG 320k", ("size_new", 3)),
    "OGG_192": ("HQ 高品质 OGG 192k", "size_192ogg"),
    "OGG_96": ("流畅 OGG 96k", "size_96ogg"),
    "MP3_320": ("HQ 高品质 MP3 320k", "size_320mp3"),
    "MP3_128": ("标准音质 MP3 128k", "size_128mp3"),
    "ACC_192": ("HQ 高品质 AAC 192k", "size_192aac"),
    "ACC_96": ("流畅 AAC 96k (可能为试听)", "size_96aac"),
    "ACC_48": ("低品质 AAC 48k", "size_48aac"),
}

#: 档位级别的告警, 选之前就要让用户看见, 而不是下完才发现.
#:
#: ``NAC`` (``TL01``, ``.nac``) 是腾讯自研容器. 实测 (2026-08):
#: ``ffprobe`` / ``ffmpeg`` 一律 ``Invalid data found when processing input``,
#: mutagen 既读不出时长也写不了 tag —— 普通播放器、元数据、时长校验三样全废.
#: 文件本身能下下来 (字节数与 ``size_new[7]`` 一致), 但基本没法用.
QUALITY_CAVEATS: dict[str, str] = {
    "NAC": "普通播放器不识别；不支持元数据写入与时长校验（第 4 层校验会被跳过）",
}


def quality_caveat(code: str) -> str:
    """该档位的告警文案, 没有则为空串."""
    return QUALITY_CAVEATS.get(code, "")


#: 默认优先级链 (从高到低). 不含加密档位 —— 加密档位下载下来无法播放.
DEFAULT_QUALITY_CHAIN: list[str] = [
    "FLAC",
    "OGG_640",
    "MP3_320",
    "OGG_320",
    "ACC_192",
    "MP3_128",
]

#: 试听片段 / 伴奏等特殊档位的编码前缀, 用于 filename 前缀校验.
#: 注意 ``SpecialSongFileType.ACCOM`` 的编码同样是 ``O801`` (与 ``OGG_640`` 相同),
#: 但两者请求时传的 media mid 不同 (伴奏取 ``Song.vs[9]``), 因此不会混淆.
TRIAL_PREFIXES: frozenset[str] = frozenset({"RS02", "O802"})


def quality_label(code: str) -> str:
    """返回音质档位的中文展示名."""
    entry = QUALITY_TABLE.get(code)
    return entry[0] if entry else code


def quality_extension(code: str) -> str:
    """返回音质档位对应的文件扩展名 (含点)."""
    try:
        return SongFileType[code].e
    except KeyError:
        return ".bin"


def quality_start_code(code: str) -> str:
    """返回音质档位的四字符编码前缀 (如 ``F000``)."""
    try:
        return SongFileType[code].s
    except KeyError:
        return ""


def all_quality_codes() -> list[str]:
    """返回全部普通音质档位, 顺序与 ``QUALITY_TABLE`` 一致 (由高到低)."""
    return list(QUALITY_TABLE)


# --------------------------------------------------------------------------- #
# 状态枚举
# --------------------------------------------------------------------------- #


class ItemStatus(str, Enum):
    """单曲状态."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    TRIAL = "trial"


#: 终态: 引擎不会再处理这些条目.
TERMINAL_ITEM_STATUSES: frozenset[ItemStatus] = frozenset(
    {
        ItemStatus.SUCCESS,
        ItemStatus.FAILED,
        ItemStatus.SKIPPED,
        ItemStatus.UNAVAILABLE,
        ItemStatus.TRIAL,
    },
)


class TaskStatus(str, Enum):
    """任务状态."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    PAUSED_RATELIMITED = "paused_ratelimited"
    PAUSED_AUTH = "paused_auth"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


#: 任一暂停态, UI 上都要停在原地等用户操作.
PAUSED_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.PAUSED,
        TaskStatus.PAUSED_RATELIMITED,
        TaskStatus.PAUSED_AUTH,
    },
)


class SubStatus(str, Enum):
    """歌词 / 封面 / tag 的子状态. 子状态失败不影响整首歌成功."""

    SKIPPED = "skipped"
    OK = "ok"
    NONE = "none"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class TrialReason(str, Enum):
    """试听 / 静默降级的检出依据."""

    FILENAME_PREFIX = "filename_prefix"
    SIZE_TRY_MATCH = "size_try_match"
    SIZE_MISMATCH = "size_mismatch"
    DURATION_SHORT = "duration_short"


class SourceType(str, Enum):
    """歌单来源."""

    CREATED = "created"
    FAV_SONGLIST = "fav_songlist"
    FAV_SONG = "fav_song"
    SONGLIST = "songlist"
    ALBUM = "album"
    SINGER = "singer"
    SEARCH = "search"
    MANUAL = "manual"


# --------------------------------------------------------------------------- #
# 歌曲快照
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SingerRef:
    """歌手引用."""

    mid: str
    name: str

    def as_dict(self) -> dict[str, str]:
        """转为可 JSON 序列化的字典."""
        return {"mid": self.mid, "name": self.name}


@dataclass(slots=True)
class SongEntry:
    """一首歌在列表阶段的完整快照.

    列表接口 (``songlist.get_detail`` / ``album.get_song`` / ``singer.get_songs_list`` /
    ``user.get_fav_song`` / ``search_by_type``) 返回的都是完整的 ``Song`` 对象, 且
    ``file`` / ``pay`` 是必填字段, 因此这里的信息在拉列表时就已全部到手, 不需要为
    每首歌额外调用 ``query_song``.
    """

    songmid: str
    songid: int
    title: str
    singers: list[SingerRef]
    album_name: str
    album_mid: str
    media_mid: str
    song_type: int
    interval: int
    track_no: int
    disc_no: int
    year: str
    #: 上下架状态: 0 正常 / 1 下架 / 2 待审核 / 3 仅限部分区域播放
    status: int
    #: 64 位权益位掩码. 其位布局在库源码与官方文档中均无定义, 因此仅原样记录展示,
    #: 不参与任何选档判断 —— 猜测位含义会造成静默错判.
    sa: int
    #: 各档位文件大小 (字节). 值为 0 表示该档位不存在.
    sizes: dict[str, int]
    #: pay_month / pay_play / pay_down / pay_status / price_track / price_album / time_free
    pay: dict[str, int]
    size_try: int
    try_begin_ms: int
    try_end_ms: int
    #: ``Song.vs``: 关联版本与高级媒体 MID 列表 (试听/伴奏等特殊档位要用其中的 media mid).
    vs: list[str]
    #: ``Song.vi``: [4] 试听开始毫秒, [5] 试听结束毫秒.
    vi: list[int]
    #: ``Song.vf``: ReplayGain 三元组 (增益 dB / 峰值 / 响度范围).
    vf: list[float]

    # -- 派生属性 ---------------------------------------------------------- #

    def cover_url(self, size: int = 500) -> str:
        """封面链接.

        复刻 ``Song.cover_url`` 的策略: 专辑封面优先, 退到首个可用歌手封面,
        都没有则返回空串. 用库自己的模型来拼 URL, 不重复实现拼接规则 —— 这样
        库改了 CDN 路径我们跟着变.
        """
        from qqmusic_api.models.base import Album as ApiAlbum
        from qqmusic_api.models.base import CoverSize
        from qqmusic_api.models.base import Singer as ApiSinger

        if self.album_mid:
            return ApiAlbum(mid=self.album_mid).cover_url(cast("CoverSize", size))
        for singer in self.singers:
            if singer.mid:
                return ApiSinger(mid=singer.mid).cover_url(cast("CoverSize", size))
        return ""

    @property
    def singer_names(self) -> list[str]:
        """歌手名列表."""
        return [s.name for s in self.singers]

    @property
    def available_qualities(self) -> dict[str, int]:
        """仅保留 size > 0 的档位."""
        return {code: size for code, size in self.sizes.items() if size > 0}

    @property
    def is_offline(self) -> bool:
        """是否已下架 / 待审核 (必然拿不到文件, 列表阶段即可过滤)."""
        return self.status in (1, 2)

    @property
    def is_region_limited(self) -> bool:
        """是否仅限部分区域播放."""
        return self.status == 3

    @property
    def trial_windows(self) -> list[tuple[int, int, str]]:
        """全部已知的试听窗口 ``(开始ms, 结束ms, 来源)``.

        ``file.try_begin/try_end`` 与 ``vi[4]/vi[5]`` **不是主备关系, 而是两个
        并列候选**. 实测「晴天」: ``file`` 给 84346~142843ms (58.5s), ``vi`` 给
        84346~114346ms (30s) —— 差距太大, 不像取整误差, 更像是不同档位各有各的
        试听区间.

        所以两个都要留着, 时长校验时**任意一个命中就判 trial**. 漏判 trial 正是
        这个项目最怕的方向, 不能因为选了其中一个就对另一个视而不见.
        """
        windows: list[tuple[int, int, str]] = []
        if self.try_end_ms > self.try_begin_ms:
            windows.append((self.try_begin_ms, self.try_end_ms, "file.try_begin/try_end"))
        if len(self.vi) > 5:
            begin, end = self.vi[4], self.vi[5]
            if end > begin and (begin, end) != (self.try_begin_ms, self.try_end_ms):
                windows.append((begin, end, "vi[4]/vi[5]"))
        return windows

    @property
    def trial_window_ms(self) -> tuple[int, int]:
        """展示用的默认窗口 (``file`` 优先). **判定不要只用这一个**, 见
        :attr:`trial_windows`."""
        windows = self.trial_windows
        return (windows[0][0], windows[0][1]) if windows else (0, 0)

    @property
    def trial_window_with_source(self) -> tuple[int, int, str]:
        """展示用的默认窗口及其来源. 判定请用 :attr:`trial_windows`."""
        windows = self.trial_windows
        return windows[0] if windows else (0, 0, "(无)")

    @property
    def replaygain(self) -> tuple[float, float] | None:
        """从 ``vf`` 取 (增益 dB, 峰值). 数据缺失时返回 ``None``."""
        if len(self.vf) < 2:
            return None
        gain, peak = self.vf[0], self.vf[1]
        if gain == 0.0 and peak == 0.0:
            return None
        return gain, peak

    def special_media_mid(self, index: int) -> str:
        """取 ``vs[index]`` 的 media mid, 越界返回空串."""
        return self.vs[index] if 0 <= index < len(self.vs) else ""

    # -- 序列化 ------------------------------------------------------------ #

    def as_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典 (REST/WS 出参与落库共用)."""
        return {
            "songmid": self.songmid,
            "songid": self.songid,
            "title": self.title,
            "singers": [s.as_dict() for s in self.singers],
            "album_name": self.album_name,
            "album_mid": self.album_mid,
            "media_mid": self.media_mid,
            "song_type": self.song_type,
            "interval": self.interval,
            "track_no": self.track_no,
            "disc_no": self.disc_no,
            "year": self.year,
            "status": self.status,
            "sa": self.sa,
            "sizes": self.sizes,
            "pay": self.pay,
            "size_try": self.size_try,
            "try_begin_ms": self.try_begin_ms,
            "try_end_ms": self.try_end_ms,
            "vs": self.vs,
            "vi": self.vi,
            "vf": self.vf,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SongEntry:
        """从 :meth:`as_dict` 的输出还原."""
        return cls(
            songmid=data["songmid"],
            songid=data.get("songid", 0),
            title=data.get("title", ""),
            singers=[SingerRef(mid=s.get("mid", ""), name=s.get("name", "")) for s in data.get("singers", [])],
            album_name=data.get("album_name", ""),
            album_mid=data.get("album_mid", ""),
            media_mid=data.get("media_mid", ""),
            song_type=data.get("song_type", 0),
            interval=data.get("interval", 0),
            track_no=data.get("track_no", 0),
            disc_no=data.get("disc_no", 0),
            year=data.get("year", ""),
            status=data.get("status", 0),
            sa=data.get("sa", 0),
            sizes=dict(data.get("sizes", {})),
            pay=dict(data.get("pay", {})),
            size_try=data.get("size_try", 0),
            try_begin_ms=data.get("try_begin_ms", 0),
            try_end_ms=data.get("try_end_ms", 0),
            vs=list(data.get("vs", [])),
            vi=list(data.get("vi", [])),
            vf=list(data.get("vf", [])),
        )


def _size_of(file_obj: Any, spec: tuple[str, int] | str) -> int:
    """按 :data:`QUALITY_TABLE` 的取值规则从 ``File`` 上读出档位大小."""
    if isinstance(spec, str):
        return int(getattr(file_obj, spec, 0) or 0)
    attr, index = spec
    values = getattr(file_obj, attr, None) or []
    if index < len(values):
        return int(values[index] or 0)
    return 0


def from_api_song(song: ApiSong) -> SongEntry:
    """把 ``qqmusic_api`` 的 ``Song`` 转成内部快照.

    这是库模型进入 qmdler 的唯一入口 —— 库升级时只需要改这一个函数.
    """
    file_obj = song.file
    sizes = {code: _size_of(file_obj, spec) for code, (_, spec) in QUALITY_TABLE.items()}
    return SongEntry(
        songmid=song.mid,
        songid=song.id,
        title=song.title or song.name,
        singers=[SingerRef(mid=s.mid, name=s.name or s.title) for s in song.singer],
        album_name=song.album.name or song.album.title,
        album_mid=song.album.mid,
        media_mid=file_obj.media_mid,
        song_type=song.type,
        interval=song.interval,
        track_no=song.index_album,
        disc_no=song.index_cd,
        # {年份} 优先取 Song.time_public, 回退到 Album.time_public.
        year=song.time_public or song.album.time_public,
        status=song.status,
        sa=song.sa,
        sizes=sizes,
        pay={
            "pay_month": song.pay.pay_month,
            "pay_play": song.pay.pay_play,
            "pay_down": song.pay.pay_down,
            "pay_status": song.pay.pay_status,
            "price_track": song.pay.price_track,
            "price_album": song.pay.price_album,
            "time_free": song.pay.time_free,
        },
        size_try=file_obj.size_try,
        try_begin_ms=file_obj.try_begin,
        try_end_ms=file_obj.try_end,
        vs=list(song.vs),
        vi=list(song.vi),
        vf=list(song.vf),
    )


# --------------------------------------------------------------------------- #
# 任务 / 条目记录 (与数据库行一一对应)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TaskRecord:
    """一个下载任务."""

    id: str
    name: str
    source_type: str
    source_id: str
    source_ref: str
    save_root: str
    quality_chain: list[str]
    options: dict[str, Any]
    status: TaskStatus
    pause_reason: str
    total_items: int
    est_total_bytes: int
    created_at: int
    updated_at: int
    started_at: int | None = None
    finished_at: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典."""
        return {
            "id": self.id,
            "name": self.name,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_ref": self.source_ref,
            "save_root": self.save_root,
            "quality_chain": self.quality_chain,
            "options": self.options,
            "status": self.status.value,
            "pause_reason": self.pause_reason,
            "total_items": self.total_items,
            "est_total_bytes": self.est_total_bytes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(slots=True)
class ItemRecord:
    """任务队列中的一首歌."""

    id: int
    task_id: str
    position: int
    entry: SongEntry
    selected: bool
    status: ItemStatus
    requested_quality: str
    actual_quality: str
    degraded: bool
    trial_reason: str
    target_path: str
    bytes_expected: int
    bytes_downloaded: int
    api_result_code: int | None
    error_type: str
    error_message: str
    retry_count: int
    lyric_status: SubStatus
    cover_status: SubStatus
    tag_status: SubStatus
    #: 四层校验里有层没跑成 (如 ``.nac`` 读不出时长). ``success`` 但校验不完整,
    #: 汇总报告要单列, 否则会给出「全部通过四层校验」的假象.
    verify_incomplete: bool
    created_at: int
    updated_at: int
    started_at: int | None = None
    finished_at: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典 (前端表格/卡片直接消费)."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "position": self.position,
            "song": self.entry.as_dict(),
            "title": self.entry.title,
            "singers": "、".join(self.entry.singer_names),
            "album": self.entry.album_name,
            "interval": self.entry.interval,
            "selected": self.selected,
            "status": self.status.value,
            "requested_quality": self.requested_quality,
            "requested_quality_label": quality_label(self.requested_quality),
            "actual_quality": self.actual_quality,
            "actual_quality_label": quality_label(self.actual_quality) if self.actual_quality else "",
            "degraded": self.degraded,
            "degrade_note": (
                f"已降级：{quality_label(self.requested_quality)} → {quality_label(self.actual_quality)}"
                if self.degraded and self.actual_quality
                else ""
            ),
            "trial_reason": self.trial_reason,
            "target_path": self.target_path,
            "bytes_expected": self.bytes_expected,
            "bytes_downloaded": self.bytes_downloaded,
            "progress": (
                round(self.bytes_downloaded / self.bytes_expected, 4)
                if self.bytes_expected > 0
                else (1.0 if self.status is ItemStatus.SUCCESS else 0.0)
            ),
            "api_result_code": self.api_result_code,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "lyric_status": self.lyric_status.value,
            "cover_status": self.cover_status.value,
            "tag_status": self.tag_status.value,
            "verify_incomplete": self.verify_incomplete,
            "pay_flags": self.entry.pay,
            "available_qualities": self.entry.available_qualities,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def dumps(value: Any) -> str:
    """紧凑 JSON 序列化 (落库用)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    """宽容的 JSON 反序列化: 损坏或为空时返回 ``default``."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
