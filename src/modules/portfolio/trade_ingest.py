"""成交同步的落库层：账户/股票解析、幂等、执行、导入预览。

所有入口（手机短信/通知、交割单、助手工具）都走这里，保证：

- **口径一致**：复用 ``wealth.execute_trade``，与手工记账同一套成本/费用/现金逻辑；
- **幂等**：优先用券商成交编号，其次用「账户+代码+方向+数量+价格+日期」复合键；
- **可预览**：``dry_run`` 只解析不写库，导入接口先预览再确认。
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from src.modules.portfolio import wealth
from src.platform.persistence.models import Account, Position, Stock, TradeRecord

logger = logging.getLogger(__name__)

SOURCE_LABELS = {
    "sms": "短信",
    "notification": "通知",
    "import": "交割单",
    "assistant": "助手",
    "manual": "手工",
}


def resolve_account(
    db: Session, account_id: int | None = None, account_name: str = ""
) -> tuple[Account | None, str]:
    """确定记账账户：指定 id → 指定名称 → 唯一启用账户。"""
    if account_id:
        row = db.query(Account).filter(Account.id == int(account_id)).first()
        return (row, "") if row else (None, f"账户 {account_id} 不存在")
    if account_name:
        row = (
            db.query(Account)
            .filter(Account.name == account_name.strip())
            .first()
        )
        return (row, "") if row else (None, f"账户「{account_name}」不存在")
    rows = db.query(Account).filter(Account.enabled == True).all()  # noqa: E712
    if len(rows) == 1:
        return rows[0], ""
    if not rows:
        return None, "还没有启用中的账户，请先在持仓页创建账户"
    names = "、".join(r.name for r in rows[:5])
    return None, f"有多个账户（{names}），请在请求里指定 account_id"


def resolve_stock(
    db: Session,
    symbol: str = "",
    name: str = "",
    *,
    market: str = "CN",
    create: bool = True,
) -> tuple[Stock | None, str]:
    """按代码/名称找股票；找不到且有代码+名称时自动建自选。"""
    symbol = (symbol or "").strip()
    name = (name or "").strip()
    market = (market or "CN").upper()

    if symbol:
        row = (
            db.query(Stock)
            .filter(Stock.symbol == symbol, Stock.market == market)
            .first()
        )
        if row is None:
            row = db.query(Stock).filter(Stock.symbol == symbol).first()
        if row is not None:
            if not row.name and name:
                row.name = name
            return row, ""
        if not name:
            name = _lookup_name(symbol, market)
        if not create:
            return None, "股票不在自选，导入时会自动创建"
        if not name:
            return None, f"股票 {symbol} 不在自选，且无法获取名称"
        row = Stock(symbol=symbol, name=name, market=market)
        db.add(row)
        db.flush()
        return row, ""

    if name:
        row = db.query(Stock).filter(Stock.name == name).first()
        if row is not None:
            return row, ""
        matched = _search_by_name(name, market)
        if matched:
            symbol, matched_name = matched
            row = db.query(Stock).filter(Stock.symbol == symbol).first()
            if row is not None:
                return row, ""
            if create:
                row = Stock(symbol=symbol, name=matched_name or name, market=market)
                db.add(row)
                db.flush()
                return row, ""
        return None, f"没找到名称「{name}」对应的股票"
    return None, "缺少股票代码或名称"


def _lookup_name(symbol: str, market: str = "CN") -> str:
    try:
        from src.platform.marketdata.stock_list import get_stock_list

        for item in get_stock_list() or []:
            if str(item.get("symbol") or item.get("code") or "") == symbol:
                return str(item.get("name") or "")
    except Exception:  # noqa: BLE001 — 名称补全是锦上添花
        pass
    return ""


def _search_by_name(name: str, market: str = "CN") -> tuple[str, str] | None:
    try:
        from src.platform.marketdata.stock_list import search_stocks

        rows = search_stocks(name, market=market, limit=5) or []
        for row in rows:
            if str(row.get("name") or "").strip() == name.strip():
                return str(row.get("symbol") or ""), str(row.get("name") or "")
    except Exception:  # noqa: BLE001
        pass
    return None


def find_duplicate(
    db: Session,
    *,
    account: Account,
    symbol: str,
    action: str,
    quantity: int,
    price: float,
    trade_date: str,
    external_id: str = "",
) -> TradeRecord | None:
    """幂等查找：成交编号优先，否则复合键 + 价格容差。"""
    base = db.query(TradeRecord).filter(TradeRecord.account_id == account.id)
    if external_id:
        return (
            base.filter(TradeRecord.external_id == external_id)
            .order_by(TradeRecord.id.desc())
            .first()
        )
    rows = (
        base.filter(
            TradeRecord.stock_symbol == symbol,
            TradeRecord.action == action,
            TradeRecord.quantity == int(quantity),
            TradeRecord.trade_date == trade_date,
        )
        .order_by(TradeRecord.id.desc())
        .limit(10)
        .all()
    )
    for row in rows:
        if abs(float(row.price or 0) - float(price)) <= 0.0005:
            return row
    return None


def _trade_payload(row: TradeRecord) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "symbol": row.stock_symbol,
        "name": row.stock_name,
        "action": row.action,
        "quantity": row.quantity,
        "price": row.price,
        "amount": row.amount,
        "commission": row.commission,
        "stamp_duty": row.stamp_duty,
        "transfer_fee": row.transfer_fee,
        "total_fee": row.total_fee,
        "realized_pnl": row.realized_pnl,
        "cash_change": row.cash_change,
        "trade_date": row.trade_date,
        "note": row.note,
        "source": row.source,
        "external_id": row.external_id or "",
    }


def _position_payload(db: Session, account: Account, stock: Stock) -> dict | None:
    row = (
        db.query(Position)
        .filter(Position.account_id == account.id, Position.stock_id == stock.id)
        .first()
    )
    if row is None:
        return None
    return {
        "quantity": row.quantity,
        "cost_price": row.cost_price,
        "invested_amount": row.invested_amount,
    }


def _fee_override(fees: dict, quantity: int, price: float) -> dict:
    """把解析到的费用拼成 ``execute_trade`` 需要的结构。

    解析只给了总额时，把「总额 - 印花税 - 过户费 - 其它」记到佣金，
    保证账面总费用与券商一致（明细里佣金是兜底项）。
    """
    gross = round(float(quantity) * float(price), 2)
    commission = float(fees.get("commission") or 0)
    stamp = float(fees.get("stamp_duty") or 0)
    transfer = float(fees.get("transfer_fee") or 0)
    other = float(fees.get("other_fee") or 0)
    total = float(fees.get("total") or 0)
    if total <= 0:
        total = round(commission + stamp + transfer + other, 2)
    elif abs(total - (commission + stamp + transfer + other)) > 0.011:
        commission = round(max(0.0, total - stamp - transfer - other), 2)
    return {
        "gross": gross,
        "commission": round(commission, 2),
        "stamp_duty": round(stamp, 2),
        "transfer_fee": round(transfer, 2),
        "total_fee": round(total, 2),
    }


def ingest_trade(
    db: Session,
    payload: dict,
    *,
    source: str = "sms",
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """把一条已解析的交易写入账本。

    payload 需要 action/symbol/quantity/price，可选 name/trade_date/time/fees/
    external_id/note/account_id/broker。
    """
    action = str(payload.get("action") or "").lower()
    if action not in ("buy", "sell"):
        return {"ok": False, "error": "成交方向必须是 buy 或 sell"}
    symbol = str(payload.get("symbol") or "").strip()
    name = str(payload.get("name") or "").strip()
    try:
        quantity = int(payload.get("quantity") or 0)
        price = float(payload.get("price") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "成交数量/价格不是数字"}
    if quantity <= 0:
        return {"ok": False, "error": "成交数量必须大于 0"}
    if price <= 0:
        return {"ok": False, "error": "成交价格必须大于 0"}
    if not symbol and not name:
        return {"ok": False, "error": "缺少股票代码或名称"}

    account, account_error = resolve_account(
        db,
        account_id=payload.get("account_id"),
        account_name=str(payload.get("account_name") or ""),
    )
    if account is None:
        return {"ok": False, "error": account_error}

    stock, stock_note = resolve_stock(
        db, symbol, name, market=str(payload.get("market") or "CN"), create=not dry_run
    )
    if stock is None:
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "duplicate": False,
                "needs_stock": True,
                "message": stock_note or "股票不在自选",
                "payload": payload,
                "account": {"id": account.id, "name": account.name},
            }
        db.rollback()
        return {"ok": False, "error": stock_note}

    trade_date = str(payload.get("trade_date") or date.today().isoformat())
    external_id = str(payload.get("external_id") or "").strip()
    duplicate = None
    if not force:
        duplicate = find_duplicate(
            db,
            account=account,
            symbol=stock.symbol,
            action=action,
            quantity=quantity,
            price=price,
            trade_date=trade_date,
            external_id=external_id,
        )
    if duplicate is not None:
        db.rollback()
        return {
            "ok": True,
            "duplicate": True,
            "trade": _trade_payload(duplicate),
            "account": {"id": account.id, "name": account.name},
            "stock": {"symbol": stock.symbol, "name": stock.name},
            "message": "这笔成交已经记过，跳过",
        }

    fees = payload.get("fees") or {}
    fee_override = _fee_override(fees, quantity, price)
    note_parts = [str(payload.get("note") or "").strip()]
    broker = str(payload.get("broker") or "").strip()
    source_label = SOURCE_LABELS.get(source, source)
    tag = f"[{source_label}]"
    if broker:
        tag += f"[{broker}]"
    note_parts.append(tag)
    if external_id:
        note_parts.append(f"成交编号 {external_id}")
    time_text = str(payload.get("time") or "")
    if time_text:
        note_parts.append(time_text)
    note = " ".join(part for part in note_parts if part).strip()[:500]

    if dry_run:
        db.rollback()
        return {
            "ok": True,
            "dry_run": True,
            "duplicate": False,
            "payload": {**payload, "symbol": stock.symbol, "name": stock.name, "trade_date": trade_date},
            "account": {"id": account.id, "name": account.name},
            "stock": {"symbol": stock.symbol, "name": stock.name},
            "fees": fee_override,
            "message": "预览：确认后才会写入账本",
        }

    try:
        result = wealth.execute_trade(
            db,
            account=account,
            stock=stock,
            action=action,
            quantity=quantity,
            price=price,
            trade_date=trade_date,
            note=note,
            source=source,
            fee_override=fee_override,
            deduct_cash=True,
        )
        trade_id = int(result["trade_id"])
        record = db.query(TradeRecord).filter(TradeRecord.id == trade_id).first()
        if record is not None and external_id:
            record.external_id = external_id
        db.commit()
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("成交同步写库失败")
        return {"ok": False, "error": f"写库失败: {exc}"}

    return {
        "ok": True,
        "duplicate": False,
        "trade": _trade_payload(record) if record is not None else {"id": trade_id},
        "account": {"id": account.id, "name": account.name},
        "stock": {"symbol": stock.symbol, "name": stock.name},
        "position": _position_payload(db, account, stock),
        "message": "已记账",
    }


# ── 交割单批量导入 ────────────────────────────────────────────────────────


def annotate_rows(
    db: Session,
    rows: list[dict],
    *,
    account: Account,
) -> list[dict]:
    """给解析后的交割单行标注状态（ok/duplicate/invalid/new_stock）。"""
    out: list[dict] = []
    stock_cache: dict[str, Stock | None] = {}
    for row in rows:
        item = dict(row)
        if not item.get("ok"):
            item["status"] = "invalid"
            item["message"] = item.get("reason") or "解析失败"
            out.append(item)
            continue

        symbol = str(item.get("symbol") or "")
        if symbol not in stock_cache:
            stock, note = resolve_stock(db, symbol, str(item.get("name") or ""), create=False)
            stock_cache[symbol] = stock
        stock = stock_cache[symbol]
        if stock is not None:
            item["name"] = item.get("name") or stock.name
        else:
            item["name"] = item.get("name") or ""

        duplicate = find_duplicate(
            db,
            account=account,
            symbol=symbol,
            action=str(item.get("action") or ""),
            quantity=int(item.get("quantity") or 0),
            price=float(item.get("price") or 0),
            trade_date=str(item.get("trade_date") or ""),
            external_id=str(item.get("external_id") or ""),
        )
        if duplicate is not None:
            item["status"] = "duplicate"
            item["message"] = f"已存在（#{duplicate.id}），跳过"
        else:
            item["status"] = "new_stock" if stock is None else "ok"
            item["message"] = "将自动创建自选" if stock is None else ""
        out.append(item)
    return out


def commit_rows(
    db: Session,
    rows: list[dict],
    *,
    account: Account,
    source: str = "import",
) -> dict:
    """按成交时间顺序写入；单行失败不影响其它行。"""
    ordered = sorted(
        rows,
        key=lambda r: (str(r.get("trade_date") or ""), str(r.get("time") or "00:00:00")),
    )
    results: list[dict] = []
    created = 0
    duplicate = 0
    failed = 0
    for row in ordered:
        if row.get("status") in ("invalid", "duplicate") or not row.get("ok"):
            results.append({"line": row.get("line"), "status": row.get("status") or "invalid", "message": row.get("message") or row.get("reason")})
            continue
        result = ingest_trade(
            db,
            {
                **row,
                "account_id": account.id,
                "market": "CN",
            },
            source=source,
            force=False,
        )
        if result.get("ok") and not result.get("duplicate"):
            created += 1
            status = "created"
        elif result.get("duplicate"):
            duplicate += 1
            status = "duplicate"
        else:
            failed += 1
            status = "error"
        results.append(
            {
                "line": row.get("line"),
                "status": status,
                "message": result.get("message") or result.get("error") or "",
                "trade_id": (result.get("trade") or {}).get("id"),
            }
        )
    return {
        "created": created,
        "duplicate": duplicate,
        "failed": failed,
        "total": len(rows),
        "results": results,
    }
