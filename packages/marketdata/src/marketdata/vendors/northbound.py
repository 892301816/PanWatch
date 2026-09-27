"""北向资金 vendor:同花顺(ths/hexin)当日分钟累计净买入,市场级(symbols 恒空)。

背景:东财 datacenter/push2 的北向资金接口(kamt)自 2024-08 起断供(返回 NaN/0),
不可用。改走同花顺 hexin 私有接口 `data.hexin.cn/market/hsgtApi/method/dayChart/`,
返回当日分钟级累计净买入序列,`hgt`(沪股通)/`sgt`(深股通),单位均为"亿元"。

**实测结构(2026-09 实抓校准,替换原先的"待校准"猜测)**:响应是**扁平**的平行数组,
没有 data 包裹::

    {"time": ["09:10", ..., "15:00"], "hgt": [0, 0.03, ..., -9.28], "sgt": [364.81, ...]}

``time`` 与 ``hgt`` 均为 262 点(09:10–15:00)且按下标对齐;``sgt`` 实测只有 35 点,
与 time 对不齐。单位为亿元。

已知坑:`sgt`(深股通)数据不可靠——既可能量级异常(远超合理的"亿元"范围),实测还
与 time 长度不一致,无法确定末值对应哪一分钟。故除量级阈值外再加对齐校验,任一不过
即 sgt_net=None,不参与 total_net 计算,也不让它污染 hgt_net。绝不用无参
now()/time()/random 填充缺失的 date/time。
"""
from __future__ import annotations

from marketdata.http import market_get
from marketdata.symbol import Symbol
from marketdata.types import NorthboundItem
from marketdata.vendors.base import NorthboundVendor as _NorthboundVendorBase

_HEXIN_URL = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
_HEXIN_HOST = "data.hexin.cn"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_HEADERS = {
    "Host": _HEXIN_HOST,
    "Referer": "https://data.hexin.cn/",
    "User-Agent": _UA,
}

# sgt(深股通)近期不可靠,可能出现量级异常(远超合理"亿元"净买入范围)的脏值;
# 超过此绝对值阈值一律视为异常丢弃。阈值本身是防御性经验值,非精确业务规则。
_SGT_MAX_ABS = 2000.0


def _to_float(value) -> float | None:
    """宽松转 float;None/无法转换/NaN 一律 None(NaN 用 f != f 判定,不额外 import math)。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _sgt_valid(value) -> float | None:
    """sgt 专用:在 _to_float 基础上再做量级容错(近期不可靠,可能 NaN/异常大)。"""
    f = _to_float(value)
    if f is None:
        return None
    if abs(f) > _SGT_MAX_ABS:
        return None
    return f


def _unwrap_payload(resp) -> dict:
    """剥离外层包裹,还原成含 time/hgt/sgt 的那一层。

    实测(2026-09)hexin 返回的是**扁平**结构:
        {"time": ["09:10", ...], "hgt": [0, 0.03, ...], "sgt": [364.81, ...]}
    没有 data 包裹。此处兼容历史上猜测的 {"data": {...}} / {"data": {"data": {...}}}
    两层写法,拿不到就原样返回(交给下游按键名取值)。
    """
    if not isinstance(resp, dict):
        return {}
    layer = resp.get("data")
    if isinstance(layer, dict):
        inner = layer.get("data")
        return inner if isinstance(inner, dict) else layer
    return resp


def _is_flat_series(series) -> bool:
    """序列是否只有数值、时间要靠同级 time 数组对齐(实测的扁平形态)。

    每点自带时间的写法(``[time, value]`` 或 ``{"time":..,"value":..}``)返回 False ——
    它们自带时间轴,不需要也不能做长度对齐校验。
    """
    if not isinstance(series, (list, tuple)) or not series:
        return False
    return not isinstance(series[0], (list, tuple, dict))


def _aligned_times(series, times) -> list | None:
    """平行数组的时间轴;长度与取值序列不一致时返回 None(无法确定取值对应哪一分钟)。"""
    if not isinstance(times, (list, tuple)):
        return None
    if len(times) != len(series):
        return None
    return list(times)


def _last_point(series, times=None) -> tuple[object, object]:
    """取分钟序列末值(当日最新累计净买入),返回 (时间, 值)。

    实测结构是**平行数组**:``hgt``/``sgt`` 只有数值,时间在同级的 ``time`` 数组里,
    按下标对齐;因此必须把 times 一并传入。同时兼容每点为 ``[time, value]`` 或
    ``{"time":..,"value":..}`` 的写法。取不到返回 (None, None)。
    """
    if not isinstance(series, (list, tuple)) or not series:
        return None, None
    last = series[-1]
    if isinstance(last, (list, tuple)) and len(last) >= 2:
        return last[0], last[1]
    if isinstance(last, dict):
        t = last.get("time") or last.get("t") or last.get("x")
        v = last.get("value") or last.get("v") or last.get("y") or last.get("net")
        return t, v
    aligned = _aligned_times(series, times)
    return (aligned[-1] if aligned else None), last


class HexinNorthboundVendor(_NorthboundVendorBase):
    """北向资金(同花顺 hexin):市场级,fetch 忽略 symbols。取当日分钟序列末值组装 1 条。"""

    name = "ths"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[NorthboundItem]:
        data = market_get(
            _HEXIN_URL,
            host_key=_HEXIN_HOST,
            headers=_HEADERS,
            parse="json",
            retries=2,
            timeout=8,
            log_label="北向资金",
        )
        if not data:
            return []

        payload = _unwrap_payload(data)
        if not payload:
            return []

        times = payload.get("time")
        hgt_series = payload.get("hgt") or []
        sgt_series = payload.get("sgt") or []

        hgt_time, hgt_raw = _last_point(hgt_series, times)
        sgt_time, sgt_raw = _last_point(sgt_series, times)
        hgt_net = _to_float(hgt_raw)
        sgt_net = _sgt_valid(sgt_raw)
        # sgt 除量级存疑外,实测(扁平形态)长度还与 time 对不上(35 vs 262 点),无法确定
        # 末值对应哪一分钟 —— 扁平序列对不齐时整体判为不可信。每点自带时间的写法不受
        # 此限(它自带时间轴)。
        if sgt_net is not None and _is_flat_series(sgt_series):
            if not _aligned_times(sgt_series, times):
                sgt_net = None
        if hgt_net is None and sgt_net is None:
            return []

        total_net = hgt_net + sgt_net if (hgt_net is not None and sgt_net is not None) else None
        date = str(
            (data.get("date") if isinstance(data, dict) else None)
            or payload.get("date")
            or (config or {}).get("date")
            or ""
        )
        time_point = hgt_time if hgt_time is not None else sgt_time
        time_str = str(time_point) if time_point is not None else ""

        return [
            NorthboundItem(
                date=date,
                hgt_net=hgt_net,
                sgt_net=sgt_net,
                total_net=total_net,
                time=time_str,
            )
        ]
