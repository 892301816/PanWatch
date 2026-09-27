"""分钟K线与当日分时(腾讯)。

背景:原先只有日线,周/月是本地从日线聚合的,而开发方案把"分时曲线"列为需要
额外部署 tdx-api 的第 5 批。实测腾讯在**已在用的同一个主机**上另有分钟K线
(kline/mkline)与分时(minute/query)两个端点,无需引入新数据源。

两个容易踩的点在这里钉住:
1. 分时的 volume/amount 是**累计值**(全天单调递增),不是单分钟增量;
2. 分钟端点在**另一个主机**(ifzq.gtimg.cn,日线在 web.ifzq.gtimg.cn),
   且**不带复权**。
"""
import pytest

import marketdata.vendors.kline as kv
from marketdata.symbol import Symbol
from marketdata.types import Bar, IntradayPoint


class TestPeriodRouting:
    def test_requested_period_defaults_to_day(self):
        assert kv.requested_period({}) == "day"
        assert kv.requested_period(None) == "day"
        assert kv.requested_period({"period": " M5 "}) == "m5"

    @pytest.mark.parametrize("period", ["m1", "m5", "m15", "m30", "m60", "intraday"])
    def test_minute_and_intraday_are_flagged_unsupported_for_others(self, period):
        """其它 vendor 见到分钟/分时必须是"不支持",而不是退回日线。

        否则 Engine 的失败转移会把"请求分钟线、拿到日线"当成成功,静默给出错误口径
        的数据 —— 这比拿不到数据更糟。
        """
        assert kv._is_unsupported_here({"period": period}) is True

    @pytest.mark.parametrize("period", ["day", "week", "month", "", None])
    def test_daily_periods_are_supported(self, period):
        assert kv._is_unsupported_here({"period": period} if period else {}) is False

    def test_non_tencent_vendors_return_empty_for_minute(self, monkeypatch):
        """三个非腾讯 vendor 请求分钟线时返回空。"""
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: {"data": {"diff": []}})
        sym = [Symbol.parse("600519", market="CN")]
        cfg = {"period": "m5", "days": 5}
        assert kv.EastmoneyKlineVendor().fetch(sym, cfg) == []
        assert kv.StooqKlineVendor().fetch(sym, cfg) == []
        assert (
            kv.YahooKlineVendor().fetch([Symbol.parse("AAPL", market="US")], cfg) == []
        )


class TestMinuteKlineParsing:
    # 真实响应形态: [时间, 开, 收, 高, 低, 量(手), {}, 换手率]
    ROWS = [
        ["202609241450", "1238.22", "1239.23", "1239.23", "1238.00", "569.00", {}, "0.29"],
        ["202609241455", "1238.96", "1238.10", "1238.96", "1237.33", "664.00", {}, "0.42"],
    ]

    def _payload(self):
        return {"data": {"sh600519": {"m5": self.ROWS, "qt": {}}}}

    def test_parses_ohlc_and_normalizes_time(self, monkeypatch):
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: self._payload())
        out = kv.fetch_tencent_minute_kline("sh600519", "m5", 2)
        assert len(out) == 2
        b = out[0]
        assert isinstance(b, Bar)
        # 时间归一化成可读形态,前端直接显示
        assert b.date == "2026-09-24 14:50"
        # 顺序是 开/收/高/低(与日线 fqkline 一致)
        assert (b.open, b.close, b.high, b.low) == (1238.22, 1239.23, 1239.23, 1238.00)
        assert b.volume == 569.0

    def test_unknown_period_returns_empty(self, monkeypatch):
        called = []
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: called.append(1))
        assert kv.fetch_tencent_minute_kline("sh600519", "h1", 5) == []
        assert kv.fetch_tencent_minute_kline("sh600519", "day", 5) == []
        assert called == [], "不支持的周期不该发请求"

    def test_count_is_clamped(self, monkeypatch):
        seen = {}

        def fake(url, *, params=None, **kw):
            seen["param"] = params["param"]
            return self._payload()

        monkeypatch.setattr(kv, "market_get", fake)
        kv.fetch_tencent_minute_kline("sh600519", "m5", 99999)
        assert seen["param"].endswith(",640")  # mkline 上限

    def test_malformed_rows_are_skipped(self, monkeypatch):
        rows = [["bad"], ["202609241450", "1", "2", "3", "4", "5", {}, "0"], "garbage"]
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: {"data": {"s": {"m5": rows}}})
        out = kv.fetch_tencent_minute_kline("s", "m5", 5)
        assert len(out) == 1 and out[0].date == "2026-09-24 14:50"


class TestIntradayParsing:
    # 真实响应: '时间 价 累计量(手) 累计额(元)'
    LINES = [
        "0930 1250.01 183 22875182.71",
        "0931 1249.99 1393 174122899.37",
        "0932 1247.28 1632 203950104.16",
    ]

    def _payload(self):
        return {
            "data": {
                "sh600519": {
                    "data": {"data": self.LINES, "date": "20260924"},
                    "qt": {"sh600519": ["1", "贵州茅台", "600519", "1237.00", "1251.24"]},
                }
            }
        }

    def test_parses_points_and_prev_close(self, monkeypatch):
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: self._payload())
        pts, prev = kv.fetch_tencent_intraday("sh600519")
        assert len(pts) == 3
        assert isinstance(pts[0], IntradayPoint)
        assert (pts[0].time, pts[0].price) == ("0930", 1250.01)
        # 量额是累计值,原样保留(均价线的计算依赖累计口径)
        assert pts[0].volume == 183.0
        assert pts[2].volume == 1632.0
        assert pts[0].amount == 22875182.71
        # 昨收从响应的 qt 里顺带取,省一次行情请求
        assert prev == 1251.24

    def test_prev_close_none_when_qt_missing(self, monkeypatch):
        payload = {"data": {"s": {"data": {"data": self.LINES}}}}
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: payload)
        pts, prev = kv.fetch_tencent_intraday("s")
        assert len(pts) == 3 and prev is None

    def test_bad_json_returns_empty(self, monkeypatch):
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: "not json")
        assert kv.fetch_tencent_intraday("s") == ([], None)

    def test_empty_response(self, monkeypatch):
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: None)
        assert kv.fetch_tencent_intraday("s") == ([], None)

    def test_short_lines_skipped(self, monkeypatch):
        payload = {"data": {"s": {"data": {"data": ["0930 1.0", self.LINES[0]]}}}}
        monkeypatch.setattr(kv, "market_get", lambda *a, **k: payload)
        pts, _ = kv.fetch_tencent_intraday("s")
        assert len(pts) == 1


class TestVendorDispatch:
    """分时按 DataSource 顺序找声明支持的 vendor。"""

    def _md(self, providers):
        from marketdata import MarketData
        from marketdata.defaults import StaticConfigProvider
        from marketdata.ports import SourceConfig

        cfg = StaticConfigProvider({
            "kline": [SourceConfig(vendor=p, priority=i, enabled=True)
                      for i, p in enumerate(providers)]
        })
        return MarketData(config=cfg)

    def test_uses_first_vendor_that_supports_intraday(self, monkeypatch):
        monkeypatch.setattr(
            kv, "fetch_tencent_intraday",
            lambda tsym, config=None: ([IntradayPoint(time="0930", price=1.0)], 1.0),
        )
        # eastmoney 优先级更高但不支持分时 → 应跳过它落到 tencent
        pts, prev = self._md(["eastmoney", "tencent"]).intraday("600519", market="CN")
        assert len(pts) == 1 and prev == 1.0

    def test_no_supporting_vendor_returns_empty(self, monkeypatch):
        pts, prev = self._md(["eastmoney", "stooq"]).intraday("600519", market="CN")
        assert (pts, prev) == ([], None)
