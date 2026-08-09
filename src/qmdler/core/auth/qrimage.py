"""把二维码图片转成终端字符画.

规范里写的是「TUI 用 ``qrcode`` 库渲染终端字符二维码」, 但 ``qrcode`` 只能**编码**,
需要二维码的文本载荷; 而 0.7.2 的 ``QR.data`` 是一张 PNG/JPEG 图片, 库不暴露载荷:

* QQ 走 ``ssl.ptlogin2.qq.com/ptqrshow``, 只拿得到图片和 cookie 里的 ``qrsig``;
* 微信只拿得到 ``uuid`` 和图片;
* APP 扫码拿到的是 base64 图片和 ``qrcodeID``.

所以这里反过来做: 从图片还原出二维码的**模块矩阵**, 再用半块字符画出来. 模块矩阵
就是渲染需要的全部信息, 不需要真正解码出载荷内容.

转换在服务端完成, TUI 直接拿现成的字符画, 保持纯客户端.

**还原结果必须通过两道自检才允许输出** —— 宁可报错让用户走图片/文件/载荷这三条
退路, 也绝不打印一张可能扫不出来的图:

1. **边长合法性**: 模块数必须是 ``21 + 4n`` (QR version 1~40);
2. **定位图案**: 左上、右上、左下三个角必须匹配标准 7×7 finder pattern.

中心 logo 不做特殊处理, 照原样画出 —— QR 自带的纠错会兜住.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

_FULL = "█"
_UPPER = "▀"
_LOWER = "▄"
_BLANK = " "

#: QR version 1~40 的合法模块数.
VALID_SIZES: frozenset[int] = frozenset(21 + 4 * n for n in range(40))

#: 标准 7×7 定位图案.
_FINDER = [
    [1, 1, 1, 1, 1, 1, 1],
    [1, 0, 0, 0, 0, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 0, 0, 0, 0, 1],
    [1, 1, 1, 1, 1, 1, 1],
]

#: 单个定位图案允许的最大错格数. 采样落在模块边界上偶尔会差一两格,
#: 但整块对不上就说明还原本身错了.
_FINDER_TOLERANCE = 2


class QrRenderError(RuntimeError):
    """二维码还原失败, 不能输出字符画."""


def _finder_mismatch(matrix: list[list[bool]], top: int, left: int) -> int:
    """定位图案的错格数."""
    mismatch = 0
    for row in range(7):
        for col in range(7):
            expected = bool(_FINDER[row][col])
            if matrix[top + row][left + col] != expected:
                mismatch += 1
    return mismatch


def validate_matrix(matrix: list[list[bool]]) -> None:
    """两道自检. 不通过就抛 :class:`QrRenderError`.

    Raises:
        QrRenderError: 边长非法, 或定位图案对不上.
    """
    size = len(matrix)
    if size not in VALID_SIZES:
        raise QrRenderError(f"还原出的模块数 {size} 不是合法的二维码边长（应为 21+4n）")

    corners = {"左上": (0, 0), "右上": (0, size - 7), "左下": (size - 7, 0)}
    bad = {
        name: _finder_mismatch(matrix, top, left)
        for name, (top, left) in corners.items()
        if _finder_mismatch(matrix, top, left) > _FINDER_TOLERANCE
    }
    if bad:
        detail = "、".join(f"{name}错{count}格" for name, count in bad.items())
        raise QrRenderError(f"定位图案对不上（{detail}），还原不可靠")


def _to_binary_matrix(data: bytes) -> list[list[bool]] | None:
    """把二维码图片还原成 ``True=黑`` 的模块矩阵."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow 是必需依赖, 缺失只在异常环境
        logger.warning("Pillow 不可用, 无法渲染终端二维码")
        return None

    try:
        image = Image.open(io.BytesIO(data)).convert("L")
    except Exception as exc:
        logger.warning("二维码图片解析失败: %s", exc)
        return None

    width, height = image.size
    if width < 8 or height < 8:
        return None
    pixels = image.load()
    if pixels is None:  # pragma: no cover
        return None

    dark = [[int(pixels[x, y]) < 128 for x in range(width)] for y in range(height)]  # type: ignore[arg-type]

    # 1) 裁掉静区: 取全部黑像素的包围盒.
    rows = [y for y in range(height) if any(dark[y])]
    cols = [x for x in range(width) if any(dark[y][x] for y in range(height))]
    if not rows or not cols:
        return None
    top, bottom = rows[0], rows[-1]
    left, right = cols[0], cols[-1]
    box_w = right - left + 1
    box_h = bottom - top + 1

    # 2) 求单模块像素数: 二维码顶边穿过左上角的定位图案, 其外框恰好 7 个模块宽.
    run = 0
    for x in range(left, right + 1):
        if dark[top][x]:
            run += 1
        else:
            break
    module_px = (run / 7.0) if run >= 7 else 0.0
    if module_px <= 0:
        return None

    modules = round(box_w / module_px)
    if not (11 <= modules <= 201):
        # 版本 1~40 的模块数是 21~177; 超出说明推断不可靠, 放弃.
        return None

    # 3) 采样每个模块的中心点.
    step_x = box_w / modules
    step_y = box_h / modules
    matrix: list[list[bool]] = []
    for row in range(modules):
        y = min(height - 1, int(top + (row + 0.5) * step_y))
        matrix.append([dark[y][min(width - 1, int(left + (col + 0.5) * step_x))] for col in range(modules)])
    return matrix


def render_terminal_qr(data: bytes, *, quiet_zone: int = 2, invert: bool = False) -> str:
    """把二维码图片渲染成半块字符画.

    每个字符横向表示 1 个模块、纵向表示 2 个模块, 所以输出宽度约等于模块数
    (常见 41 列), 高度约为模块数的一半.

    Args:
        data: 二维码图片字节.
        quiet_zone: 四周留白的模块数. 少于 2 会有终端扫不出来.
        invert: 反色. 浅色背景的终端需要打开.

    Returns:
        多行字符串.

    Raises:
        QrRenderError: 图片解析不了, 或没通过两道自检.
    """
    matrix = _to_binary_matrix(data)
    if matrix is None:
        raise QrRenderError("二维码图片无法解析")
    validate_matrix(matrix)

    size = len(matrix)
    padded_size = size + quiet_zone * 2
    padded = [[False] * padded_size for _ in range(padded_size)]
    for y in range(size):
        for x in range(size):
            padded[y + quiet_zone][x + quiet_zone] = matrix[y][x]

    lines: list[str] = []
    for y in range(0, padded_size, 2):
        chars: list[str] = []
        for x in range(padded_size):
            top = padded[y][x]
            bottom = padded[y + 1][x] if y + 1 < padded_size else False
            if invert:
                top, bottom = not top, not bottom
            # 终端默认深色背景: 用「亮块 = 二维码白模块」渲染, 摄像头才认得出.
            if top and bottom:
                chars.append(_BLANK)
            elif top:
                chars.append(_LOWER)
            elif bottom:
                chars.append(_UPPER)
            else:
                chars.append(_FULL)
        lines.append("".join(chars))
    return "\n".join(lines)


def try_render(data: bytes, *, quiet_zone: int = 2, invert: bool = False) -> tuple[str, str]:
    """渲染字符画, 不抛异常.

    Returns:
        ``(字符画, 错误说明)``. 还原失败时字符画为空串, 错误说明非空 ——
        调用方据此提示用户改走图片端点 / 落盘文件 / 载荷文本这三条退路.
    """
    try:
        return render_terminal_qr(data, quiet_zone=quiet_zone, invert=invert), ""
    except QrRenderError as exc:
        logger.warning("终端二维码还原失败: %s", exc)
        return "", str(exc)


def qr_module_count(data: bytes) -> int:
    """返回二维码的模块数 (渲染宽度估算用), 失败返回 0."""
    matrix = _to_binary_matrix(data)
    return len(matrix) if matrix else 0


def art_width(art: str) -> int:
    """字符画的列宽 (终端至少要这么宽才放得下)."""
    lines = art.split("\n")
    return len(lines[0]) if lines and lines[0] else 0
