"""发现(东财热门榜)vendor:单源、市场级、非 symbol 模型。

移植自 PanWatch src/collectors/discovery_collector.py 的
fetch_hot_stocks(L55-105)/fetch_hot_boards(L107-149)/fetch_board_stocks(L151-196)/
_get_json(L198-248):fid/fields/fs/params 计算与 f-code 字段映射(f12/f14/f2/f3/f4/f5/f6)
逐一照搬。原实现是 async(httpx.AsyncClient);此处改为同步 market_get。

不继承 marketdata.vendors.base.Vendor —— discovery 是市场级、单源、非 symbol 的取数,
硬套 symbol-based 失败转移 Engine 是设计错配,故不进 Engine/不进 DataSource taxonomy。
vendor 内不做缓存(TTL 缓存留宿主 collector)。
"""
from __future__ import annotations

from typing import Any

from marketdata.http import market_get
from marketdata.types import HotBoard, HotStock

# push2 occasionally accepts the TCP connection and closes it before sending
# headers in desktop/NAS proxy environments. push2delay exposes the same clist
# contract and is reachable both directly and through the configured proxy.
_STOCKS_API = "https://push2delay.eastmoney.com/api/qt/clist/get"
_BOARDS_API = "https://push2delay.eastmoney.com/api/qt/clist/get"
_HOST_KEY = "push2.eastmoney.com"

# bkzj 榜单走 data.eastmoney.com,与 push2 是两个不同的主机。部分网络出口会
# 直接 reset push2 的连接(桌面/NAS/公司代理常见),而 data.eastmoney.com 仍可达,
# 故作为可切换的第二来源(宿主 DataSource 表按 provider 选择)。
_BKZJ_API = "https://data.eastmoney.com/dataapi/bkzj/getbkzj"
_BKZJ_HOST_KEY = "data.eastmoney.com"

# bkzj 的 code 选择器:语义同 push2 的 fs,但作为 code 参数传入。
# 逗号分隔可合并多个板块(实测 4 段合并返回 5561 只,即全市场 A 股)。
_CN_ALL_STOCKS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
_CN_INDUSTRY_BOARDS = "m:90+t:2"  # 行业板块(与原 push2 实现同参数,约 496 个)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

_BKZJ_HEADERS = {**_HEADERS, "Referer": "https://data.eastmoney.com/"}

# bkzj 的原始值是定点整数,需按字段缩放。f5/f6(量/额)是原值不缩放。
_BKZJ_SCALE = {"f2": 100.0, "f3": 100.0, "f4": 100.0}


def _bkzj_value(row: dict, key: str) -> float | None:
    """取 bkzj 行里 key 对应的数值并按字段缩放。停牌等返回 "-",统一成 None。

    bkzj 的 key 参数既决定"返回哪个字段"也决定排序,所以数值挂在 key 名下。
    """
    raw = row.get(key)
    if raw is None or raw == "-":
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return n / _BKZJ_SCALE.get(key, 1.0)


def _normalize_diff(data: dict | None) -> list[dict]:
    """东财 clist 的 diff 字段可能是 list,也可能是 dict(按 index 为 key)。统一成 list。"""
    diff = ((data or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        return list(diff.values())
    return diff


class DiscoveryVendor:
    """东财热门榜(股票/板块/板块成分)。不继承 Vendor(市场级、非 symbol)。"""

    def hot_stocks(
        self,
        *,
        market: str = "CN",
        mode: str = "turnover",
        limit: int = 20,
        proxy: str | None = None,
    ) -> list[HotStock]:
        market = (market or "CN").upper()

        fid = "f6" if mode == "turnover" else "f3"
        fields = "f12,f14,f2,f3,f6,f5"
        if market == "CN":
            fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"  # A-share
        elif market == "HK":
            fs = "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"  # HK
        elif market == "US":
            fs = "m:105,m:106,m:107"  # US
        else:
            return []

        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 100)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": fields,
        }

        data = self._get_json(_STOCKS_API, params=params, proxy=proxy)
        result: list[HotStock] = []
        for it in _normalize_diff(data):
            try:
                result.append(
                    HotStock(
                        symbol=str(it.get("f12") or "").strip(),
                        market=market,
                        name=str(it.get("f14") or "").strip(),
                        price=it.get("f2"),
                        change_pct=it.get("f3"),
                        turnover=it.get("f6"),
                        volume=it.get("f5"),
                    )
                )
            except Exception:
                continue
        return result

    def hot_boards(
        self,
        *,
        market: str = "CN",
        mode: str = "gainers",
        limit: int = 12,
        proxy: str | None = None,
    ) -> list[HotBoard]:
        if market != "CN":
            return []

        fid = "f3" if mode in ("gainers", "hot") else "f6"
        fields = "f12,f14,f2,f3,f4,f6"
        fs = "m:90+t:2"  # industry boards

        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 100)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": fields,
        }

        data = self._get_json(_BOARDS_API, params=params, proxy=proxy)
        result: list[HotBoard] = []
        for it in _normalize_diff(data):
            try:
                result.append(
                    HotBoard(
                        code=str(it.get("f12") or "").strip(),
                        name=str(it.get("f14") or "").strip(),
                        change_pct=it.get("f3"),
                        change_amount=it.get("f4"),
                        turnover=it.get("f6"),
                    )
                )
            except Exception:
                continue
        return result

    def board_stocks(
        self,
        *,
        board_code: str,
        mode: str = "gainers",
        limit: int = 20,
        proxy: str | None = None,
    ) -> list[HotStock]:
        code = (board_code or "").strip()
        if not code:
            return []

        fid = "f3" if mode in ("gainers", "hot") else "f6"
        fields = "f12,f14,f2,f3,f6,f5"
        fs = f"b:{code}"

        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 100)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": fields,
        }

        data = self._get_json(_STOCKS_API, params=params, proxy=proxy)
        result: list[HotStock] = []
        for it in _normalize_diff(data):
            try:
                result.append(
                    HotStock(
                        symbol=str(it.get("f12") or "").strip(),
                        market="CN",
                        name=str(it.get("f14") or "").strip(),
                        price=it.get("f2"),
                        change_pct=it.get("f3"),
                        turnover=it.get("f6"),
                        volume=it.get("f5"),
                    )
                )
            except Exception:
                continue
        return result

    def _get_json(self, url: str, *, params: dict, proxy: str | None) -> dict[str, Any]:
        data = market_get(
            url,
            host_key=_HOST_KEY,
            params=params,
            headers=_HEADERS,
            proxy=proxy,
            verify=False,
            parse="json",
            retries=1,
            timeout=10,
            min_interval_s=0.0,
            log_label="发现榜单",
        )
        return data or {}


class DiscoveryBkzjVendor:
    """东财 bkzj 榜单(data.eastmoney.com),push2 不可达时的替代来源。

    与 :class:`DiscoveryVendor` 的差异:

    - **主机不同**。bkzj 在 ``data.eastmoney.com``,push2 在 ``push2.eastmoney.com``;
      部分网络出口只 reset 后者。
    - **接口语义不同**。bkzj 的 ``key`` 参数既决定返回哪个字段、也决定按哪个字段
      排序,一次只给一个指标,``fields`` 参数被忽略。因此每行的数值挂在 key 名下。
    - **不返回价格/成交额**。板块与成分股只给代码、名称与该 key 对应的指标;
      ``price``/``turnover``/``volume`` 一律留 None,由宿主用既有行情层补齐
      (见 ``src/modules/market/api/discovery.py`` 的行情补全)。

    同样不继承 ``Vendor``(市场级、非 symbol),不进 Engine。
    """

    def hot_stocks(
        self,
        *,
        market: str = "CN",
        mode: str = "turnover",
        limit: int = 20,
        proxy: str | None = None,
    ) -> list[HotStock]:
        if (market or "CN").upper() != "CN":
            return []  # bkzj 只覆盖 A 股
        key = "f6" if mode == "turnover" else "f3"
        rows = self._fetch(code=_CN_ALL_STOCKS, key=key, proxy=proxy)
        # 排序键是 f3 时顺带拿到涨跌幅;是 f6 时不额外请求 —— 个股价格/涨跌幅/成交额
        # 由宿主的行情层补全(见类文档),比再取一次 f3 更准也更省。
        return [
            HotStock(
                symbol=sym,
                market="CN",
                name=str(r.get("f14") or "").strip(),
                price=None,
                change_pct=_bkzj_value(r, "f3") if key == "f3" else None,
                turnover=None,
                volume=None,
            )
            for r, sym in self._rows(rows, limit)
        ]

    def hot_boards(
        self,
        *,
        market: str = "CN",
        mode: str = "gainers",
        limit: int = 12,
        proxy: str | None = None,
    ) -> list[HotBoard]:
        if (market or "CN").upper() != "CN":
            return []
        key = "f3" if mode in ("gainers", "hot") else "f6"
        rows = self._fetch(code=_CN_INDUSTRY_BOARDS, key=key, proxy=proxy)
        # 板块不能走行情层补全,涨跌幅只能自己取。排序键不是 f3 时(成交额榜)
        # 需再取一次 f3 按 f12 关联,否则前端卡片上的涨跌幅会是空的。
        pct_by_code: dict[str, float | None] = {}
        if key != "f3":
            pct_by_code = {
                sym: _bkzj_value(r, "f3")
                for r, sym in self._rows(self._fetch(code=_CN_INDUSTRY_BOARDS, key="f3", proxy=proxy), None)
            }
        return [
            HotBoard(
                code=sym,
                name=str(r.get("f14") or "").strip(),
                change_pct=_bkzj_value(r, "f3") if key == "f3" else pct_by_code.get(sym),
                # 单 key 请求拿不到涨跌额;板块卡片不展示该项。
                change_amount=None,
                turnover=_bkzj_value(r, "f6") if key == "f6" else None,
            )
            for r, sym in self._rows(rows, limit)
        ]

    def board_stocks(
        self,
        *,
        board_code: str,
        mode: str = "gainers",
        limit: int = 20,
        proxy: str | None = None,
    ) -> list[HotStock]:
        code = (board_code or "").strip()
        if not code:
            return []  # 与 DiscoveryVendor 一致:空代码不发请求
        key = "f3" if mode in ("gainers", "hot") else "f6"
        rows = self._fetch(code=f"b:{code}", key=key, proxy=proxy)
        return [
            HotStock(
                symbol=sym,
                market="CN",
                name=str(r.get("f14") or "").strip(),
                price=None,
                change_pct=_bkzj_value(r, "f3") if key == "f3" else None,
                turnover=None,
                volume=None,
            )
            for r, sym in self._rows(rows, limit)
        ]

    @staticmethod
    def _rows(rows: list[dict], limit: int | None) -> list[tuple[dict, str]]:
        """过滤空代码,按 limit 截断,返回 [(原始行, 代码)]。limit=None 表示不截断。"""
        out = [(r, str(r.get("f12") or "").strip()) for r in rows]
        out = [(r, sym) for r, sym in out if sym]
        return out if limit is None else out[: max(1, min(int(limit), 100))]

    def _fetch(self, *, code: str, key: str, proxy: str | None) -> list[dict]:
        data = market_get(
            _BKZJ_API,
            host_key=_BKZJ_HOST_KEY,
            params={"key": key, "code": code},
            headers=_BKZJ_HEADERS,
            proxy=proxy,
            parse="json",
            retries=1,
            timeout=10,
            min_interval_s=0.0,
            log_label="发现榜单(bkzj)",
        )
        return _normalize_diff(data)


# discovery 的合法 vendor 权威:provider 名 → vendor 类。宿主 DataSource 表按
# (type="discovery", provider=?) 选择;registry.py 的同名映射只覆盖 Engine 类型,
# discovery 不进 Engine 故单独放在这里(与 vendors/discovery.py 同处,避免漂移)。
DISCOVERY_VENDOR_CLASSES: dict[str, type] = {
    "eastmoney": DiscoveryVendor,           # push2 clist(历史默认)
    "eastmoney_bkzj": DiscoveryBkzjVendor,  # data.eastmoney.com bkzj
}

DISCOVERY_PROVIDERS: frozenset[str] = frozenset(DISCOVERY_VENDOR_CLASSES)
