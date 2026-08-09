"""二维码载荷解码 —— **仅诊断模式使用**.

``zxing-cpp`` 只在 dev 依赖里, 运行时代码不得依赖它: 正常登录流程用不着载荷,
只有在人要「把二维码转成自己方便扫的形式」时才需要。

因此这里全程惰性导入, 缺库就返回一句说明, 绝不让 ImportError 冒到调用方。
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

MISSING_HINT = "未安装 zxing-cpp（仅诊断用途的可选依赖）：pip install 'qmdler[dev]' 或 pip install zxing-cpp"


def decode_payload(data: bytes) -> tuple[str, str]:
    """解出二维码里的文本载荷.

    Returns:
        ``(载荷, 错误说明)``. 成功时错误说明为空串.
    """
    try:
        import zxingcpp
        from PIL import Image
    except ImportError:
        return "", MISSING_HINT

    try:
        image = Image.open(io.BytesIO(data))
        results = zxingcpp.read_barcodes(image)
    except Exception as exc:
        logger.debug("二维码载荷解码失败: %s", exc)
        return "", f"解码失败: {exc}"

    if not results:
        return "", "图片中没有识别到二维码"
    return results[0].text, ""


def available() -> bool:
    """诊断用的解码器是否可用."""
    try:
        import zxingcpp  # noqa: F401
    except ImportError:
        return False
    return True
