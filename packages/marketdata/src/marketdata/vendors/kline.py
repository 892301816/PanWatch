"""K 线 vendors:腾讯(全市场)/ Stooq(US)/ 东财(CN/HK)/ Yahoo(US/HK)。移植自 PanWatch kline_collector 抓取核。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from marketdata.http import market_get
from marketdata.symbol import Market, Symbol
from marketdata.types import Bar, IntradayPoint
from marketdata.vendors.base import KlineVendor

logger = logging.getLogger(__name__)

_TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_STOOQ_URL = "https://stooq.com/q/d/l/"
_YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"


def _days(config: dict, default: int = 60) -> int:
    try:
        return int(config.get("days") or default)
    except Exception:
        return default


# ── 周期路由 ──────────────────────────────────────────────────────────
#
# 日/周/月是各 vendor 都有的口径(腾讯走 fqkline,其余各有实现);分钟与分时
# 目前只有腾讯提供,其余 vendor 见到这些 period 必须返回空 —— 否则 Engine 的
# 失败转移会把"请求分钟线、拿到日线"这种静默错误数据当成成功。
MINUTE_PERIODS: frozenset[str] = frozenset({"m1", "m5", "m15", "m30", "m60"})
INTRADAY_PERIOD = "intraday"


def requested_period(config: dict | None) -> str:
    """从 vendor config 取请求周期,缺省 day。"""
    return str((config or {}).get("period") or "day").strip().lower()


def _is_unsupported_here(config: dict | None) -> bool:
    """非腾讯 vendor 用:请求的是分钟/分时就直接返回空。"""
    return requested_period(config) in MINUTE_PERIODS or requested_period(config) == INTRADAY_PERIOD


# 腾讯 fqkline 对 count 有上限:实测 ≤800 正常返(800→801根),1000-2000 退化到 ~641,
# ≥3000 直接返空(0根)。上层 want 常放大到 3000(为长历史/回测),若原样透传腾讯会返 0
# → 每个标的都白白落到东财补全 → 东财一挂就没数据。故把请求 count 截到 800(取回最多)。
_TENCENT_MAX_COUNT = 800


def fetch_tencent_kline_raw(tsym: str, days: int) -> list[Bar]:
    """按**原始腾讯符号**取日K(不经 Symbol 转换)。

    供指数等显式符号场景复用(sh000001/hkHSI/usDJI…;指数与个股的符号规则不同,
    必须显式传入)。个股路径请走 TencentKlineVendor。
    """
    days = min(max(int(days or 1), 1), _TENCENT_MAX_COUNT)
    text = market_get(
        _TENCENT_URL, host_key="web.ifzq.gtimg.cn", min_interval_s=0.15,
        params={"param": f"{tsym},day,,,{days},qfq", "_var": "kline_dayqfq"},
        timeout=10, retries=2, parse="text", log_label="腾讯K线", symbol=tsym,
    )
    if not text or "=" not in text:
        return []
    js = text.split("=", 1)[1].strip().rstrip(";")
    try:
        data = json.loads(js)
    except Exception:
        return []
    raw = data.get("data", {}) if isinstance(data, dict) else {}
    day = []
    if isinstance(raw, dict):
        sd = raw.get(tsym, {})
        if isinstance(sd, dict):
            day = sd.get("day") or sd.get("qfqday") or []
    elif isinstance(raw, list):
        day = raw
    out: list[Bar] = []
    for it in day or []:
        if len(it) >= 5:
            try:
                out.append(Bar(date=it[0], open=float(it[1]), close=float(it[2]),
                               high=float(it[3]), low=float(it[4]),
                               volume=float(it[5]) if len(it) > 5 else 0.0))
            except Exception:
                continue
    return out


# 腾讯美股日K必须带交易所后缀(usTSLA.OQ=纳斯达克 / usBABA.N=纽交所);裸 us{CODE}
# 只回"首日+最新"两根退化数据,错后缀只回 1 根。后缀无法从代码推断 → 依次试
# .OQ/.N/裸,根数达标即命中并进程内记忆(下次直达,不再多请求)。
_US_SUFFIX_CACHE: dict[str, str] = {}


def _fetch_tencent_us_kline(code: str, days: int) -> list[Bar]:
    want = min(max(int(days or 1), 1), _TENCENT_MAX_COUNT)
    ok_threshold = min(want, 5)  # 正常历史远多于 5 根;退化响应只有 1-2 根
    cached = _US_SUFFIX_CACHE.get(code)
    suffixes = ([cached] if cached is not None else []) + [
        s for s in (".OQ", ".N", "") if s != cached
    ]
    best: list[Bar] = []
    for suf in suffixes:
        bars = fetch_tencent_kline_raw(f"us{code}{suf}", want)
        if len(bars) >= ok_threshold:
            _US_SUFFIX_CACHE[code] = suf
            return bars
        if len(bars) > len(best):
            best = bars
    return best


_TENCENT_MKLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_TENCENT_MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"

# mkline 的 count 上限:实测 640 正常返,1000/2000 都退化到 320。
_TENCENT_MAX_MINUTE_COUNT = 640


def _normalize_minute_time(raw) -> str:
    """'202609161055' → '2026-09-16 10:55'。格式不符则原样返回(不丢数据)。"""
    s = str(raw or "").strip()
    if len(s) == 12 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
    return s


def fetch_tencent_minute_kline(tsym: str, period: str, count: int) -> list[Bar]:
    """腾讯分钟K线(m1/m5/m15/m30/m60)。

    **不带复权** —— 该端点没有 qfq 参数,与日线的前复权口径不同。分钟级看的是
    当日/近几日的相对形态,不复权的影响可忽略,但跨越除权日做对比时要注意。

    返回的 Bar.date 归一化为 'YYYY-MM-DD HH:MM',方便前端直接显示。
    """
    period = str(period or "m5").strip().lower()
    if period not in MINUTE_PERIODS:
        return []
    want = min(max(int(count or 1), 1), _TENCENT_MAX_MINUTE_COUNT)

    data = market_get(
        _TENCENT_MKLINE_URL, host_key="ifzq.gtimg.cn", min_interval_s=0.15,
        params={"param": f"{tsym},{period},,{want}"},
        timeout=10, retries=2, parse="json", log_label="腾讯分钟K线", symbol=tsym,
    )
    node = ((data or {}).get("data") or {}).get(tsym) or {}
    rows = node.get(period) or []

    out: list[Bar] = []
    for it in rows:
        # 每项: [时间, 开, 收, 高, 低, 量(手), {}, 换手率?]
        if not isinstance(it, (list, tuple)) or len(it) < 6:
            continue
        try:
            out.append(
                Bar(
                    date=_normalize_minute_time(it[0]),
                    open=float(it[1]),
                    close=float(it[2]),
                    high=float(it[3]),
                    low=float(it[4]),
                    volume=float(it[5] or 0.0),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def fetch_tencent_intraday(
    tsym: str, config: dict | None = None
) -> tuple[list[IntradayPoint], float | None]:
    """腾讯当日分时。返回 ``(分时点, 昨收)``;昨收取不到时为 None。

    每行形如 ``'0930 1250.01 183 22875182.71'`` = 时间/价/**累计**量(手)/**累计**额(元)。
    昨收(基准线)从响应的 ``qt`` 里顺带取,省一次行情请求。
    """
    # 该端点返回纯 JSON(不像 fqkline 那样套 JSONP),直接按 json 解析
    payload = market_get(
        _TENCENT_MINUTE_URL, host_key="web.ifzq.gtimg.cn", min_interval_s=0.15,
        params={"code": tsym}, timeout=10, retries=2, parse="json",
        log_label="腾讯分时", symbol=tsym,
    )
    if not isinstance(payload, dict):
        return [], None

    node = (payload.get("data") or {}).get(tsym) or {}
    rows = ((node.get("data") or {}).get("data")) or []

    out: list[IntradayPoint] = []
    for row in rows:
        parts = str(row).split()
        if len(parts) < 4:
            continue
        try:
            out.append(
                IntradayPoint(
                    time=parts[0],
                    price=float(parts[1]),
                    volume=float(parts[2] or 0.0),
                    amount=float(parts[3] or 0.0),
                )
            )
        except (TypeError, ValueError):
            continue

    prev_close: float | None = None
    qt = node.get("qt") or {}
    arr = qt.get(tsym) if isinstance(qt, dict) else None
    if isinstance(arr, list) and len(arr) > 4:
        try:
            prev_close = float(arr[4])
        except (TypeError, ValueError):
            prev_close = None
    return out, prev_close


class TencentKlineVendor(KlineVendor):
    name = "tencent"
    supports_markets = {"CN", "HK", "US"}

    _MAX_COUNT = _TENCENT_MAX_COUNT  # 兼容旧引用(测试/外部按类属性取)

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        sym = symbols[0]
        period = requested_period(config)
        if period in MINUTE_PERIODS:
            return fetch_tencent_minute_kline(sym.to_tencent(), period, _days(config))
        if sym.market == Market.US:
            return _fetch_tencent_us_kline(sym.code, _days(config))
        return fetch_tencent_kline_raw(sym.to_tencent(), _days(config))

    def fetch_intraday(
        self, sym: Symbol, config: dict
    ) -> tuple[list[IntradayPoint], float | None]:
        """当日分时。不经 Engine —— 返回的不是 Bar,混进同一条 Response 会让
        "kline 引擎返回 list[Bar]" 这个契约失真。"""
        return fetch_tencent_intraday(sym.to_tencent(), config)


class StooqKlineVendor(KlineVendor):
    name = "stooq"
    supports_markets = {"US"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        # 分钟/分时只有腾讯提供;这里返回空而不是退回日线 —— 否则 Engine 的
        # 失败转移会把"请求分钟线、拿到日线"当成成功,静默给出错误口径的数据。
        if _is_unsupported_here(config):
            return []
        sym = symbols[0].code.strip().lower()
        if not sym:
            return []
        text = market_get(
            _STOOQ_URL, host_key="stooq.com", params={"s": f"{sym}.us", "i": "d"},
            headers={"User-Agent": "PanWatch/1.0 (+https://github.com/)"},
            timeout=12, retries=2, parse="text", log_label="Stooq K线", symbol=sym,
        )
        if not text:
            return []
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) <= 1:
            return []
        out: list[Bar] = []
        for ln in lines[1:]:
            p = ln.split(",")
            if len(p) < 6 or not p[0] or p[0] == "Date":
                continue
            try:
                out.append(Bar(date=p[0], open=float(p[1]), close=float(p[4]),
                               high=float(p[2]), low=float(p[3]),
                               volume=float(p[5]) if p[5] else 0.0))
            except Exception:
                continue
        return out


def _em_secid(sym: Symbol) -> str:
    if sym.market == Market.HK:
        return f"116.{sym.code}"
    if sym.market == Market.US:
        return f"105.{sym.code}"
    from marketdata.symbol import _cn_exchange
    return f"{'1' if _cn_exchange(sym.code) == 'sh' else '0'}.{sym.code}"


def fetch_eastmoney_kline(secid: str, days: int) -> list[Bar]:
    """按显式 secid 取东财日K,不经个股 secid 推导规则(_em_secid)。

    供指数等显式符号场景复用(指数与个股 secid 前缀规则不同,必须显式映射)。
    """
    payload = market_get(
        _EASTMONEY_URL, host_key="push2his.eastmoney.com", min_interval_s=0.2,
        params={"secid": secid, "klt": "101", "fqt": "1",
                "lmt": str(min(max(int(days or 1), 1200), 20000)), "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56",
                "ut": "fa5fd1943c7b386f172d6893dbfba10b"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        timeout=12, retries=1, parse="json", log_label="东财K线", symbol=secid,
    )
    raw = (payload or {}).get("data", {}).get("klines", []) if isinstance(payload, dict) else []
    out: list[Bar] = []
    for row in raw or []:
        p = str(row).split(",")
        if len(p) < 6:
            continue
        try:
            out.append(Bar(date=p[0], open=float(p[1]), close=float(p[2]),
                           high=float(p[3]), low=float(p[4]), volume=float(p[5])))
        except Exception:
            continue
    return out


class EastmoneyKlineVendor(KlineVendor):
    name = "eastmoney"
    supports_markets = {"CN", "HK"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        # 分钟/分时只有腾讯提供。这里返回空而不是退回日线 —— 否则 Engine 的失败
        # 转移会把"请求分钟线、拿到日线"当成成功,静默给出错误口径的数据。
        if _is_unsupported_here(config):
            return []
        sym = symbols[0]
        if sym.market not in (Market.CN, Market.HK):
            return []
        days = _days(config)
        return fetch_eastmoney_kline(_em_secid(sym), days)


def _yahoo_range(days: int) -> str:
    """days → Yahoo chart v8 的 range 枚举(不用 period1/period2,避免依赖当前时间)。"""
    if days <= 5:
        return "5d"
    if days <= 22:
        return "1mo"
    if days <= 66:
        return "3mo"
    if days <= 130:
        return "6mo"
    if days <= 260:
        return "1y"
    if days <= 520:
        return "2y"
    if days <= 1300:
        return "5y"
    return "max"


class YahooKlineVendor(KlineVendor):
    """Yahoo chart v8 日K,零 crumb / 零 cookie(crumb 只有 quoteSummary 基本面才需要)。"""

    name = "yahoo"
    supports_markets = {"US", "HK"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        # 分钟/分时只有腾讯提供。这里返回空而不是退回日线 —— 否则 Engine 的失败
        # 转移会把"请求分钟线、拿到日线"当成成功,静默给出错误口径的数据。
        if _is_unsupported_here(config):
            return []
        sym = symbols[0]
        if sym.market not in (Market.US, Market.HK):
            return []
        days = _days(config)
        ysym = sym.to_yfinance()
        proxy = config.get("proxy")
        payload = market_get(
            _YAHOO_CHART_URL.format(sym=ysym), host_key="query2.finance.yahoo.com",
            params={"interval": "1d", "range": _yahoo_range(days)},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=10, retries=2, parse="json", proxy=proxy,
            log_label="Yahoo K线", symbol=ysym,
        )
        if not isinstance(payload, dict):
            return []
        try:
            result = ((payload.get("chart") or {}).get("result")) or []
            if not result:
                return []
            r0 = result[0] or {}
            timestamps = r0.get("timestamp") or []
            indicators = r0.get("indicators") or {}
            quote = (indicators.get("quote") or [{}])[0] or {}
            adjcloses = (indicators.get("adjclose") or [{}])[0].get("adjclose") if indicators.get("adjclose") else None
        except Exception:
            return []
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        out: list[Bar] = []
        for i, ts in enumerate(timestamps or []):
            try:
                o = opens[i] if i < len(opens) else None
                h = highs[i] if i < len(highs) else None
                low = lows[i] if i < len(lows) else None
                c = closes[i] if i < len(closes) else None
                if o is None or h is None or low is None or c is None:
                    continue
                if adjcloses is not None and i < len(adjcloses) and adjcloses[i] is not None:
                    c = adjcloses[i]
                v = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
                date = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
                out.append(Bar(date=date, open=float(o), close=float(c),
                               high=float(h), low=float(low), volume=float(v)))
            except Exception:
                continue
        if days > 0 and len(out) > days:
            out = out[-days:]
        return out
