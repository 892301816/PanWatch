"""股票代码格式互转。

PanWatch 内部统一用 6 位数字（``600000``）；扶摇/TickFlow/通达信用带交易所后缀的
``600000.SH``。这里集中处理，避免每个 provider 各写一份。
"""

from __future__ import annotations


def to_std(code: str) -> str:
    """任意写法 → 6 位数字。

    支持 ``600000`` / ``600000.SH`` / ``sh600000`` / ``SZ000001``。
    """
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw.split(".", 1)[0].strip()
    if raw[:2] in ("SH", "SZ", "BJ"):
        return raw[2:].strip()
    return raw


def exchange_of(code: str) -> str:
    """按代码段推断交易所。"""
    c = to_std(code)
    if not c or not c.isdigit():
        return ""
    # 北交所: 43/83/87/88 开头与 920 新代码段;900 开头才是沪市 B 股
    if c.startswith(("4", "8")) or c.startswith("92"):
        return "BJ"
    if c.startswith(("6", "9")):
        return "SH"
    if c.startswith(("0", "2", "3")):
        return "SZ"
    return ""


def to_exchange_code(code: str) -> str:
    """6 位数字 → ``600000.SH`` 形式（扶摇/TickFlow/通达信通用）。"""
    c = to_std(code)
    ex = exchange_of(c)
    if not c:
        return ""
    return f"{c}.{ex}" if ex else c
