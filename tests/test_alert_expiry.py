"""提醒过期时间:时区口径与"管到哪个交易日收盘"。

回归背景:原实现把计划日期当天 23:59 的 **本地时间** 当作 **UTC** 存,界面上再 +8
回显 —— 用户选 15:00 会看到 23:00,自动生成的在 9/27 晚上加、9/28 显示成 07:59,
既不在任何市场边界上,又仍然早于开盘。而用户的典型用法是"收盘后复盘,把这套价位
加到第二天的盘",那个时间点等于提醒还没用上就已经过期。
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.modules.market.price_alert_service import (
    _app_tz,
    default_alert_expiry,
    parse_expire_at,
)

# 2026-09-26 周六 / 09-27 周日 / 09-28 周一 / 09-29 周二
SAT, SUN, MON, TUE = (datetime(2026, 9, d) for d in (26, 27, 28, 29))


def _local(dt_utc_naive: datetime) -> datetime:
    """把库里存的 UTC naive 还原成应用时区的本地时间(与 API 的 _format_datetime 同口径)。"""
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(_app_tz())


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, tzinfo=_app_tz())


class TestParseExpireAt:
    def test_naive_input_is_read_as_local(self):
        """无时区输入按本地时间解释 —— 前端日期选择器选 15:00 就该是本地 15:00。"""
        parsed = parse_expire_at("2026-09-28 15:00:00")
        assert _local(parsed).hour == 15
        assert _local(parsed).date() == MON.date()

    def test_naive_input_is_not_stored_as_local(self):
        """存的是 UTC,所以非 UTC 时区下两者不该相等(修复前正是相等的)。"""
        parsed = parse_expire_at("2026-09-28 15:00:00")
        if _app_tz() != timezone.utc:
            assert parsed.hour != 15 or parsed != datetime(2026, 9, 28, 15, 0, 0)

    def test_aware_input_keeps_its_instant(self):
        aware = datetime(2026, 9, 28, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert _local(parse_expire_at(aware)).hour == 15
        assert parse_expire_at(aware) == aware.astimezone(timezone.utc).replace(tzinfo=None)

    def test_datetime_passthrough_is_normalized_to_utc(self):
        aware = datetime(2026, 9, 28, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert parse_expire_at(aware).tzinfo is None

    def test_empty_returns_none(self):
        assert parse_expire_at(None) is None
        assert parse_expire_at("") is None

    def test_bad_format_raises(self):
        with pytest.raises(ValueError):
            parse_expire_at("不是时间")


class TestDefaultAlertExpiry:
    """默认过期时间 = 下一个交易日收盘(本地 15:00)。返回 aware 本地时间。"""

    def test_intraday_expires_today_close(self):
        """盘中加提醒 → 管到今天收盘。"""
        got = default_alert_expiry(_at(MON, 10, 30))
        assert got.date() == MON.date()
        assert (got.hour, got.minute) == (15, 0)

    def test_before_open_expires_today_close(self):
        """开盘前加提醒 → 也是管到今天收盘(即将到来的那个交易日)。"""
        got = default_alert_expiry(_at(MON, 8, 0))
        assert got.date() == MON.date()
        assert got.hour == 15

    def test_after_close_rolls_to_next_trading_day(self):
        """收盘后复盘 → 顺延到下一个交易日,否则提醒一建就已过期。"""
        got = default_alert_expiry(_at(MON, 16, 0))
        assert got.date() == TUE.date()
        assert got.hour == 15

    def test_weekend_rolls_to_next_trading_day(self):
        """周末加提醒 → 下个交易日收盘(用户"前一晚复盘加到第二天"的主场景)。"""
        got = default_alert_expiry(_at(SUN, 20, 0))
        assert got.date() == MON.date()
        assert got.hour == 15

    def test_saturday_evening_also_lands_on_monday(self):
        got = default_alert_expiry(_at(SAT, 21, 0))
        assert got.date() == MON.date()

    def test_result_is_aware_local(self):
        """返回带时区的本地时间 —— 若返回 naive-UTC,下游会当本地时间再转一次(实测转成 07:00)。"""
        got = default_alert_expiry(_at(MON, 10, 0))
        assert got.tzinfo is not None
        assert got.hour == 15

    def test_roundtrip_through_parse_lands_on_local_close(self):
        """默认值经 parse_expire_at 落库后,回显必须还是本地 15:00(防双重转换)。"""
        stored = parse_expire_at(default_alert_expiry(_at(SUN, 20, 0)))
        assert stored.tzinfo is None
        back = _local(stored)
        assert back.date() == MON.date()
        assert (back.hour, back.minute) == (15, 0)

    def test_expiry_is_in_the_future(self):
        """任何情况下都不该返回一个已经过去的时间点。"""
        for when in (_at(MON, 8, 0), _at(MON, 16, 0), _at(SUN, 12, 0)):
            assert default_alert_expiry(when) > when
