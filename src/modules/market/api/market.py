"""市场指数 API - 公共数据，无需认证"""
import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter

from src.platform.marketdata.collectors.kline_collector import get_index_klines
from src.platform.marketdata.models import MarketCode

logger = logging.getLogger(__name__)
router = APIRouter()


def get_market_data():
    """惰性 import,避免包未装/循环 import 影响本模块加载。"""
    from src.platform.marketdata.marketdata_client import get_market_data as _g

    return _g()

# 主要市场指数配置
# response_symbol: 腾讯 API 返回的 symbol（用于匹配）
MARKET_INDICES = [
    # A股指数
    {"symbol": "000001", "name": "上证指数", "market": "CN", "tencent_symbol": "sh000001", "response_symbol": "000001"},
    {"symbol": "399001", "name": "深证成指", "market": "CN", "tencent_symbol": "sz399001", "response_symbol": "399001"},
    {"symbol": "399006", "name": "创业板指", "market": "CN", "tencent_symbol": "sz399006", "response_symbol": "399006"},
    # 港股指数
    {"symbol": "HSI", "name": "恒生指数", "market": "HK", "tencent_symbol": "hkHSI", "response_symbol": "HSI"},
    # 亚洲其他指数:腾讯不覆盖,走东财 ulist(secid 显式映射)
    {"symbol": "N225", "name": "日经225", "market": "JP", "tencent_symbol": "", "eastmoney_secid": "100.N225", "response_symbol": "N225"},
    {"symbol": "KS11", "name": "韩国综合", "market": "KR", "tencent_symbol": "", "eastmoney_secid": "100.KS11", "response_symbol": "KS11"},
    # 美股指数 (腾讯返回的 symbol 带点号前缀: .IXIC, .DJI)
    {"symbol": "IXIC", "name": "纳斯达克", "market": "US", "tencent_symbol": "usIXIC", "response_symbol": ".IXIC"},
    {"symbol": "DJI", "name": "道琼斯", "market": "US", "tencent_symbol": "usDJI", "response_symbol": ".DJI"},
]

# 指数响应内存缓存:60s(行情价格要新鲜)。
_INDICES_CACHE: dict[str, tuple[float, list[dict]]] = {}
_INDICES_CACHE_TTL_S = 60

# spark(近20日收盘)独立缓存:日线一天才变,30 分钟足够新鲜。
# 没有它,响应缓存每 60s 过期就要重付一轮 6×指数K线(部分环境东财先失败再腾讯兜底,
# 串行约 4s)——这曾是首页快车道最大的延迟来源。空结果也缓存(坏源别反复重拉)。
_SPARK_CACHE: dict[str, tuple[float, list[float]]] = {}
_SPARK_TTL_S = 1800


def clear_indices_cache() -> None:
    """清空指数响应/spark 缓存(测试隔离用)。"""
    _INDICES_CACHE.clear()
    _SPARK_CACHE.clear()


# 指数收盘价本地留存:日经/韩国综合在部分网络下取不到东财K线接口(云端实测被重置),
# 美股指数腾讯只给 1 根。每天把看到的最新价按日期存下来,几日后就能自己长出走势线,
# 不必依赖单一K线接口。文件在数据卷里,重启/升级都保留。
_CLOSES_LOCK = threading.Lock()
_CLOSES_MAX = 40


def _closes_path() -> Path:
    base = os.environ.get("DATA_DIR", "").strip()
    root = Path(base) if base else Path.cwd() / "data"
    return root / "index_closes.json"


def _load_closes() -> dict[str, list[list]]:
    try:
        data = json.loads(_closes_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _merge_closes(symbol: str, pairs: list[tuple[str, float]]) -> None:
    """合并若干 (日期, 收盘) 到本地留存,同日覆盖;失败静默。"""
    rows_in = [(str(d), float(c)) for d, c in pairs if d and c and c > 0]
    if not symbol or not rows_in:
        return
    try:
        with _CLOSES_LOCK:
            data = _load_closes()
            merged = {
                str(r[0]): float(r[1])
                for r in data.get(symbol, [])
                if isinstance(r, (list, tuple)) and len(r) == 2
            }
            merged.update(rows_in)
            ordered = sorted(merged.items())[-_CLOSES_MAX:]
            new_rows = [[d, c] for d, c in ordered]
            if data.get(symbol) == new_rows:
                # 值没变就不落盘:指数接口每分钟刷新一次,8 个指数会白白写 8 次
                return
            data[symbol] = new_rows
            path = _closes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"指数收盘留存失败 {symbol}: {exc}")


def _stored_rows(symbol: str) -> list[tuple[str, float]]:
    rows = _load_closes().get(symbol) or []
    return [
        (str(r[0]), float(r[1]))
        for r in rows
        if isinstance(r, (list, tuple)) and len(r) == 2
    ]


def _spark_for(idx: dict, quote: dict | None = None) -> list[float]:
    """近 20 日收盘价,供首页指数走势 sparkline 用(带 30min 独立缓存)。

    fail-soft:市场码非法/取数异常/无映射一律吞掉,返回空列表,绝不影响 quote 主体。
    """
    # 缓存命中也要记录当日价,否则本地走势线永远长不出来。
    # 周末不记:非交易日拿到的只是上一个交易日的收盘价,记下来会多出一根假数据。
    if quote and quote.get("current_price") and time.localtime().tm_wday < 5:
        _merge_closes(
            idx["symbol"], [(time.strftime("%Y-%m-%d"), quote["current_price"])]
        )

    now = time.time()
    hit = _SPARK_CACHE.get(idx["symbol"])
    if hit and now - hit[0] < _SPARK_TTL_S:
        return hit[1]

    merged: dict[str, float] = dict(_stored_rows(idx["symbol"]))
    try:
        try:
            market_code = MarketCode(idx["market"])
        except ValueError:
            # JP/KR 不在 MarketCode 枚举里;指数K线只按 secid 取数,market 不参与
            market_code = MarketCode.CN
        klines = get_index_klines(idx["symbol"], market_code, days=20)
        history = [(str(k.date), k.close) for k in klines or []]
        if history:
            merged.update(history)
            # 顺手把K线历史也落盘,换网络环境时不必从零积累
            _merge_closes(idx["symbol"], history)
    except Exception as e:
        logger.debug(f"指数 spark 获取失败 {idx['symbol']}: {e}")

    # get_index_klines 的 lmt 下限是 1200 根,这里统一截成近 20 个交易日
    spark = [close for _, close in sorted(merged.items())][-20:]
    _SPARK_CACHE[idx["symbol"]] = (now, spark)
    return spark


@router.get("/indices")
async def get_market_indices():
    """获取主要市场指数（公共数据，无需认证）"""
    now = time.time()
    cached = _INDICES_CACHE.get("indices")
    if cached and now - cached[0] < _INDICES_CACHE_TTL_S:
        return cached[1]

    tencent_symbols = [idx["tencent_symbol"] for idx in MARKET_INDICES if idx.get("tencent_symbol")]
    em_secids = [idx["eastmoney_secid"] for idx in MARKET_INDICES if idx.get("eastmoney_secid")]

    quotes: list[dict] = []
    try:
        quotes = get_market_data().index_quotes(tencent_symbols) or []
    except Exception as e:
        logger.error(f"获取市场指数(腾讯)失败: {e}")

    # 日经/韩国综合:腾讯无覆盖,东财补。失败只影响这两个,不影响其余指数
    if em_secids:
        try:
            from marketdata.vendors.eastmoney import fetch_index_quotes

            quotes.extend(fetch_index_quotes(em_secids))
        except Exception as e:
            logger.warning(f"获取市场指数(东财)失败: {e}")

    # 构建 response_symbol -> quote 映射
    quote_map = {}
    for q in quotes:
        quote_map[q["symbol"]] = q

    # spark 并行取(缓存未过期时零成本;冷启动=最慢单个≈1s,而非 6 个串行累加)
    sparks = await asyncio.gather(
        *[
            asyncio.to_thread(_spark_for, idx, quote_map.get(idx["response_symbol"]))
            for idx in MARKET_INDICES
        ],
        return_exceptions=True,
    )
    spark_map = {
        idx["symbol"]: (sp if isinstance(sp, list) else [])
        for idx, sp in zip(MARKET_INDICES, sparks)
    }

    result = []
    for idx in MARKET_INDICES:
        # 使用 response_symbol 匹配
        quote = quote_map.get(idx["response_symbol"])
        spark = spark_map.get(idx["symbol"], [])

        if quote:
            result.append({
                "symbol": idx["symbol"],
                "name": idx["name"],
                "market": idx["market"],
                "current_price": quote["current_price"],
                "change_pct": quote["change_pct"],
                "change_amount": quote["change_amount"],
                "prev_close": quote["prev_close"],
                "spark": spark,
            })
        else:
            # 即使没有行情也返回基本信息
            result.append({
                "symbol": idx["symbol"],
                "name": idx["name"],
                "market": idx["market"],
                "current_price": None,
                "change_pct": None,
                "change_amount": None,
                "prev_close": None,
                "spark": spark,
            })

    _INDICES_CACHE["indices"] = (now, result)
    return result
