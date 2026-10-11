"""快讯(7×24)vendor:cls(财联社)/ sina(新浪直播)/ eastmoney(东财快讯),均市场级、单源。

三家均为未公开文档的私有接口,字段以浏览器抓包常见键名为准,未文档化字段
(cls 的 level/stock_list、sina 的 ext.stocks)一律用防御性 .get() + 缺失填默认,
避免上游改字段导致硬失败。**待实抓校准**(沙箱代理拦截,无法验证真实响应结构)。

时间统一走 datetime.fromtimestamp(ts, tz=timezone.utc) 或防御解析字符串;
解析失败一律回退到 EPOCH(1970-01-01 UTC),绝不用无参 datetime.now()/time.time(),
保证离线测试可复现。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from marketdata.http import market_get
from marketdata.symbol import Symbol
from marketdata.types import FlashNews
from marketdata.vendors.base import FlashNewsVendor as _FlashNewsVendorBase

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _parse_epoch_seconds(ts) -> datetime:
    """秒级时间戳(int/float/数字字符串)→ UTC datetime;解析失败回退 EPOCH。"""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return _EPOCH


def _parse_datetime_str(s, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime:
    """字符串时间(可能是数字戳或格式化串)→ UTC datetime;解析失败回退 EPOCH。"""
    if s is None:
        return _EPOCH
    text = str(s).strip()
    if not text:
        return _EPOCH
    if text.isdigit():
        return _parse_epoch_seconds(text)
    try:
        return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _EPOCH


def _extract_symbol_code(entry) -> str:
    """防御性地从一个"关联股"元素里取代码:可能是纯字符串,也可能是各种键名的 dict。"""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("code", "stock_code", "stockCode", "SecurityCode", "secu_code", "symbol", "Code"):
            v = entry.get(key)
            if v:
                return str(v).strip()
    return ""


def _extract_symbols(raw) -> list[str]:
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for entry in raw:
        try:
            code = _extract_symbol_code(entry)
            if code:
                out.append(code)
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# cls(财联社)
# ---------------------------------------------------------------------------

_CLS_URL = "https://www.cls.cn/v1/roll/get_roll_list"
_CLS_HOST = "www.cls.cn"

# cls "level" 字段的常见取值(A/B/C 或数字)→ importance;未识别一律 0。
_CLS_LEVEL_MAP = {"A": 3, "B": 2, "C": 1}


def _cls_importance(item: dict) -> int:
    level = item.get("level")
    if isinstance(level, bool):
        return 0
    if isinstance(level, (int, float)):
        try:
            return int(level)
        except (TypeError, ValueError):
            return 0
    if isinstance(level, str):
        return _CLS_LEVEL_MAP.get(level.strip().upper(), 0)
    return 0


def _cls_sign(params: dict) -> str:
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()


# cls 接口对 rn 有硬上限:实测 rn=50 正常,rn>=80 一律返回空(不报错,静默空)。
# 调用方传更大的 limit(如聚合时按用户 limit 透传)会把它整个打空,故在此钳制。
_CLS_MAX_RN = 50


class ClsFlashNewsVendor(_FlashNewsVendorBase):
    name = "cls"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[FlashNews]:
        limit = min(int((config or {}).get("days") or 50), _CLS_MAX_RN)
        params = {
            "appName": "CailianpressWeb",
            "os": "web",
            "sv": "7.7.5",
            "last_time": "",
            "refresh_type": 1,
            "rn": limit,
        }
        sign = _cls_sign(params)
        headers = {"User-Agent": _UA, "Referer": "https://www.cls.cn/"}

        data = market_get(
            _CLS_URL,
            host_key=_CLS_HOST,
            params={**params, "sign": sign},
            headers=headers,
            parse="json",
            retries=2,
            timeout=8,
            log_label="财联社快讯",
        )
        if not data:
            return []

        items = ((data or {}).get("data") or {}).get("roll_data") or []
        result: list[FlashNews] = []
        for item in items:
            try:
                brief = item.get("brief") or ""
                title = (item.get("title") or brief or "").strip()
                content = (item.get("content") or brief or "").strip()
                if not title and not content:
                    continue
                publish_time = _parse_epoch_seconds(item.get("ctime"))
                symbols_out = _extract_symbols(item.get("stock_list") or item.get("shares") or [])
                result.append(
                    FlashNews(
                        source="cls",
                        external_id=str(item.get("id", "")),
                        title=title,
                        content=content,
                        publish_time=publish_time,
                        symbols=symbols_out,
                        importance=_cls_importance(item),
                        url=item.get("shareurl", "") or "",
                    )
                )
            except Exception:
                continue
        return result


# ---------------------------------------------------------------------------
# sina(新浪财经直播)
# ---------------------------------------------------------------------------

_SINA_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
_SINA_HOST = "zhibo.sina.com.cn"


def _sina_symbols(item: dict) -> list[str]:
    """sina 的关联股藏在 ext(JSON 字符串)里的 stocks 字段,防御性解析。"""
    ext_raw = item.get("ext")
    if not ext_raw:
        return []
    try:
        ext = json.loads(ext_raw) if isinstance(ext_raw, str) else ext_raw
    except (ValueError, TypeError):
        return []
    if not isinstance(ext, dict):
        return []
    stocks = ext.get("stocks") or []
    out = []
    for s in stocks:
        if isinstance(s, dict):
            code = s.get("symbol") or s.get("code") or ""
            if code:
                out.append(str(code).strip())
        elif isinstance(s, str) and s:
            out.append(s.strip())
    return out


# sina 直播接口 page_size 过大时行为异常:实测 100 正常,200 只返 10 条(疑似服务端截断)。
_SINA_MAX_PAGE_SIZE = 100


class SinaFlashNewsVendor(_FlashNewsVendorBase):
    name = "sina"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[FlashNews]:
        limit = min(int((config or {}).get("days") or 50), _SINA_MAX_PAGE_SIZE)
        params = {"zhibo_id": 152, "page_size": limit, "dire": "f"}
        headers = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}

        data = market_get(
            _SINA_URL,
            host_key=_SINA_HOST,
            params=params,
            headers=headers,
            parse="json",
            retries=2,
            timeout=8,
            log_label="新浪快讯",
        )
        if not data:
            return []

        feed_list = (
            ((((data or {}).get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
        )
        result: list[FlashNews] = []
        for item in feed_list:
            try:
                content = (item.get("rich_text") or item.get("content") or item.get("text") or "").strip()
                if not content:
                    continue
                publish_time = _parse_datetime_str(item.get("create_time"))
                result.append(
                    FlashNews(
                        source="sina",
                        external_id=str(item.get("id", "")),
                        title=content[:40],
                        content=content,
                        publish_time=publish_time,
                        symbols=_sina_symbols(item),
                        importance=0,
                        url="",
                    )
                )
            except Exception:
                continue
        return result


# ---------------------------------------------------------------------------
# eastmoney(东财快讯)
# ---------------------------------------------------------------------------

_EM_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
_EM_HOST = "np-weblist.eastmoney.com"


class EastmoneyFlashNewsVendor(_FlashNewsVendorBase):
    name = "eastmoney"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[FlashNews]:
        limit = int((config or {}).get("days") or 50)
        params = {
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "sortEnd": "",
            "pageSize": limit,
            # 固定串占位(非随机/非时间戳),避免破坏离线测试可复现性。
            "req_trace": "marketdata",
        }
        headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}

        data = market_get(
            _EM_URL,
            host_key=_EM_HOST,
            params=params,
            headers=headers,
            parse="json",
            retries=2,
            timeout=8,
            log_label="东财快讯",
        )
        if not data:
            return []

        items = ((data or {}).get("data") or {}).get("fastNewsList") or []
        result: list[FlashNews] = []
        for item in items:
            try:
                title = (item.get("title") or "").strip()
                content = (item.get("summary") or "").strip()
                if not title and not content:
                    continue
                publish_time = _parse_datetime_str(item.get("showTime"))
                result.append(
                    FlashNews(
                        source="eastmoney",
                        external_id=str(item.get("id", "")),
                        title=title,
                        content=content,
                        publish_time=publish_time,
                        symbols=[],
                        importance=0,
                        url="",
                    )
                )
            except Exception:
                continue
        return result


# ---------------------------------------------------------------------------
# ths(同花顺,市场级 A 股要闻)
# ---------------------------------------------------------------------------

_THS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
_THS_HOST = "news.10jqka.com.cn"


class ThsFlashNewsVendor(_FlashNewsVendorBase):
    """同花顺 A 股要闻。市场级(接口不接受 symbol 参数),但条目自带关联股
    stock[].stockCode,故 symbol 维度信息比纯市场快讯更丰富。"""

    name = "ths"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[FlashNews]:
        limit = int((config or {}).get("days") or 50)
        params = {"page": 1, "tag": "", "track": "website", "pagesize": limit}
        headers = {"User-Agent": _UA, "Referer": "https://news.10jqka.com.cn/"}

        data = market_get(
            _THS_URL,
            host_key=_THS_HOST,
            params=params,
            headers=headers,
            parse="json",
            retries=2,
            timeout=8,
            log_label="同花顺要闻",
        )
        if not data:
            return []

        items = ((data or {}).get("data") or {}).get("list") or []
        result: list[FlashNews] = []
        for item in items:
            try:
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                result.append(
                    FlashNews(
                        source="ths",
                        external_id=str(item.get("id", "")),
                        title=title,
                        content=(item.get("digest") or "").strip(),
                        publish_time=_parse_epoch_seconds(item.get("rtime")),
                        symbols=_extract_symbols(item.get("stock") or []),
                        importance=0,
                        url=item.get("url", "") or "",
                    )
                )
            except Exception:
                continue
        return result


# ---------------------------------------------------------------------------
# jin10(金十数据)
# ---------------------------------------------------------------------------

_JIN10_URL = "https://flash-api.jin10.com/get_flash_list"
_JIN10_HOST = "flash-api.jin10.com"
# web 端公开的固定应用标识(非用户凭证,所有访客一致)
_JIN10_APP_ID = "bVBF4FyRTn5NJF5n"
_JIN10_VERSION = "1.0.0"
# 金十的 data.title 恒为空,标题写在 content 开头的【】里
_JIN10_TITLE_RE = re.compile(r"^【([^】]{1,60})】")


def _jin10_title(title: str, content: str) -> str:
    """金十标题取法:优先 data.title;空则从 content 抽开头【...】;再无则截断正文。"""
    t = (title or "").strip()
    if t:
        return t
    text = (content or "").strip()
    m = _JIN10_TITLE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text[:40]


class Jin10FlashNewsVendor(_FlashNewsVendorBase):
    """金十数据快讯。市场级,以宏观/数据/公司公告类消息为主,时效密集。"""

    name = "jin10"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[FlashNews]:
        limit = int((config or {}).get("days") or 50)
        params = {"channel": "-8200", "vip": "1"}
        headers = {
            "User-Agent": _UA,
            "Referer": "https://www.jin10.com/",
            "x-app-id": _JIN10_APP_ID,
            "x-version": _JIN10_VERSION,
        }

        data = market_get(
            _JIN10_URL,
            host_key=_JIN10_HOST,
            params=params,
            headers=headers,
            parse="json",
            retries=2,
            timeout=8,
            log_label="金十快讯",
        )
        if not data:
            return []

        items = (data or {}).get("data") or []
        result: list[FlashNews] = []
        for item in items:
            try:
                body = item.get("data") or {}
                content = (body.get("content") or "").strip()
                title = _jin10_title(body.get("title"), content)
                if not title and not content:
                    continue
                try:
                    importance = int(item.get("important") or 0)
                except (TypeError, ValueError):
                    importance = 0
                result.append(
                    FlashNews(
                        source="jin10",
                        external_id=str(item.get("id", "")),
                        title=title,
                        content=content,
                        publish_time=_parse_datetime_str(item.get("time")),
                        symbols=[],
                        importance=max(0, min(importance, 3)),
                        url="",
                    )
                )
            except Exception:
                continue
        return result
