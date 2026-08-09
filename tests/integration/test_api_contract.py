"""REST 接口契约.

WebUI 和 TUI 都靠这组端点, 形状不能随便改.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from qmdler.server.app import create_app


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """带完整 lifespan 的测试客户端, XDG 目录指向临时路径."""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
    ):
        yield http


async def test_health(client: httpx.AsyncClient) -> None:
    """健康检查是 TUI 探测后端的依据, 必须带 app 标识."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["app"] == "qmdler"
    assert payload["ok"] is True
    assert "logged_in" in payload
    assert "engine_running" in payload


async def test_snapshot_shape(client: httpx.AsyncClient) -> None:
    """WS 连上时先推的那帧快照, 结构与这里一致."""
    payload = (await client.get("/api/snapshot")).json()
    assert set(payload) == {"auth", "engine", "settings"}
    assert "logged_in" in payload["auth"]
    assert "counts" in payload["engine"]
    assert "quality" in payload["settings"]


async def test_config_roundtrip(client: httpx.AsyncClient) -> None:
    """配置能改能存, 并且立即在 GET 里生效."""
    response = await client.patch("/api/config", json={"patch": {"download": {"interval_seconds": 240}}})
    assert response.status_code == 200
    assert response.json()["download"]["interval_seconds"] == 240
    assert (await client.get("/api/config")).json()["download"]["interval_seconds"] == 240


async def test_config_rejects_invalid_values(client: httpx.AsyncClient) -> None:
    """非法配置要被挡住, 不能写进文件."""
    response = await client.patch("/api/config", json={"patch": {"quality": {"chain": ["不存在的档位"]}}})
    assert response.status_code == 400


async def test_quality_catalog_covers_all_plain_types(client: httpx.AsyncClient) -> None:
    """音质表要覆盖全部 17 个普通档位, 且不含加密档位."""
    payload = (await client.get("/api/config/qualities")).json()
    codes = [item["code"] for item in payload["items"]]
    assert len(codes) == 17
    assert "MASTER" in codes
    assert "DTS_X" in codes
    assert "NAC" in codes
    assert not any(code.endswith("_ENC") for code in codes)
    # 加密档位的警告文案必须在
    assert "不解密" in payload["notes"]["encrypted"]
    assert "会员等级不是万能的" in payload["notes"]["vip"]


async def test_template_preview(client: httpx.AsyncClient) -> None:
    """模板预览返回完整路径, 供 UI 实时刷新."""
    response = await client.post(
        "/api/config/templates/preview",
        json={"dir_template": "{歌手}/{专辑}", "file_template": "{曲序}. {歌名}", "root": "/music"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["full"].startswith("/music/")
    assert payload["full"].endswith(".flac")
    assert "//" not in payload["full"].replace("://", "")


async def test_template_preview_reports_unknown_placeholders(client: httpx.AsyncClient) -> None:
    """打错的占位符要能被指出来."""
    payload = (
        await client.post(
            "/api/config/templates/preview",
            json={"file_template": "{歌名}-{打错了}", "root": "/m"},
        )
    ).json()
    assert payload["unknown_file_placeholders"] == ["打错了"]


@pytest.mark.parametrize(
    ("text", "expected_type", "expected_id"),
    [
        ("https://y.qq.com/n/ryqq/playlist/7392570702", "songlist", "7392570702"),
        ("7392570702", "songlist", "7392570702"),
        ("https://y.qq.com/n/ryqq/albumDetail/000MkMni19ClKG", "album", "000MkMni19ClKG"),
        ("https://y.qq.com/n/ryqq/singer/0025NhlN2yWrP4", "singer", "0025NhlN2yWrP4"),
    ],
)
async def test_resolve_share_links(
    client: httpx.AsyncClient,
    text: str,
    expected_type: str,
    expected_id: str,
) -> None:
    """分享链接解析."""
    payload = (await client.post("/api/sources/resolve", json={"text": text})).json()
    assert payload["source_type"] == expected_type
    assert payload["identifier"] == expected_id


async def test_resolve_rejects_garbage(client: httpx.AsyncClient) -> None:
    """解析不出来要明确报错, 不能装作成功."""
    response = await client.post("/api/sources/resolve", json={"text": "这不是链接"})
    assert response.status_code == 400


async def test_sources_require_login(client: httpx.AsyncClient) -> None:
    """未登录时账号相关来源返回 401."""
    assert (await client.get("/api/sources/created")).status_code == 401
    assert (await client.post("/api/sources/fetch", json={"source_type": "fav_song"})).status_code == 401


async def test_task_lifecycle(client: httpx.AsyncClient, tmp_path: Path) -> None:
    """建任务 → 列条目 → 勾选 → 报告 → 删除."""
    song = {
        "songmid": "mid1",
        "songid": 1,
        "title": "歌一",
        "singers": [{"mid": "s1", "name": "歌手"}],
        "album_name": "专辑",
        "album_mid": "a1",
        "media_mid": "mid1",
        "song_type": 0,
        "interval": 200,
        "track_no": 1,
        "disc_no": 1,
        "year": "2020",
        "status": 0,
        "sa": 0,
        "sizes": {"MP3_128": 1000},
        "pay": {},
        "size_try": 0,
        "try_begin_ms": 0,
        "try_end_ms": 0,
        "vs": [],
        "vi": [],
        "vf": [],
    }
    created = await client.post(
        "/api/tasks",
        json={
            "name": "契约测试",
            "source_type": "manual",
            "save_root": str(tmp_path / "out"),
            "items": [song],
        },
    )
    assert created.status_code == 200
    task_id = created.json()["id"]

    items = (await client.get(f"/api/tasks/{task_id}/items")).json()
    assert len(items) == 1
    assert items[0]["requested_quality"] == "MP3_128"
    assert items[0]["selected"] is True
    assert items[0]["status"] == "pending"

    await client.post(f"/api/tasks/{task_id}/selection", json={"item_ids": [items[0]["id"]], "selected": False})
    assert (await client.get(f"/api/tasks/{task_id}/items")).json()[0]["selected"] is False

    await client.post(f"/api/tasks/{task_id}/selection/invert")
    assert (await client.get(f"/api/tasks/{task_id}/items")).json()[0]["selected"] is True

    matched = await client.post(f"/api/tasks/{task_id}/selection/filter", json={"keyword": "歌一"})
    assert matched.json()["matched"] == 1

    report = (await client.get(f"/api/tasks/{task_id}/report")).json()
    assert set(report["counts"]) >= {"success", "failed", "skipped", "unavailable", "trial"}
    assert "试听" in report["summary"]

    csv_response = await client.get(f"/api/tasks/{task_id}/report.csv")
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.headers["content-type"]
    assert "歌一" in csv_response.text

    assert (await client.delete(f"/api/tasks/{task_id}")).status_code == 200
    assert (await client.get(f"/api/tasks/{task_id}")).status_code == 404


async def test_path_check(client: httpx.AsyncClient, tmp_path: Path) -> None:
    """保存目录校验: 存在性、可写性、剩余空间."""
    payload = (await client.post("/api/config/path/check", json={"path": str(tmp_path)})).json()
    assert payload["ok"] is True
    assert payload["writable"] is True
    assert payload["total_bytes"] > 0

    missing = (await client.post("/api/config/path/check", json={"path": str(tmp_path / "nope")})).json()
    assert missing["ok"] is False

    created = (
        await client.post("/api/config/path/check", json={"path": str(tmp_path / "made"), "create": True})
    ).json()
    assert created["ok"] is True


async def test_presets_listed(client: httpx.AsyncClient) -> None:
    """两套内置预设都在."""
    payload = (await client.get("/api/config/presets")).json()
    assert "无损归档" in payload["presets"]
    assert "手机随身版" in payload["presets"]


async def test_preset_activation_changes_settings(client: httpx.AsyncClient) -> None:
    """切换预设会真正改变生效配置."""
    payload = (await client.post("/api/config/presets/activate", json={"name": "手机随身版"})).json()
    assert payload["preset"] == "手机随身版"
    assert payload["quality"]["chain"][0] == "MP3_320"
    assert payload["quality"]["on_all_unavailable"] == "lowest"


async def test_start_unknown_task_404(client: httpx.AsyncClient) -> None:
    """启动不存在的任务返回 404."""
    assert (await client.post("/api/tasks/nope/start")).status_code == 404


async def test_auth_endpoints_when_logged_out(client: httpx.AsyncClient) -> None:
    """未登录时的登录态与 profile 行为."""
    state = (await client.get("/api/auth/state")).json()
    assert state["logged_in"] is False
    assert (await client.get("/api/auth/profile")).status_code == 401


async def test_credential_import_validates(client: httpx.AsyncClient) -> None:
    """凭证导入要校验必填字段."""
    response = await client.post("/api/auth/credential", json={"musicid": 0, "musickey": ""})
    assert response.status_code in (400, 422)


async def test_credential_import_normalizes_str_musicid(client: httpx.AsyncClient) -> None:
    """导入后 str_musicid 必须补齐 —— get_song_urls 取的就是它, 且库里没有兜底."""
    response = await client.post("/api/auth/credential", json={"musicid": 123456, "musickey": "Q_H_L_test"})
    assert response.status_code == 200
    assert response.json()["musicid"] == 123456


async def test_web_shell_served(client: httpx.AsyncClient) -> None:
    """WebUI 静态资源与 PWA 清单可达."""
    index = await client.get("/")
    assert index.status_code == 200
    assert "qmdler" in index.text
    assert 'name="viewport"' in index.text

    manifest = await client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"

    assert (await client.get("/sw.js")).status_code == 200
    # 依赖必须是 vendored 的, 不能引用 CDN
    assert (await client.get("/vendor/vue.global.prod.js")).status_code == 200
    assert "//unpkg.com" not in index.text
    assert "//cdn." not in index.text
