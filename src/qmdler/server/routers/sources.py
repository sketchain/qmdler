# SPDX-License-Identifier: GPL-3.0-or-later
"""歌单来源端点."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from qqmusic_api.core.exceptions import BaseApiException

from ...core.auth.manager import NotLoggedInError
from ...core.quality import annotate
from ...core.sources.resolver import parse_mid_list, resolve
from ...core.sources.service import DEFAULT_LIMIT, FetchCancelledError, SourceResult
from ..deps import Ctx

router = APIRouter(prefix="/sources", tags=["sources"])


class FetchRequest(BaseModel):
    """拉取一个来源的曲目."""

    source_type: Literal["created", "fav_songlist", "fav_song", "songlist", "album", "singer", "search", "manual"]
    identifier: str = ""
    #: 分页必须翻完, 但必须有上限, 别无限拉.
    #: 默认值与上限是**同一个常量**, 不在这里另写一个数.
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=DEFAULT_LIMIT)
    name: str = ""


class ResolveRequest(BaseModel):
    """解析分享链接."""

    text: str


def _payload(context: Ctx, result: SourceResult) -> dict[str, Any]:
    chain = context.settings.quality.chain
    on_all = context.settings.quality.on_all_unavailable
    return {
        **result.as_dict(),
        # 列表阶段就预先标注: 可用的最高音质 / 是否付费 / 是否无版权.
        "items": [
            {
                **entry.as_dict(),
                "annotation": annotate(entry, chain, on_all_unavailable=on_all),
            }
            for entry in result.entries
        ],
    }


@router.post("/resolve")
async def resolve_link(payload: ResolveRequest) -> dict[str, Any]:
    """从分享链接 / 裸 ID 里解析出歌单、专辑、歌手或单曲."""
    resolved = resolve(payload.text)
    if resolved is None:
        mids = parse_mid_list(payload.text)
        if mids:
            return {"source_type": "manual", "identifier": ",".join(mids), "mids": mids}
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法从输入中解析出歌单 / 专辑 / 歌手 / 单曲")
    return {
        "source_type": resolved.source_type.value,
        "identifier": resolved.identifier,
        "raw": resolved.raw,
    }


@router.get("/created")
async def created_songlists(context: Ctx) -> list[dict[str, Any]]:
    """账号自建歌单列表."""
    return await _guard(context.sources.created_songlists())


@router.get("/fav-songlists")
async def fav_songlists(context: Ctx, limit: int = 200) -> list[dict[str, Any]]:
    """账号收藏的他人歌单."""
    return await _guard(context.sources.fav_songlists(limit=limit))


@router.post("/fetch")
async def fetch(payload: FetchRequest, context: Ctx) -> dict[str, Any]:
    """按来源拉取曲目列表."""
    service = context.sources
    kind = payload.source_type

    if kind == "fav_song":
        result = await _guard(service.from_fav_song(limit=payload.limit))
    elif kind == "songlist":
        if not payload.identifier.isdigit():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "歌单 ID 必须是数字")
        result = await _guard(service.from_songlist(int(payload.identifier), limit=payload.limit))
    elif kind == "album":
        result = await _guard(service.from_album(payload.identifier, limit=payload.limit))
    elif kind == "singer":
        result = await _guard(service.from_singer(payload.identifier, limit=payload.limit))
    elif kind == "search":
        result = await _guard(service.from_search(payload.identifier, limit=min(payload.limit, 500)))
    elif kind == "manual":
        mids = parse_mid_list(payload.identifier)
        if not mids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有解析出任何有效的 mid")
        result = await _guard(service.from_mids(mids, name=payload.name or "手动列表"))
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"该来源需要先选择具体歌单：{kind}")

    return _payload(context, result)


@router.post("/cancel")
async def cancel_fetch(context: Ctx) -> dict[str, Any]:
    """取消正在进行的拉取.

    大歌单要翻几十页, 耗时不短, 必须能中断. 取消在下一个翻页检查点生效,
    并且**不返回半截列表** —— 半截列表看起来像完整的, 比拿不到更危险.
    """
    context.sources.cancel_fetch()
    return {"cancelled": True}


async def _guard(awaitable: Any) -> Any:
    """把库异常翻成 HTTP 错误."""
    try:
        return await awaitable
    except NotLoggedInError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except FetchCancelledError as exc:
        # 499 是 nginx 的「客户端主动断开」, 这里借来表达「用户取消」——
        # 不是错误, 前端据此静默清空而不是弹错误框.
        raise HTTPException(499, str(exc)) from exc
    except BaseApiException as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
