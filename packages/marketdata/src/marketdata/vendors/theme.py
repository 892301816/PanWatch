"""题材数据 vendor:涨停/炸板/跌停池(push2ex)+ 概念板块榜(push2 clist)。

均 A 股市场级、不进 Engine —— 与 discovery 同模式:宿主按 DataSource.provider 选
vendor,直接调 MarketData 上的分派方法(``md.zt_pool(vendor=...)``)。

**涨停板池是 A 股"炒作题材"最核心的信号源**:
- ``lbc`` 连板数   → 空间高度(出现 9 连板说明接力资金活跃、题材未走完)
- ``fund`` 封板资金 → 承接强度
- ``hybk`` 所属行业 → 可聚合出"今天哪些板块在涨停"
- ``zttj`` 涨停统计 → 近期活跃度(days 天内涨停 ct 次)

两个主机不同,可分别失效:
- ``push2ex.eastmoney.com``  涨停/炸板/跌停池
- ``push2.eastmoney.com``    概念板块(clist)

东财的 ``ut`` 参数是网页端公开的固定常量,非用户凭证。

**未做的事**(明确边界):涨停池条目里的 ``p``(价格)缩放比例未校准,故不解析该字段;
本模块也不需要它 —— 题材聚合只用到涨跌幅、连板数、封板资金、行业。
"""
from __future__ import annotations

import logging
from typing import Any

from marketdata.http import market_get
from marketdata.types import ConceptBoard, ZtPoolItem

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ---- 涨停池系(push2ex) ----
_POOL_HOST_KEY = "push2ex.eastmoney.com"
_ZT_POOL_API = "https://push2ex.eastmoney.com/getTopicZTPool"
_ZB_POOL_API = "https://push2ex.eastmoney.com/getTopicZBPool"
_DT_POOL_API = "https://push2ex.eastmoney.com/getTopicDTPool"
_POOL_HEADERS = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
# 网页端公开的固定 ut(非用户凭证)
_POOL_UT = "7eea3edcaed734bea9cbfc24409ed989"
_POOL_PAGE_SIZE_MAX = 200

# ---- 概念板块(push2 clist) ----
_BOARD_HOST_KEY = "push2.eastmoney.com"
_CLIST_API = "https://push2.eastmoney.com/api/qt/clist/get"
_BOARD_HEADERS = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
# 概念板块选择器(行业板块是 m:90+t:2)
_CONCEPT_BOARDS = "m:90+t:3"
# f12 代码 / f14 名称 / f3 涨跌幅 / f6 成交额 / f62 主力净流入
# f104 上涨家数 / f105 下跌家数 / f128 领涨股名 / f140 领涨股代码 / f136 领涨股涨跌幅
_BOARD_FIELDS = "f12,f14,f3,f6,f62,f104,f105,f128,f140,f136"

# 成分股列表复用同一个 clist 接口,只是把 fs 换成 "b:<板块代码>"。
_STOCK_FIELDS = "f12,f14,f2,f3,f6,f5"
_STOCK_PAGE_SIZE_MAX = 100


def _safe_float(v: Any) -> float | None:
    """东财常把缺失值写成 "-",统一成 None。"""
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int:
    f = _safe_float(v)
    return int(f) if f is not None else 0


def _fmt_seal_time(v: Any) -> str:
    """首次封板时间是 HHMMSS 整数(92500 → "092500"),补零成 6 位便于展示与比较。"""
    n = _safe_int(v)
    return f"{n:06d}" if n > 0 else ""


def _normalize_diff(data: Any) -> list[dict]:
    """东财 clist 的 diff 可能是 list,也可能是 {"0": {...}} 形式,统一成 list。"""
    diff = ((data or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        return list(diff.values())
    return diff


class ThemeVendor:
    """东财题材数据:涨停/炸板/跌停池 + 概念板块榜。不继承 Vendor(市场级、非 symbol)。"""

    name = "eastmoney"

    # -- 涨停池系 ----------------------------------------------------------

    def zt_pool(self, *, date: str, limit: int = 200, proxy: str | None = None) -> list[ZtPoolItem]:
        """涨停池。date 形如 "20261010"(交易日,非交易日返回空)。"""
        return self._fetch_pool(_ZT_POOL_API, date=date, limit=limit, sort="fbt:asc", proxy=proxy)

    def zb_pool(self, *, date: str, limit: int = 200, proxy: str | None = None) -> list[ZtPoolItem]:
        """炸板池(盘中曾涨停但收盘未封住)—— 衡量分歧强度。"""
        return self._fetch_pool(_ZB_POOL_API, date=date, limit=limit, sort="fbt:asc", proxy=proxy)

    def dt_pool(self, *, date: str, limit: int = 200, proxy: str | None = None) -> list[ZtPoolItem]:
        """跌停池。"""
        return self._fetch_pool(_DT_POOL_API, date=date, limit=limit, sort="fund:asc", proxy=proxy)

    def _fetch_pool(
        self, url: str, *, date: str, limit: int, sort: str, proxy: str | None
    ) -> list[ZtPoolItem]:
        params = {
            "ut": _POOL_UT,
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": max(1, min(int(limit), _POOL_PAGE_SIZE_MAX)),
            "sort": sort,
            "date": date,
        }
        data = market_get(
            url,
            host_key=_POOL_HOST_KEY,
            params=params,
            headers=_POOL_HEADERS,
            parse="json",
            retries=1,
            timeout=12,
            min_interval_s=0.2,
            proxy=proxy,
            log_label="东财题材池",
        )
        if not isinstance(data, dict):
            return []

        pool = ((data.get("data") or {}).get("pool")) or []
        out: list[ZtPoolItem] = []
        for it in pool:
            try:
                if not isinstance(it, dict):
                    continue
                stat = it.get("zttj") or {}
                out.append(
                    ZtPoolItem(
                        code=str(it.get("c") or "").strip(),
                        name=str(it.get("n") or "").strip(),
                        change_pct=_safe_float(it.get("zdp")),
                        amount=_safe_float(it.get("amount")),
                        turnover_rate=_safe_float(it.get("hs")),
                        limit_up_days=_safe_int(it.get("lbc")),
                        first_seal_time=_fmt_seal_time(it.get("fbt")),
                        seal_fund=_safe_float(it.get("fund")),
                        break_count=_safe_int(it.get("zbc")),
                        industry=str(it.get("hybk") or "").strip(),
                        stat_days=_safe_int(stat.get("days")),
                        stat_count=_safe_int(stat.get("ct")),
                        float_market_cap=_safe_float(it.get("ltsz")),
                    )
                )
            except Exception:
                continue
        return out

    # -- 概念板块 ----------------------------------------------------------

    def concept_boards(
        self, *, market: str = "CN", limit: int = 200, proxy: str | None = None
    ) -> list[ConceptBoard]:
        """概念板块榜,按主力净流入降序。

        一次请求拿全所有字段(f3 涨幅/f62 主力净流入/f104 上涨家数/f128 领涨股…),
        故不用 bkzj —— 那个接口的 ``key`` 参数一请求只返回一个字段,拿三项要打三次。
        """
        if market != "CN":
            return []

        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 500)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f62",  # 按主力净流入排序
            "fs": _CONCEPT_BOARDS,
            "fields": _BOARD_FIELDS,
        }
        data = market_get(
            _CLIST_API,
            host_key=_BOARD_HOST_KEY,
            params=params,
            headers=_BOARD_HEADERS,
            parse="json",
            retries=1,
            timeout=12,
            min_interval_s=0.2,
            proxy=proxy,
            log_label="东财概念板块",
        )
        if not data:
            return []

        out: list[ConceptBoard] = []
        for it in _normalize_diff(data):
            try:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("f12") or "").strip()
                if not code:
                    continue
                out.append(
                    ConceptBoard(
                        code=code,
                        name=str(it.get("f14") or "").strip(),
                        change_pct=_safe_float(it.get("f3")),
                        turnover=_safe_float(it.get("f6")),
                        main_net_inflow=_safe_float(it.get("f62")),
                        up_count=_safe_int(it.get("f104")),
                        down_count=_safe_int(it.get("f105")),
                        leader_name=str(it.get("f128") or "").strip(),
                        leader_code=str(it.get("f140") or "").strip(),
                        leader_change_pct=_safe_float(it.get("f136")),
                    )
                )
            except Exception:
                continue
        return out

    # -- 板块成分股 --------------------------------------------------------

    def concept_stocks(
        self, *, board_code: str, limit: int = 100, proxy: str | None = None
    ) -> list[dict]:
        """某板块的成分股(涨幅降序)。board_code 形如 "BK0968"。

        **为什么不用 discovery 的 board_stocks**:那条路走 ``push2delay.eastmoney.com``
        (为代理环境准备的延迟行情主机),实测在部分网络出口已被 reset,而普通
        ``push2.eastmoney.com`` 仍可用。概念板块本身就走 push2,成分股复用同一主机
        可保证"板块能拿到 ⇒ 成分股也能拿到",不会出现半通半不通。

        返回 [{code,name,change_pct,turnover,volume}];失败返回 []。
        """
        code = (board_code or "").strip()
        if not code:
            return []

        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), _STOCK_PAGE_SIZE_MAX)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",           # 按涨幅降序 —— 涨停股必然在最前,分页截断不影响求交
            "fs": f"b:{code}",
            "fields": _STOCK_FIELDS,
        }
        data = market_get(
            _CLIST_API,
            host_key=_BOARD_HOST_KEY,
            params=params,
            headers=_BOARD_HEADERS,
            parse="json",
            retries=1,
            timeout=12,
            min_interval_s=0.3,
            proxy=proxy,
            log_label="东财板块成分股",
        )
        if not data:
            return []

        out: list[dict] = []
        for it in _normalize_diff(data):
            try:
                if not isinstance(it, dict):
                    continue
                c = str(it.get("f12") or "").strip()
                if not c:
                    continue
                out.append({
                    "code": c,
                    "name": str(it.get("f14") or "").strip(),
                    "change_pct": _safe_float(it.get("f3")),
                    "turnover": _safe_float(it.get("f6")),
                    "volume": _safe_float(it.get("f5")),
                })
            except Exception:
                continue
        return out


# 与 DISCOVERY_VENDOR_CLASSES 同理:provider 名 → vendor 类,宿主 DataSource 表按
# (type="theme", provider=?) 选择。registry.py 只覆盖 Engine 类型,故单独放这里。
THEME_VENDOR_CLASSES: dict[str, type] = {
    "eastmoney": ThemeVendor,
}

THEME_PROVIDERS: frozenset[str] = frozenset(THEME_VENDOR_CLASSES)
