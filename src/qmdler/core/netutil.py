"""网络地址工具.

打印二维码图片地址时要给出**手机真能打开**的 URL: 绑在 ``0.0.0.0`` 或
``127.0.0.1`` 上时, 回环地址对手机毫无用处, 所以把本机的局域网 IPv4 也一并列出.
"""

from __future__ import annotations

import ipaddress
import socket

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"})


def local_addresses() -> list[str]:
    """本机可用于局域网访问的 IPv4 地址."""
    found: list[str] = []

    # 连一个外部地址不会真的发包, 但能让内核选出默认出口网卡的地址.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(("223.5.5.5", 80))
            found.append(probe.getsockname()[0])
        except OSError:
            pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    except OSError:
        pass

    result: list[str] = []
    for address in found:
        try:
            parsed = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
            continue
        if address not in result:
            result.append(address)
    return result


def public_bases(host: str, port: int, scheme: str = "http") -> list[str]:
    """给出这个监听地址对外可访问的 base 列表, 首选排在前面.

    绑在 ``0.0.0.0`` 上时回环排后面 —— 手机打不开回环.
    """
    bases: list[str] = []
    if host not in LOOPBACK_HOSTS:
        bases.append(f"{scheme}://{host}:{port}")
    else:
        bases.extend(f"{scheme}://{address}:{port}" for address in local_addresses())
        bases.append(f"{scheme}://127.0.0.1:{port}")

    deduped: list[str] = []
    for base in bases:
        if base not in deduped:
            deduped.append(base)
    return deduped
