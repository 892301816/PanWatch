"""个股概览补充数据:主营构成 + 快照估值指标。

主营构成走东财 F10(datacenter.eastmoney.com),与行情 push2 不是同一域名,
在本机网络下可用。结果按股票缓存 6 小时,AI 上下文与概览弹窗共用。
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://emweb.securities.eastmoney.com/",
}
_TTL_S = 6 * 3600
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()

_TYPE_LABELS = {"1": "按产品", "2": "按行业", "3": "按地区", "4": "其他"}
_EXCHANGE = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}


def _secucode(symbol: str, market: str = "CN") -> str:
    code = str(symbol or "").strip().split(".")[0]
    market = (market or "CN").upper()
    if market != "CN":
        return ""
    if code.startswith(("6", "9")):
        ex = "SH"
    elif code.startswith(("4", "8")) or code.startswith("92"):
        ex = "BJ"
    else:
        ex = "SZ"
    return f"{code}.{ex}"


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def fetch_business(symbol: str, market: str = "CN") -> dict:
    """最新报告期的主营构成。失败返回 {"error": ...} 而不抛异常。"""
    secucode = _secucode(symbol, market)
    if not secucode:
        return {"symbol": symbol, "groups": [], "error": "暂不支持非 A 股"}

    key = secucode
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _TTL_S:
        return cached[1]

    params = {
        "reportName": "RPT_F10_FN_MAINOP",
        "columns": (
            "SECUCODE,REPORT_DATE,MAINOP_TYPE,ITEM_NAME,"
            "MAIN_BUSINESS_INCOME,MBI_RATIO,GROSS_RPOFIT_RATIO"
        ),
        "filter": f'(SECUCODE="{secucode}")',
        "pageSize": "100",
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "source": "HSF10",
        "client": "PC",
    }
    try:
        resp = httpx.get(_URL, params=params, headers=_HEADERS, timeout=12)
        resp.raise_for_status()
        rows = ((resp.json() or {}).get("result") or {}).get("data") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[概览] 主营构成获取失败 {symbol}: {exc}")
        return {"symbol": symbol, "groups": [], "error": str(exc)[:120]}

    if not rows:
        return {"symbol": symbol, "groups": [], "error": "无主营构成数据"}

    latest = max(str(r.get("REPORT_DATE") or "")[:10] for r in rows)
    latest_rows = [r for r in rows if str(r.get("REPORT_DATE") or "")[:10] == latest]

    groups = []
    for type_code in sorted({str(r.get("MAINOP_TYPE") or "") for r in latest_rows}):
        items = [r for r in latest_rows if str(r.get("MAINOP_TYPE") or "") == type_code]
        parsed = []
        for r in items:
            name = str(r.get("ITEM_NAME") or "").strip()
            # 东财会把「其他」的明细再列一遍(如「其中:其他」),避免重复计入口径
            if not name or name.startswith("其中"):
                continue
            income = _num(r.get("MAIN_BUSINESS_INCOME"))
            ratio = _num(r.get("MBI_RATIO"))
            margin = _num(r.get("GROSS_RPOFIT_RATIO"))
            parsed.append(
                {
                    "name": name,
                    "income": income,
                    "income_yi": round(income / 1e8, 2) if income is not None else None,
                    "ratio": round(ratio * 100, 2) if ratio is not None else None,
                    "gross_margin": round(margin * 100, 2) if margin is not None else None,
                }
            )
        parsed.sort(key=lambda x: (x["income"] is None, -(x["income"] or 0)))
        if parsed:
            groups.append(
                {
                    "type": type_code,
                    "label": _TYPE_LABELS.get(type_code, f"类型{type_code}"),
                    "items": parsed[:12],
                }
            )

    # 「按产品/按行业/按地区」常常是同一批数字换个标签(如 000858 行业与地区收入完全相同)。
    # 用收入金额指纹去重,保留类型优先级更高(产品 > 行业 > 地区)的那组。
    if len(groups) > 1:
        groups.sort(key=lambda g: {"1": 0, "2": 1, "3": 2}.get(g["type"], 3))
        seen: set[tuple] = set()
        deduped = []
        for g in groups:
            fingerprint = tuple(
                round(float(i["income"]), -4) for i in g["items"] if i.get("income") is not None
            )
            if fingerprint and fingerprint in seen:
                continue
            if fingerprint:
                seen.add(fingerprint)
            deduped.append(g)
        groups = deduped

    out = {
        "symbol": str(symbol),
        "report_date": latest,
        "groups": groups,
        "error": "",
    }
    with _LOCK:
        _CACHE[key] = (now, out)
    return out


def business_summary(symbol: str, limit: int = 3) -> str:
    """给 AI 用的一行摘要:按首选分组取收入占比最高的几项。"""
    data = fetch_business(symbol)
    groups = data.get("groups") or []
    if not groups:
        return ""
    # 按 产品 > 行业 > 地区 的优先级取第一组真正拆开的(≥2 项);
    # 「制造业 100%」这种单一汇总行没有信息量,直接跳过。
    priority = {"1": 0, "2": 1, "3": 2}
    ordered = sorted(groups, key=lambda g: priority.get(g["type"], 3))
    group = next((g for g in ordered if len(g["items"]) >= 2), ordered[0])
    parts = []
    for item in group["items"][:limit]:
        income = item.get("income")
        ratio = item.get("ratio")
        margin = item.get("gross_margin")
        if income is None:
            continue
        text = f"{item['name']} {income / 1e8:.2f}亿"
        if ratio is not None:
            text += f"(占比{ratio:.1f}%)"
        if margin is not None:
            text += f"(毛利率{margin:.1f}%)"
        parts.append(text)
    if not parts:
        return ""
    return f"{data.get('report_date', '')} {group.get('label', '')}：" + "；".join(parts)


def snapshot_metrics(symbol: str) -> dict:
    """从本地全市场快照取估值/流动性指标(换手、量比、PE、市值)。"""
    try:
        from sqlalchemy import text

        from src.platform.persistence.database import SessionLocal

        db = SessionLocal()
        try:
            row = db.execute(
                text(
                    "SELECT trade_date, turnover_rate, volume_ratio, pe_ttm, "
                    "total_mv, float_mv, amplitude FROM screener_snapshots "
                    "WHERE symbol = :symbol ORDER BY trade_date DESC LIMIT 1"
                ),
                {"symbol": str(symbol)},
            ).fetchone()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return {}
    if not row:
        return {}
    return {
        "trade_date": row[0],
        "turnover_rate": row[1],
        "volume_ratio": row[2],
        "pe_ttm": row[3],
        "total_mv": (row[4] / 1e8) if row[4] else None,
        "float_mv": (row[5] / 1e8) if row[5] else None,
        "amplitude": row[6],
    }
