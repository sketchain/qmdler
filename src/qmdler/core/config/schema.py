"""配置模型.

全部配置持久化为 TOML (路径遵循 XDG), 并支持环境变量覆盖, 前缀 ``QMDLER_``,
嵌套用 ``__`` 分隔, 例如::

    QMDLER_DOWNLOAD__INTERVAL_SECONDS=240
    QMDLER_SERVER__PORT=8800
    QMDLER_PATHS__SAVE_ROOT=/data/music
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from ..models import DEFAULT_QUALITY_CHAIN, QUALITY_TABLE

CoverSize = Literal[150, 300, 500, 800, 1200, 1500]


class PathsConfig(BaseModel):
    """保存位置."""

    save_root: str = Field(default="~/Music/qmdler", description="默认下载根目录")
    #: 每个歌单单独的保存位置: ``{来源标识: 目录}``.
    per_source_roots: dict[str, str] = Field(default_factory=dict)
    #: 磁盘剩余空间低于预估总大小 + 该余量 (字节) 时告警.
    disk_headroom_bytes: int = 512 * 1024 * 1024


class QualityConfig(BaseModel):
    """音质选择."""

    #: 优先级链, 拖拽排序生成, 从高到低.
    chain: list[str] = Field(default_factory=lambda: list(DEFAULT_QUALITY_CHAIN))
    #: 全部档位都拿不到时: 跳过并记录 / 用最低可用档位兜底.
    on_all_unavailable: Literal["skip", "lowest"] = "skip"
    #: 拒绝试听文件 (检出试听/降级即判失败并尝试下一档).
    reject_trial: bool = True
    #: 检出试听后是否保留已落盘的文件.
    keep_trial_file: bool = False
    #: 请求档位与返回 filename 前缀不符时: 记为降级 / 判为失败.
    on_prefix_mismatch: Literal["degrade", "fail"] = "degrade"
    #: Content-Length 与 size_* 的容许偏差比例.
    size_tolerance: float = 0.02
    #: 实际时长短于 interval 的容许比例 (低于该比例判为试听).
    duration_tolerance: float = 0.9
    #: 高级选项: 在 UI 中暴露加密档位. 加密档位下载下来无法播放, 本项目不提供解密.
    expose_encrypted: bool = False
    #: 高级选项: 在 UI 中暴露特殊档位 (试听/伴奏/AI 演奏) 与铃声档位.
    expose_special: bool = False

    @field_validator("chain")
    @classmethod
    def _validate_chain(cls, value: list[str]) -> list[str]:
        unknown = [code for code in value if code not in QUALITY_TABLE]
        if unknown:
            raise ValueError(f"未知音质档位: {unknown}")
        if not value:
            raise ValueError("音质优先级链不能为空")
        # 去重且保序
        seen: set[str] = set()
        return [code for code in value if not (code in seen or seen.add(code))]  # type: ignore[func-returns-value]


class DownloadConfig(BaseModel):
    """下载节奏与重试."""

    #: 每首之间的间隔 (秒). ``random_interval`` 为 True 时作为区间下界.
    interval_seconds: float = 180.0
    random_interval: bool = False
    interval_min_seconds: float = 150.0
    interval_max_seconds: float = 210.0
    #: 元数据类请求 (歌词 / 封面 / 补全详情) 的轻量限速区间 (秒).
    metadata_delay_min: float = 0.5
    metadata_delay_max: float = 1.0
    #: 网络类异常的指数退避重试次数.
    max_retries: int = 3
    retry_backoff_base: float = 2.0
    #: 单次 HTTP 读超时 (秒).
    request_timeout: float = 60.0
    #: 文件名冲突策略.
    conflict_policy: Literal["skip", "overwrite", "suffix"] = "suffix"
    #: 启动时清理无主 ``.part`` 文件.
    cleanup_orphan_parts: bool = True
    #: 高级选项: 批量 query_song 补全详情 (默认关闭, 逐首请求).
    batch_query_song: bool = False
    batch_query_size: int = 20

    @model_validator(mode="after")
    def _validate_interval(self) -> DownloadConfig:
        if self.random_interval and self.interval_min_seconds > self.interval_max_seconds:
            raise ValueError("interval_min_seconds 不能大于 interval_max_seconds")
        if self.metadata_delay_min > self.metadata_delay_max:
            raise ValueError("metadata_delay_min 不能大于 metadata_delay_max")
        if self.batch_query_size < 1 or self.batch_query_size > 100:
            raise ValueError("batch_query_size 必须在 1~100 之间 (接口单次上限 100)")
        return self


class NamingConfig(BaseModel):
    """文件名与目录模板."""

    dir_template: str = "{歌单名}"
    file_template: str = "{歌名} - {歌手}"
    #: 多歌手连接符. 不要用 ``/``, 会与路径分隔符冲突.
    singer_separator: str = "、"
    #: 单段文件名的最大字节数 (UTF-8).
    max_segment_bytes: int = 120
    unknown_album: str = "未知专辑"
    unknown_artist: str = "未知艺术家"
    unknown_value: str = "未知"


class LyricConfig(BaseModel):
    """歌词."""

    enabled: bool = False
    #: 下载哪几路.
    original: bool = True
    translation: bool = False
    romaji: bool = False
    singing_annotations: bool = False
    multi_style_translation: bool = False
    #: 逐字歌词 (QRC). 关闭则直接取逐行 LRC.
    qrc: bool = True
    #: 输出格式.
    output_format: Literal["enhanced_lrc", "line_lrc", "raw_qrc"] = "enhanced_lrc"
    #: 双语处理.
    bilingual_mode: Literal["separate_file", "two_lines", "inline"] = "separate_file"
    #: 存放位置 (可多选).
    save_as_file: bool = True
    embed_in_tag: bool = True
    #: 独立歌词文件名模板, 留空则与音频同名.
    file_suffix: str = ""


class CoverConfig(BaseModel):
    """封面."""

    enabled: bool = False
    #: 嵌入音频用的尺寸.
    embed_size: CoverSize = 500
    #: 单独保存用的尺寸 (可以与嵌入尺寸不同).
    save_size: CoverSize = 1500
    embed: bool = True
    save_file: bool = False
    #: 单独保存的文件名模板, 支持占位符; ``cover.jpg`` 表示每目录一张.
    file_name: str = "cover.jpg"


class TagConfig(BaseModel):
    """Tag 字段开关. 默认全开."""

    enabled: bool = True
    title: bool = True
    artist: bool = True
    album: bool = True
    album_artist: bool = True
    composer: bool = True
    lyricist: bool = True
    year: bool = True
    track_number: bool = True
    disc_number: bool = True
    genre: bool = True
    cover: bool = True
    lyrics: bool = True
    #: 从 ``Song.vf`` 写入 ReplayGain (零额外请求).
    replaygain: bool = True

    @property
    def needs_producer(self) -> bool:
        """是否需要额外请求 ``song.get_producer`` (作曲/作词)."""
        return self.enabled and (self.composer or self.lyricist)

    @property
    def needs_album_detail(self) -> bool:
        """是否需要额外请求 ``album.get_detail`` (流派文本)."""
        return self.enabled and self.genre


class AuthConfig(BaseModel):
    """凭证与登录."""

    #: 后台主动刷新的检查间隔 (秒).
    refresh_check_interval: float = 30 * 60
    #: 距离过期不足该秒数时提前刷新.
    refresh_margin_seconds: float = 6 * 3600
    #: 二维码登录的整体超时 (秒).
    qrcode_timeout: float = 180.0
    #: 库内部令牌桶: 每秒请求数与突发容量. 默认比库的 10/50 保守得多.
    api_rate: float = 2.0
    api_capacity: float = 5.0


class ServerConfig(BaseModel):
    """服务端."""

    host: str = "127.0.0.1"
    port: int = 8770
    #: WebUI 允许的跨域来源. 默认同源, 不开放.
    cors_origins: list[str] = Field(default_factory=list)
    #: 迟到的 WS 客户端最多回放多少条历史事件.
    event_replay_limit: int = 200


class LogConfig(BaseModel):
    """日志."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    #: 留空表示不写文件.
    file: str = ""
    max_bytes: int = 8 * 1024 * 1024
    backup_count: int = 3


class Settings(BaseSettings):
    """完整配置."""

    model_config = SettingsConfigDict(
        env_prefix="QMDLER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """让**环境变量压过初始化参数**.

        pydantic-settings 的默认顺序是 ``init → env → dotenv → secrets``,
        **靠前的赢** —— 也就是说默认情况下 ``Settings(**file_data)`` 里的文件值
        会盖住环境变量, 与我们文档里承诺的「文件 < 环境变量」正好相反.

        这一条是实测发现的: ``QMDLER_DOWNLOAD__INTERVAL_SECONDS=99`` 配上
        文件里的 ``interval_seconds = 7``, 拿到的是 7. 容器部署时靠环境变量
        覆盖文件的用法因此完全不生效.
        """
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)

    preset: str = "default"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    lyric: LyricConfig = Field(default_factory=LyricConfig)
    cover: CoverConfig = Field(default_factory=CoverConfig)
    tag: TagConfig = Field(default_factory=TagConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    log: LogConfig = Field(default_factory=LogConfig)

    def as_dict(self) -> dict[str, Any]:
        """转为普通字典 (写 TOML / REST 出参)."""
        return self.model_dump(mode="json")


#: 内置配置预设. 用户可一键切换.
BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "无损归档": {
        "quality": {
            "chain": ["MASTER", "ATMOS_2", "FLAC", "OGG_640", "MP3_320"],
            "on_all_unavailable": "skip",
            "reject_trial": True,
        },
        "download": {"interval_seconds": 180.0, "random_interval": True},
        "naming": {
            "dir_template": "{歌手}/{专辑}",
            "file_template": "{曲序}. {歌名}",
        },
        "lyric": {"enabled": True, "qrc": True, "output_format": "enhanced_lrc", "translation": True},
        "cover": {"enabled": True, "embed": True, "save_file": True, "embed_size": 800, "save_size": 1500},
    },
    "手机随身版": {
        "quality": {
            "chain": ["MP3_320", "OGG_320", "ACC_192", "MP3_128"],
            "on_all_unavailable": "lowest",
            "reject_trial": True,
        },
        "download": {"interval_seconds": 150.0, "random_interval": True},
        "naming": {
            "dir_template": "{歌单名}",
            "file_template": "{歌名} - {歌手}",
        },
        "lyric": {"enabled": True, "qrc": False, "output_format": "line_lrc"},
        "cover": {"enabled": True, "embed": True, "save_file": False, "embed_size": 500},
    },
}
