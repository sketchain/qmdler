"""终端二维码还原与两道自检.

素材是从上游真实接口取回的登录二维码 (见 ``tests/data/qr/README.md``) ——
真图才有非整数的每模块像素数、JPEG 噪点和中心 logo 这些真实形态.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from qmdler.core.auth.qrimage import (
    VALID_SIZES,
    QrRenderError,
    _to_binary_matrix,
    art_width,
    qr_module_count,
    render_terminal_qr,
    try_render,
    validate_matrix,
)

QR_DIR = Path(__file__).resolve().parents[1] / "data" / "qr"
QQ = QR_DIR / "qq-login.png"
WX = QR_DIR / "wx-login.jpg"


def read(path: Path) -> bytes:
    """读素材."""
    return path.read_bytes()


# --------------------------------------------------------------------------- #
# 还原
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("path", "modules"), [(QQ, 33), (WX, 41)])
def test_recovers_module_grid_from_real_images(path: Path, modules: int) -> None:
    """真实二维码能还原出正确的模块数.

    QQ 那张是 111px / 33 模块 = 3.36 像素每模块, 不是整数 —— 按包围盒 + 中心
    采样才扛得住.
    """
    assert qr_module_count(read(path)) == modules


@pytest.mark.parametrize("path", [QQ, WX])
def test_module_count_is_a_legal_qr_size(path: Path) -> None:
    """边长必须是 21+4n."""
    assert qr_module_count(read(path)) in VALID_SIZES


def test_valid_sizes_covers_version_1_to_40() -> None:
    """version 1~40 → 21~177."""
    assert min(VALID_SIZES) == 21
    assert max(VALID_SIZES) == 177
    assert len(VALID_SIZES) == 40


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [QQ, WX])
def test_real_images_pass_both_self_checks(path: Path) -> None:
    """真图应当通过边长与定位图案两道自检."""
    matrix = _to_binary_matrix(read(path))
    assert matrix is not None
    validate_matrix(matrix)  # 不抛就算过


def test_illegal_edge_length_rejected() -> None:
    """边长不是 21+4n 直接拒绝."""
    matrix = [[False] * 30 for _ in range(30)]
    with pytest.raises(QrRenderError, match="21\\+4n"):
        validate_matrix(matrix)


def test_missing_finder_patterns_rejected() -> None:
    """边长合法但定位图案对不上, 同样拒绝 —— 这正是采样错位的典型表现."""
    matrix = [[False] * 25 for _ in range(25)]
    with pytest.raises(QrRenderError, match="定位图案"):
        validate_matrix(matrix)


def test_corrupted_image_raises_not_silently_renders() -> None:
    """图片损坏时必须报错, 绝不输出一张可能扫不出来的图."""
    with pytest.raises(QrRenderError):
        render_terminal_qr(b"this is not an image")


def test_try_render_reports_error_instead_of_raising() -> None:
    """``try_render`` 把失败转成错误说明, 供调用方退回其它出口."""
    art, error = try_render(b"not an image")
    assert art == ""
    assert error


@pytest.mark.parametrize("path", [QQ, WX])
def test_try_render_succeeds_on_real_images(path: Path) -> None:
    """真图不该报错."""
    art, error = try_render(read(path))
    assert error == ""
    assert art


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("path", "modules"), [(QQ, 33), (WX, 41)])
def test_art_dimensions(path: Path, modules: int) -> None:
    """半块字符: 宽 = 模块数 + 静区, 高 = 其一半."""
    art = render_terminal_qr(read(path), quiet_zone=2)
    lines = art.split("\n")
    expected_width = modules + 4
    assert art_width(art) == expected_width
    assert all(len(line) == expected_width for line in lines)
    assert len(lines) == (expected_width + 1) // 2


def test_quiet_zone_is_all_light() -> None:
    """静区必须是「亮块」—— 深色终端里那才是二维码的白边, 少了扫不出来."""
    art = render_terminal_qr(read(QQ), quiet_zone=2)
    lines = art.split("\n")
    assert set(lines[0]) == {"█"}, "顶部静区应当整行亮块"
    assert all(line.startswith("██") and line.endswith("██") for line in lines)


# --------------------------------------------------------------------------- #
# 端到端: 字符画能不能真被扫出来
# --------------------------------------------------------------------------- #


def _art_to_image(art: str, scale: int = 6):
    """把半块字符画还原成位图, 好让解码器验证它."""
    from PIL import Image

    lines = art.split("\n")
    width, height = len(lines[0]), len(lines) * 2
    image = Image.new("1", (width * scale, height * scale), 1)
    pixels = image.load()
    for row, line in enumerate(lines):
        for col, char in enumerate(line):
            top_light = char in ("█", "▀")
            bottom_light = char in ("█", "▄")
            for offset, light in ((0, top_light), (1, bottom_light)):
                for dy in range(scale):
                    for dx in range(scale):
                        pixels[col * scale + dx, (row * 2 + offset) * scale + dy] = 1 if light else 0
    return image


@pytest.mark.parametrize("path", [QQ, WX])
def test_rendered_art_decodes_to_the_same_payload(path: Path) -> None:
    """把字符画还原成图片再解码, 载荷必须与原图一致.

    这是「这张字符画真能扫」的唯一硬证据 —— 光看边长和定位图案不够.
    解码器 ``zxing-cpp`` 只在 dev 依赖里, 缺了就跳过.
    """
    zxingcpp = pytest.importorskip("zxingcpp", reason="zxing-cpp 只在 dev 依赖里")
    from PIL import Image

    data = read(path)
    original = zxingcpp.read_barcodes(Image.open(io.BytesIO(data)))
    assert original, "素材本身应当是一张可解码的二维码"

    rendered = zxingcpp.read_barcodes(_art_to_image(render_terminal_qr(data)))
    assert rendered, "字符画解不出来, 说明渲染坏了"
    assert rendered[0].text == original[0].text
