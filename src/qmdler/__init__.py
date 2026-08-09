"""qmdler — QQ 音乐歌单批量下载器.

一个核心 (``core``) + 一个服务 (``server``) + 两个纯客户端前端 (``web`` / ``tui``).

``core`` 不得 import ``server`` / ``web`` / ``tui`` 中的任何内容; 两个前端都通过
HTTP/WebSocket 连接同一个后端, 不允许各自实现下载逻辑.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
