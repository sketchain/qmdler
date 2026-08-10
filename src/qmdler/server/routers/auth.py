# SPDX-License-Identifier: GPL-3.0-or-later
"""登录相关端点. 五种方式并列."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from ...core.auth.sessions import qrcode_data_uri
from ..deps import Ctx

router = APIRouter(prefix="/auth", tags=["auth"])


class QrStartRequest(BaseModel):
    """开始扫码登录."""

    login_type: Literal["qq", "wx", "mobile"] = "qq"


class PhoneStartRequest(BaseModel):
    """发送短信验证码."""

    phone: int | str
    country_code: int = 86


class PhoneCodeRequest(BaseModel):
    """提交短信验证码."""

    session_id: str
    code: str = Field(min_length=1, max_length=16)


class CredentialImportRequest(BaseModel):
    """直接导入 Credential.

    可以只填 musicid + musickey; 但那样凭证里没有 ``encrypt_uin``,
    「我喜欢」/「收藏的歌单」/ 账号信息会不可用.
    """

    musicid: int
    musickey: str
    refresh_key: str = ""
    refresh_token: str = ""
    access_token: str = ""
    openid: str = ""
    unionid: str = ""
    encrypt_uin: str = ""
    login_type: int | None = None
    musickey_create_time: int = 0
    key_expires_in: int = 0


class CredentialFileRequest(BaseModel):
    """从 JSON 文件导入 Credential."""

    path: str


@router.get("/state")
async def state(context: Ctx) -> dict[str, Any]:
    """当前登录态."""
    return context.auth.state()


@router.get("/profile")
async def profile(context: Ctx, refresh: bool = False) -> dict[str, Any]:
    """账号昵称与会员状态.

    会员等级决定能拿到哪些音质, 但**不是万能的**: 单曲付费、该音质文件本就不存在、
    地区版权限制、设备受限都与会员等级无关.
    """
    if not context.auth.logged_in:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "尚未登录")
    return await context.auth.load_profile(refresh=refresh)


@router.post("/qrcode")
async def start_qrcode(payload: QrStartRequest, context: Ctx) -> dict[str, Any]:
    """开始扫码登录, 返回会话与二维码."""
    try:
        state_obj = await context.login.start_qrcode(payload.login_type)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    data = state_obj.as_dict()
    data["image"] = qrcode_data_uri(state_obj.png) if state_obj.png else ""
    return data


@router.get("/qrcode/payload")
async def qrcode_payload(context: Ctx, session_id: str = "") -> dict[str, Any]:
    """解出二维码载荷 —— **仅诊断用途**.

    依赖只在 dev 里的 ``zxing-cpp``; 没装就返回安装提示, 不影响正常登录。
    """
    payload, error = context.login.decode_payload(session_id)
    return {"payload": payload, "error": error}


@router.get("/qrcode/{session_id}")
async def qrcode_state(session_id: str, context: Ctx) -> dict[str, Any]:
    """轮询扫码状态 (WS 断线时的退路)."""
    state_obj = context.login.get_qrcode(session_id)
    if state_obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "扫码会话不存在")
    return state_obj.as_dict()


@router.get("/qrcode/{session_id}/image")
async def qrcode_image(session_id: str, context: Ctx) -> Response:
    """二维码图片. 手机上要足够大且能长按保存, 所以单独给一张图."""
    data, mimetype = context.login.qrcode_image(session_id)
    if not data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "二维码不存在")
    return Response(content=data, media_type=mimetype or "image/png")


@router.post("/qrcode/{session_id}/refresh")
async def refresh_qrcode(session_id: str, context: Ctx) -> dict[str, Any]:
    """二维码过期后一键刷新."""
    state_obj = await context.login.refresh_qrcode(session_id)
    data = state_obj.as_dict()
    data["image"] = qrcode_data_uri(state_obj.png) if state_obj.png else ""
    return data


@router.delete("/qrcode/{session_id}")
async def cancel_qrcode(session_id: str, context: Ctx) -> dict[str, bool]:
    """取消扫码会话."""
    await context.login.cancel_qrcode(session_id)
    return {"ok": True}


@router.post("/phone/code")
async def send_phone_code(payload: PhoneStartRequest, context: Ctx) -> dict[str, Any]:
    """发送短信验证码.

    非成功分支: ``captcha`` 需要人机验证 (返回 ``captcha_url``), ``frequency``
    是频率限制.
    """
    state_obj = await context.login.start_phone(payload.phone, payload.country_code)
    return state_obj.as_dict()


@router.post("/phone/verify")
async def verify_phone_code(payload: PhoneCodeRequest, context: Ctx) -> dict[str, Any]:
    """提交短信验证码."""
    try:
        state_obj = await context.login.submit_phone_code(payload.session_id, payload.code)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return state_obj.as_dict()


@router.post("/credential")
async def import_credential(payload: CredentialImportRequest, context: Ctx) -> dict[str, Any]:
    """直接导入 Credential."""
    data = payload.model_dump(exclude_none=True)
    try:
        await context.login.import_credential(data)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return context.auth.state()


@router.post("/credential/file")
async def import_credential_file(payload: CredentialFileRequest, context: Ctx) -> dict[str, Any]:
    """从 JSON 文件导入 Credential."""
    try:
        await context.login.import_credential_file(payload.path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return context.auth.state()


@router.post("/refresh")
async def refresh(context: Ctx) -> dict[str, Any]:
    """手动触发一次凭证刷新."""
    try:
        await context.auth.refresh(force=True)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return context.auth.state()


@router.post("/logout")
async def logout(context: Ctx) -> dict[str, Any]:
    """登出."""
    await context.auth.logout()
    return context.auth.state()
