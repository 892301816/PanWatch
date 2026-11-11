"""交易策略卡:全局默认 + 个股覆盖,以及买入/卖出前检查。

条件格式与选股一致(``{field, op, value}``),字段来自 ``backtest.single.FEATURES``。
判定规则:

- 买入条件为「且」:全部满足才算符合买入;
- 卖出条件为「或」:任意一条命中即符合卖出;
- 都不满足时,按买入条件命中比例给出「接近买入」(≥2/3)或「观察」。
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from src.modules.strategy.backtest.single import (
    FEATURES,
    Bar,
    _features,
    _match,
    load_bars,
)
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import StrategyPlaybook

logger = logging.getLogger(__name__)

LABELS = {f["key"]: f["label"] for f in FEATURES}
UNITS = {f["key"]: f["unit"] for f in FEATURES}
VALID_FIELDS = set(LABELS)

DEFAULT_BUY = [{"field": "runup_3d", "op": "between", "value": [-5, 0]}]
DEFAULT_SELL = [{"field": "profit_ratio", "op": "gte", "value": 95}]


def _clean(conditions: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for c in conditions or []:
        if not isinstance(c, dict):
            continue
        field = str(c.get("field") or "").strip()
        if field not in VALID_FIELDS:
            continue
        op = str(c.get("op") or "eq").strip()
        out.append({"field": field, "op": op, "value": c.get("value")})
    return out


def _row(row: StrategyPlaybook, has_global: bool) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "symbol": row.symbol or "",
        "name": row.name or "",
        "buy": row.buy_conditions or [],
        "sell": row.sell_conditions or [],
        "note": row.note or "",
        "enabled": bool(row.enabled),
        "is_override": row.scope == "stock" and has_global,
    }


def list_playbooks(db: Session | None = None) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        rows = (
            db.query(StrategyPlaybook)
            .order_by(StrategyPlaybook.scope.asc(), StrategyPlaybook.id.asc())
            .all()
        )
        has_global = any(r.scope == "global" for r in rows)
        return {
            "items": [_row(r, has_global) for r in rows],
            "fields": FEATURES,
            "defaults": {"buy": DEFAULT_BUY, "sell": DEFAULT_SELL},
        }
    finally:
        if own:
            db.close()


def _upsert(
    db: Session, *, scope: str, symbol: str, payload: dict
) -> StrategyPlaybook:
    row = (
        db.query(StrategyPlaybook)
        .filter(StrategyPlaybook.scope == scope, StrategyPlaybook.symbol == symbol)
        .first()
    )
    if row is None:
        row = StrategyPlaybook(scope=scope, symbol=symbol)
        db.add(row)
    row.name = str(payload.get("name") or row.name or "")
    row.buy_conditions = _clean(payload.get("buy"))
    row.sell_conditions = _clean(payload.get("sell"))
    row.note = str(payload.get("note") or "")
    row.enabled = bool(payload.get("enabled", True))
    db.commit()
    db.refresh(row)
    return row


def save_playbook(payload: dict, db: Session | None = None) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        scope = "stock" if str(payload.get("scope") or "") == "stock" else "global"
        symbol = str(payload.get("symbol") or "").strip() if scope == "stock" else ""
        if scope == "stock" and not symbol:
            raise ValueError("个股策略必须指定 symbol")
        row = _upsert(db, scope=scope, symbol=symbol, payload=payload)
        has_global = (
            db.query(StrategyPlaybook).filter(StrategyPlaybook.scope == "global").count() > 0
        )
        return _row(row, has_global)
    finally:
        if own:
            db.close()


def delete_playbook(playbook_id: int, db: Session | None = None) -> bool:
    own = db is None
    db = db or SessionLocal()
    try:
        row = db.query(StrategyPlaybook).filter(StrategyPlaybook.id == playbook_id).first()
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True
    finally:
        if own:
            db.close()


def resolve(symbol: str, db: Session) -> StrategyPlaybook | None:
    """个股策略优先,其次全局;都关闭时返回 None。"""
    stock_row = (
        db.query(StrategyPlaybook)
        .filter(StrategyPlaybook.scope == "stock", StrategyPlaybook.symbol == symbol)
        .first()
    )
    if stock_row is not None and stock_row.enabled:
        return stock_row
    global_row = (
        db.query(StrategyPlaybook).filter(StrategyPlaybook.scope == "global").first()
    )
    if global_row is not None and global_row.enabled:
        return global_row
    return None


def _current_bars(symbol: str) -> list[Bar]:
    """日K + 今日实时快照(若当日 K 还没入库)。"""
    bars = load_bars(symbol, days=520)
    if not bars:
        return bars
    try:
        from src.platform.marketdata.marketdata_client import md_quote_rows

        rows = md_quote_rows([symbol], "CN")
        quote = rows[0] if rows else None
        price = float(quote.get("current_price") or 0) if quote else 0.0
        today = date.today().isoformat()
        if price > 0 and bars[-1].date < today:
            bars.append(
                Bar(
                    date=today,
                    open=float(quote.get("open_price") or price),
                    high=float(quote.get("high_price") or price),
                    low=float(quote.get("low_price") or price),
                    close=price,
                    volume=float(quote.get("volume") or 0),
                )
            )
    except Exception:  # noqa: BLE001 — 取不到实时价就用最近收盘
        pass
    return bars


def _fmt(value, field: str) -> str:
    if value is None:
        return "--"
    unit = UNITS.get(field, "")
    if unit == "%":
        return f"{value:+.2f}%"
    if unit == "倍":
        return f"{value:.2f} 倍"
    return f"{value}"


def _expect(cond: dict) -> str:
    op = cond.get("op")
    value = cond.get("value")
    field = cond.get("field", "")
    unit = UNITS.get(field, "")
    if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{value[0]} ~ {value[1]}{unit}"
    if op == "gte":
        return f"≥ {value}{unit}"
    if op == "lte":
        return f"≤ {value}{unit}"
    if op == "gt":
        return f"> {value}{unit}"
    if op == "lt":
        return f"< {value}{unit}"
    if op == "ne":
        return f"≠ {value}{unit}"
    if op in ("in", "not_in"):
        return f"{'属于' if op == 'in' else '不属于'} {value}"
    return f"= {value}{unit}"


def check_symbol(symbol: str, db: Session | None = None) -> dict:
    """当前时点的策略检查:返回逐条对照与结论。"""
    own = db is None
    db = db or SessionLocal()
    try:
        playbook = resolve(symbol, db)
        if playbook is None:
            return {"symbol": symbol, "verdict": "未配置策略", "checks": [], "has_playbook": False}
        buy = _clean(playbook.buy_conditions)
        sell = _clean(playbook.sell_conditions)
        name = playbook.name or ("个股策略" if playbook.scope == "stock" else "全局策略")
        scope = playbook.scope
    finally:
        if own:
            db.close()

    bars = _current_bars(symbol)
    if len(bars) < 60:
        return {"symbol": symbol, "verdict": "数据不足", "checks": [], "has_playbook": True}

    row = _features(bars, len(bars) - 1)

    def build(conds: list[dict]) -> list[dict]:
        out = []
        for c in conds:
            ok = _match(row, c)
            out.append(
                {
                    "field": c["field"],
                    "label": LABELS.get(c["field"], c["field"]),
                    "op": c.get("op"),
                    "expect": _expect(c),
                    "actual": _fmt(row.get(c["field"]), c["field"]),
                    "ok": ok,
                }
            )
        return out

    buy_checks = build(buy)
    sell_checks = build(sell)
    buy_hits = sum(1 for c in buy_checks if c["ok"])
    sell_hit = any(c["ok"] for c in sell_checks)

    if sell_checks and sell_hit:
        verdict = "符合卖出"
    elif buy_checks and buy_hits == len(buy_checks):
        verdict = "符合买入"
    elif buy_checks and buy_hits / len(buy_checks) >= 2 / 3:
        verdict = "接近买入"
    else:
        verdict = "观察"

    return {
        "symbol": symbol,
        "name": name,
        "scope": scope,
        "verdict": verdict,
        "buy_hits": buy_hits,
        "buy_total": len(buy_checks),
        "sell_hits": sum(1 for c in sell_checks if c["ok"]),
        "sell_total": len(sell_checks),
        "buy_checks": buy_checks,
        "sell_checks": sell_checks,
        "price": round(bars[-1].close, 3),
        "as_of": bars[-1].date,
        "estimated": bars[-1].date == date.today().isoformat(),
        "has_playbook": True,
    }


def board(symbols: list[str], db: Session | None = None) -> dict:
    """批量检查(首页卡片 / 列表徽标用)。"""
    out = []
    for symbol in symbols:
        try:
            out.append(check_symbol(symbol, db=db))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[策略] {symbol} 检查失败: {exc}")
    return {"items": out}
