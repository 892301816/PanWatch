"""把券商成交短信/App 推送/交割单行解析成一笔记账交易。

目标是「宽容解析 + 宁可让人复核」：

- 只认成交（买入/卖出），撤单、废单、申购、分红等返回 ``ok=False`` 并给出原因；
- 代码、价格、数量必须都有；名称、费用、时间、成交编号能取就取；
- 解析结果带 ``confidence`` 与 ``warnings``，低置信度由调用方转人工确认，不直接记账。

不访问数据库、不发网络请求，方便单测和复用（手机转发、交割单、助手工具共用）。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date as date_cls

# 买入/卖出关键词（长的放前面，避免「证券买入」只匹配到「买入」）
_ACTION_PATTERNS: list[tuple[str, str]] = [
    (r"证券买入|担保品买入|融资买入|普通买入|买入", "buy"),
    (r"证券卖出|担保品卖出|融券卖出|普通卖出|卖出", "sell"),
]

_SKIP_PATTERNS = r"撤单|废单|已撤|撤废|取消|废单|申购|赎回|中签|配号|分红|红利|送股|转增|新股|缴款"

# 名称里不该出现的词（用于从「002943宇晶股份200股」里截名字）
_NAME_STOP = {
    "成交", "证券", "股票", "股份", "买入", "卖出", "数量", "价格", "金额",
    "手续费", "佣金", "印花税", "过户费", "成交价", "成交额", "余额", "可用",
}

_CODE_LABELS = r"(?:证券代码|股票代码|证券编码|代码)"
_NAME_LABELS = r"(?:证券名称|股票名称|证券简称|名称|简称)"


def to_halfwidth(text: str) -> str:
    """全角转半角（：，（）等），并压缩空白。"""
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("\u3000", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    return value.strip()


def _to_number(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(str(raw).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _first(pattern: str, text: str) -> re.Match | None:
    return re.search(pattern, text)


def _extract_action(text: str) -> str | None:
    labeled = _first(r"买卖标志\s*[:：]?\s*(买入|卖出)", text)
    if labeled:
        return "buy" if labeled.group(1) == "买入" else "sell"
    for pattern, action in _ACTION_PATTERNS:
        if re.search(pattern, text):
            return action
    return None


def _extract_symbol(text: str) -> tuple[str | None, bool]:
    """返回 (代码, 是否带标签)。带「证券代码」标签的可信度更高。"""
    labeled = _first(_CODE_LABELS + r"\s*[:：]?\s*([A-Za-z]{0,2})\s*(\d{5,6})(?!\d)", text)
    if labeled:
        return labeled.group(2), True
    # 无标签：只接受 A 股常见 6 位前缀，且不能是日期/数量（8 位日期已被 lookaround 排除）
    for m in re.finditer(r"(?<!\d)([0368]\d{5}|4\d{5}|68\d{4}|9\d{5})(?!\d)", text):
        code = m.group(1)
        # 「200股」「5900元」这类数字不会被前缀规则命中；再防一手数量
        if code.startswith(("0", "3", "6", "8", "4", "9")):
            return code, False
    hk = _first(r"(?<!\d)(\d{5})(?!\d)", text)
    if hk:
        return hk.group(1), False
    return None, False


def _extract_name(text: str, code: str | None) -> str | None:
    labeled = _first(_NAME_LABELS + r"\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9*]{2,12})", text)
    if labeled:
        return _clean_name(labeled.group(1))
    if code:
        after = _first(re.escape(code) + r"\s*([\u4e00-\u9fa5A-Za-z*]{2,10})", text)
        if after:
            return _clean_name(after.group(1))
        before = _first(r"([\u4e00-\u9fa5A-Za-z*]{2,10})\s*" + re.escape(code), text)
        if before:
            return _clean_name(before.group(1))
    # 只有名称没有代码：「卖出 宇晶股份 200股」
    loose = _first(r"([\u4e00-\u9fa5*]{2,10})\s*[\d,]+\s*(?:股|手)", text)
    if loose:
        return _clean_name(loose.group(1))
    return None


def _clean_name(value: str | None) -> str | None:
    if not value:
        return None
    # 只裁掉「成交/委托/数量…」这类尾巴，保留「股份」「科技」等正式名称的一部分
    name = re.sub(r"(成交|委托|数量|价格|金额|手续费).*$", "", value).strip()
    name = re.split(r"[\d_\-—]", name)[0].strip()
    if len(name) < 2 or name in _NAME_STOP:
        return None
    return name


def _extract_quantity(text: str) -> int | None:
    labeled = _first(r"(?:成交数量|成交股数|成交笔数|股数|数量)\s*[:：]?\s*([\d,]+)", text)
    if labeled:
        value = _to_number(labeled.group(1))
        return int(value) if value else None
    with_unit = _first(r"([\d,]+)\s*(手|股)", text)
    if with_unit:
        value = _to_number(with_unit.group(1))
        if not value:
            return None
        return int(value * 100) if with_unit.group(2) == "手" else int(value)
    return None


def _extract_price(text: str) -> float | None:
    patterns = [
        r"(?:成交价格|成交均价|成交价|委托价格|价格)\s*[:：]?\s*([\d.]+)",
        r"以\s*([\d.]+)\s*元",
        r"@\s*([\d.]+)",
        r"([\d.]+)\s*元\s*/\s*股",
    ]
    for pattern in patterns:
        m = _first(pattern, text)
        if m:
            value = _to_number(m.group(1))
            if value and 0 < value < 100000:
                return round(value, 4)
    return None


def _fee_value(text: str, labels: str) -> float | None:
    m = _first(rf"(?:{labels})\s*[:：]?\s*([\d.]+)", text)
    if not m:
        return None
    value = _to_number(m.group(1))
    return round(value, 2) if value is not None else None


def _extract_fees(text: str) -> dict:
    commission = _fee_value(text, "佣金|净佣金")
    handling = _fee_value(text, "手续费")
    stamp = _fee_value(text, "印花税")
    transfer = _fee_value(text, "过户费")
    other = _fee_value(text, "规费|经手费|证管费|其他费用")
    total_label = _fee_value(text, "费用合计|总费用|手续费合计")

    # 只有「手续费 5.00」时把它当佣金；佣金与手续费同时出现且对不上时，手续费视为总额
    if commission is None and handling is not None:
        commission = handling
    elif (
        commission is not None
        and handling is not None
        and abs(handling - (commission + (stamp or 0) + (transfer or 0) + (other or 0))) > 0.011
    ):
        total_label = total_label if total_label is not None else handling

    components = [v for v in (commission, stamp, transfer, other) if v is not None]
    total = round(sum(components), 2) if components else 0.0
    if total_label is not None:
        total = total_label
    if total <= 0 and handling:
        total = handling
    stamp = stamp or 0.0
    transfer = transfer or 0.0
    other = other or 0.0
    commission_out = commission if commission is not None else max(0.0, total - stamp - transfer - other)
    return {
        "commission": round(commission_out, 2),
        "stamp_duty": round(stamp, 2),
        "transfer_fee": round(transfer, 2),
        "other_fee": round(other, 2),
        "total": round(total, 2),
    }


def _extract_date(text: str, today: date_cls) -> str:
    m = _first(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = _first(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{today.year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = _first(r"(\d{1,2})\s*[-/]\s*(\d{1,2})", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{today.year:04d}-{month:02d}-{day:02d}"
    return today.isoformat()


def _extract_time(text: str) -> str:
    m = _first(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", text)
    if not m:
        return ""
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    second = int(m.group(3)) if m.group(3) else 0
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _extract_external_id(text: str) -> str:
    m = _first(
        r"(?:成交编号|合同编号|流水号|委托编号|申请编号|成交序号)\s*[:：]?\s*([A-Za-z0-9_-]{4,40})",
        text,
    )
    return m.group(1) if m else ""


def _extract_broker(raw: str) -> str:
    m = _first(r"^[【\[]\s*([^】\]\s]{2,12})\s*[】\]]", (raw or "").strip())
    if m:
        return m.group(1)
    for name in (
        "华泰证券", "中信证券", "国泰海通", "国泰君安", "海通证券", "东方财富", "招商证券",
        "广发证券", "中金财富", "平安证券", "华鑫证券", "国金证券", "银河证券", "申万宏源",
    ):
        if name in raw:
            return name
    return ""


def parse_trade_text(text: str, *, today: date_cls | None = None) -> dict:
    """解析一段成交文本。

    返回字段::

        ok / reason / action / symbol / name / quantity / price / trade_date /
        time / fees / external_id / broker / confidence / warnings / normalized
    """
    today = today or date_cls.today()
    raw = text or ""
    normalized = to_halfwidth(raw)
    result: dict = {
        "ok": False,
        "reason": "",
        "action": None,
        "symbol": "",
        "name": "",
        "quantity": 0,
        "price": 0.0,
        "trade_date": today.isoformat(),
        "time": "",
        "fees": {"commission": 0.0, "stamp_duty": 0.0, "transfer_fee": 0.0, "other_fee": 0.0, "total": 0.0},
        "external_id": "",
        "broker": _extract_broker(raw),
        "confidence": 0.0,
        "warnings": [],
        "normalized": normalized,
    }
    if not normalized:
        result["reason"] = "空文本"
        return result

    if re.search(_SKIP_PATTERNS, normalized) and not re.search(r"成交价|成交价格|成交均价", normalized):
        result["reason"] = "非成交记录（撤单/废单/申购/分红等）"
        return result

    action = _extract_action(normalized)
    symbol, symbol_labeled = _extract_symbol(normalized)
    quantity = _extract_quantity(normalized)
    price = _extract_price(normalized)
    name = _extract_name(normalized, symbol)
    trade_date = _extract_date(normalized, today)
    trade_time = _extract_time(normalized)
    fees = _extract_fees(normalized)
    external_id = _extract_external_id(normalized)

    # 「成交金额 5900 元」里的 5900 不能被当成数量/代码：已有数量标签时以标签为准
    if quantity and symbol and quantity == int(symbol) and not symbol_labeled:
        symbol = None

    missing = []
    if not action:
        missing.append("买卖方向")
    if not symbol and not name:
        missing.append("股票代码或名称")
    if not quantity or quantity <= 0:
        missing.append("成交数量")
    if not price or price <= 0:
        missing.append("成交价格")

    result.update(
        action=action,
        symbol=symbol or "",
        name=name or "",
        quantity=int(quantity or 0),
        price=float(price or 0.0),
        trade_date=trade_date,
        time=trade_time,
        fees=fees,
        external_id=external_id,
    )

    if missing:
        result["reason"] = "缺少" + "、".join(missing)
        result["warnings"] = [f"未解析到{field}" for field in missing]
        # 价格×数量能反推成交金额时，提示可能是通知太简略
        return result

    confidence = 0.6
    if symbol_labeled:
        confidence += 0.15
    if name:
        confidence += 0.1
    if trade_time:
        confidence += 0.05
    if fees["total"] > 0:
        confidence += 0.05
    if external_id:
        confidence += 0.05
    result.update(ok=True, confidence=round(min(confidence, 0.99), 2))
    if not symbol:
        result["warnings"].append("未解析到股票代码，将按名称匹配")
    if not name:
        result["warnings"].append("未解析到股票名称，将按代码补全")
    if fees["total"] <= 0:
        result["warnings"].append("未解析到费用，将按账户费率估算")
    return result
