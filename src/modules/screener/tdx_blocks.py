"""通达信板块归属(东财不可用时的备用源)。

通过行情服务器 TCP 7709 下载三个板块文件,一次拿全所有归属:

- ``block_gn.dat`` 概念板块(约 269 个)
- ``block_fg.dat`` 风格板块(约 161 个,新高/破净/高股息/股权集中都在这里)
- ``block_zs.dat`` 指数板块(约 117 个,沪深300/中证500…)

合计约 1.9 秒、上百万条「板块↔个股」归属,无需鉴权、无分页、无反爬。

协议参考 pytdx 的只读实现(本模块不依赖 pytdx,避免镜像重建),属于公开
行情数据,与东财/腾讯等现有数据源用途一致。

通达信只给归属、不给涨跌幅/成交额,调用方需要自己按快照聚合。
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
import zlib

logger = logging.getLogger(__name__)

# 免费行情服务器:按实测可用性排序,失败自动换下一个
_HOSTS = [
    ("124.71.187.122", 7709),
    ("110.41.147.114", 7709),
    ("116.205.163.254", 7709),
    ("180.153.18.170", 7709),
    ("180.153.18.171", 7709),
    ("202.108.253.130", 7709),
    ("60.191.117.167", 7709),
    ("115.238.56.198", 7709),
    ("218.75.126.9", 7709),
]
_TIMEOUT = 4.0
_CHUNK = 0x7530

# 建立连接的三个固定握手包(公开协议)
_SETUP = (
    bytes.fromhex("0c02189300010300" "03000d0001"),
    bytes.fromhex("0c02189400010300" "03000d0002"),
    bytes.fromhex(
        "0c03189900012000" "2000db0fd5d0c9cc" "d6a4a8af0000008f"
        "c22540130000d500" "c9ccbdf0d7ea0000" "0002"
    ),
)
_META_PKG = bytes.fromhex("0c39186900012a00" "2a00c502")
_GET_PKG = bytes.fromhex("0c37186a00016e00" "6e00b906")

_FILES = {
    "theme": "block_gn.dat",
    "style": "block_fg.dat",
    "index": "block_zs.dat",
}

_MEM: dict = {"ts": 0.0, "groups": None, "host": None}
_TTL_S = 600.0
_LOCK = threading.Lock()

# 只保留 A 股(含北交所),剔除 B 股/基金/债券等
_A_PREFIX = ("00", "30", "60", "68", "83", "87", "43", "92")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        part = sock.recv(size - len(buf))
        if not part:
            raise ConnectionError("通达信服务器断开连接")
        buf.extend(part)
    return bytes(buf)


def _call(sock: socket.socket, pkg: bytes) -> bytes:
    sock.sendall(pkg)
    head = _recv_exact(sock, 0x10)
    _, _, _, zipsize, unzipsize = struct.unpack("<IIIHH", head)
    body = _recv_exact(sock, zipsize)
    if zipsize != unzipsize:
        body = zlib.decompress(body)
    return body


def _open() -> socket.socket:
    last: Exception | None = None
    for host, port in _HOSTS:
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((host, port), timeout=_TIMEOUT)
            sock.settimeout(_TIMEOUT)
            for pkg in _SETUP:
                _call(sock, pkg)
            _MEM["host"] = host
            return sock
        except Exception as exc:  # noqa: BLE001
            last = exc
            if sock is not None:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass
    raise ConnectionError(f"通达信服务器全部不可用: {last}")


def _download(sock: socket.socket, fname: str) -> bytes:
    body = _call(sock, _META_PKG + struct.pack("<40s", fname.encode("utf-8")))
    try:
        (size,) = struct.unpack("<I", body[:4])
    except struct.error as exc:
        raise ValueError(f"{fname} 元信息解析失败") from exc
    if not 0 < size < 32 * 1024 * 1024:
        raise ValueError(f"{fname} 大小异常: {size}")

    out = bytearray()
    start = 0
    while start < size:
        head = _call(
            sock,
            _GET_PKG + struct.pack("<II100s", start, _CHUNK, fname.encode("utf-8")),
        )
        piece = head[4:]
        if not piece:
            break
        out.extend(piece)
        start += _CHUNK
    return bytes(out[:size])


def _parse(data: bytes) -> dict[str, list[str]]:
    """板块文件格式: 384 字节文件头 + 板块数(2B) + [名称(9B)+数量(2B)+类型(2B)+400*7B]。"""
    if len(data) < 386:
        return {}
    pos = 384
    (num,) = struct.unpack("<H", data[pos:pos + 2])
    pos += 2
    out: dict[str, list[str]] = {}
    for _ in range(num):
        if pos + 13 > len(data):
            break
        name = data[pos:pos + 9].decode("gbk", "ignore").rstrip("\x00").strip()
        pos += 9
        count, _type = struct.unpack("<HH", data[pos:pos + 4])
        pos += 4
        codes: list[str] = []
        count = min(count, 400)
        for i in range(count):
            code = data[pos + i * 7:pos + i * 7 + 7].decode("ascii", "ignore").rstrip("\x00")
            if len(code) == 6 and code.startswith(_A_PREFIX):
                codes.append(code)
        pos += 2800
        if name and codes:
            out[name] = codes
    return out


def fetch_groups(*, force: bool = False) -> dict[str, dict[str, list[str]]]:
    """返回 ``{"theme": {板块名: [代码]}, "style": {...}, "index": {...}}``。

    结果缓存 10 分钟;任一文件失败返回空字典,不影响调用方继续降级。
    """
    with _LOCK:
        if (
            not force
            and _MEM["groups"] is not None
            and time.monotonic() - _MEM["ts"] <= _TTL_S
        ):
            return _MEM["groups"]

        groups: dict[str, dict[str, list[str]]] = {}
        sock = None
        try:
            sock = _open()
            for kind, fname in _FILES.items():
                try:
                    groups[kind] = _parse(_download(sock, fname))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[选股] 通达信板块文件失败 {fname}: {exc}")
                    groups[kind] = {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[选股] 通达信板块归属不可用: {exc}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass

        total = sum(len(v) for v in groups.values())
        if total:
            logger.info(
                f"[选股] 通达信板块: 概念 {len(groups.get('theme', {}))} / "
                f"风格 {len(groups.get('style', {}))} / "
                f"指数 {len(groups.get('index', {}))} (host={_MEM.get('host')})"
            )
            _MEM.update(ts=time.monotonic(), groups=groups)
        else:
            # 全失败时缩短缓存,1 分钟后重试,避免一次网络抖动锁死 10 分钟
            _MEM.update(ts=time.monotonic() - (_TTL_S - 60.0), groups={})
        return groups
