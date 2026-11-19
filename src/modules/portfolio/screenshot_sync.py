"""持仓截图的差异计划和原子同步；不把估算流水当作券商真实成交。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from copy import deepcopy
from datetime import date

from sqlalchemy.orm import Session

from src.modules.portfolio import wealth
from src.platform.persistence.models import Account, Position, Stock, StockAgent

logger = logging.getLogger(__name__)


class StalePreviewError(ValueError):
    pass


def _stock(db: Session, row: dict) -> Stock | None:
    # 当前识别器仅支持六位 A 股代码，禁止 resolve_stock 的跨市场回退。
    query = db.query(Stock).filter(Stock.market == "CN")
    if row.get("symbol"):
        return query.filter(Stock.symbol == row["symbol"]).first()
    return query.filter(Stock.name == row.get("name")).first()


def state_hash(db: Session, account: Account) -> str:
    """Agent 是股票级绑定，其他账户持仓的变化也会影响解绑计划。"""
    snapshot = {
        "account": [account.id, account.available_funds],
        "positions": [
            [p.id, p.account_id, p.stock_id, p.quantity, p.cost_price]
            for p in db.query(Position).order_by(Position.id).populate_existing().all()
        ],
        "agents": [
            [a.id, a.stock_id, a.agent_name, a.schedule, a.ai_model_id, a.notify_channel_ids]
            for a in db.query(StockAgent).order_by(StockAgent.id).populate_existing().all()
        ],
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def missing_positions(db: Session, account: Account, rows: list[dict]) -> list[dict]:
    seen = {s.id for row in rows if (s := _stock(db, row)) is not None}
    return [
        {
            "symbol": s.symbol, "name": s.name, "quantity": 0,
            "cost_price": float(p.cost_price or 0), "closing": True,
            "missing": True,
        }
        for p, s in db.query(Position, Stock).join(Stock, Stock.id == Position.stock_id)
        .filter(Position.account_id == account.id, Position.quantity > 0, Stock.market == "CN")
        .order_by(Position.id).all()
        if s.id not in seen
    ]


def _agent_changes(db: Session, account: Account, rows: list[dict]) -> dict:
    before = {
        s.id: s for p, s in db.query(Position, Stock).join(Stock, Stock.id == Position.stock_id)
        .filter(Position.account_id == account.id, Position.quantity > 0, Stock.market == "CN").all()
    }
    cleared = set()
    targets = []
    for row in rows:
        stock = _stock(db, row)
        if int(row.get("quantity") or 0) == 0:
            if stock and stock.id in before:
                cleared.add(stock.id)
        elif stock is None or stock.id not in before:
            targets.append({"symbol": row["symbol"], "name": row.get("name") or row["symbol"],
                            "stock_id": stock.id if stock else None})

    sources = cleared or set(before)
    bindings = db.query(StockAgent).filter(StockAgent.stock_id.in_(sources)).order_by(StockAgent.id).all()
    profiles: dict[str, list[StockAgent]] = {}
    for binding in bindings:
        profiles.setdefault(binding.agent_name, []).append(binding)
    existing = {(a.stock_id, a.agent_name) for a in db.query(StockAgent).all()}
    changes: dict = {"attach": [], "detach": [], "retained": [], "warnings": []}
    conflicts = set()
    for name, candidates in profiles.items():
        configs = {
            json.dumps([a.schedule or "", a.ai_model_id, sorted(a.notify_channel_ids or [])])
            for a in candidates
        }
        if targets and len(configs) > 1:
            conflicts.add(name)
            changes["warnings"].append(f"{name} 在原持仓中的配置不同，保留原绑定，请手动选择新股票配置")
            continue
        source = candidates[0]
        for target in targets:
            item = {"symbol": target["symbol"], "name": target["name"], "agent_name": name}
            if (target["stock_id"], name) in existing:
                changes["retained"].append({**item, "reason": "保留新股票已有配置"})
            else:
                changes["attach"].append({
                    **item, "config": {"schedule": source.schedule or "", "ai_model_id": source.ai_model_id,
                                       "notify_channel_ids": deepcopy(source.notify_channel_ids or [])},
                })
    other_held = {
        stock_id for (stock_id,) in db.query(Position.stock_id)
        .filter(Position.account_id != account.id, Position.quantity > 0, Position.stock_id.in_(cleared)).all()
    }
    for binding in bindings:
        if binding.stock_id not in cleared:
            continue
        stock = before[binding.stock_id]
        item = {"symbol": stock.symbol, "name": stock.name, "agent_name": binding.agent_name}
        if binding.stock_id in other_held or binding.agent_name in conflicts:
            changes["retained"].append({**item, "reason": "其他账户仍持有" if binding.stock_id in other_held else "配置冲突，保留原绑定"})
        else:
            changes["detach"].append({**item, "id": binding.id})
    return changes


def agent_plan(db: Session, account: Account, rows: list[dict]) -> dict:
    changes = _agent_changes(db, account, rows)
    return {
        key: [{k: v for k, v in item.items() if k not in ("config", "id")} for item in changes[key]]
        for key in ("attach", "detach", "retained")
    } | {"warnings": changes["warnings"]}


def _number(value, label: str, *, allow_zero: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}无效，请核对预览") from None
    if not math.isfinite(result) or result < 0 or (not allow_zero and result == 0):
        raise ValueError(f"{label}无效，请核对预览")
    return result


def apply_snapshot(
    db: Session, *, account: Account, rows: list[dict], remove_missing: bool = False,
    available_funds: float | None = None, record_trades: bool = False,
    sync_agents: bool = False, missing_rows: list[dict] | None = None,
    expected_state_hash: str = "", trade_date: str | None = None,
) -> dict:
    from src.modules.portfolio.screenshot_import import _annotate_positions

    account_id, account_name = account.id, account.name
    try:
        # SQLite 的读事务不加写锁；先串行化“核对状态→写入”，防止双击生成重复流水。
        connection = db.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        db.refresh(account)
        if expected_state_hash and state_hash(db, account) != expected_state_hash:
            raise StalePreviewError("账户持仓或 Agent 配置已变化，请重新识别截图后再同步")
        day = trade_date or date.today().isoformat()
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError:
            raise ValueError("变动记账日期无效") from None
        if parsed_day.isoformat() != day:
            raise ValueError("变动记账日期须为 YYYY-MM-DD")
        if parsed_day > date.today():
            raise ValueError("变动记账日期不能晚于今天")
        cash = None if available_funds is None else _number(available_funds, "可用资金", allow_zero=True)
        prepared, seen = [], set()
        if not rows:
            raise ValueError("没有可提交的持仓")
        for raw in rows:
            row = dict(raw)
            qty = _number(row.get("quantity"), "持仓数量", allow_zero=True)
            if qty != int(qty):
                raise ValueError("持仓数量必须为整数")
            row["quantity"] = int(qty)
            row["closing"] = qty == 0
            stock = _stock(db, row)
            symbol = stock.symbol if stock else str(row.get("symbol") or "")
            if len(symbol) != 6 or not symbol.isdigit():
                raise ValueError("存在无法识别的股票，请核对代码；本次未同步任何变动")
            if symbol in seen:
                raise ValueError(f"{symbol} 在截图中重复，请合并后重试")
            seen.add(symbol)
            row["symbol"] = symbol
            if qty:
                row["cost_price"] = _number(row.get("cost_price"), f"{symbol} 成本价")
            prepared.append(row)
        missing = missing_positions(db, account, prepared) if remove_missing else []
        overrides = {row.get("symbol"): row for row in (missing_rows or [])}
        for row in missing:
            override = overrides.get(row["symbol"], {})
            for key in ("trade_price", "price_source"):
                if key in override:
                    row[key] = override[key]
        prepared += missing
        edits = [{key: row[key] for key in ("trade_price", "price_source") if key in row} for row in prepared]
        _annotate_positions(db, prepared, account)
        for row, edit in zip(prepared, edits):
            row.update(edit)
            if record_trades and row.get("trade_action"):
                row["trade_price"] = _number(row.get("trade_price"), f"{row['symbol']} 记账价格")

        changes = _agent_changes(db, account, prepared) if sync_agents else None
        baseline = db.query(Position).filter(Position.account_id == account.id, Position.quantity > 0).count() == 0
        result = dict(ok=True, mode="positions", account={"id": account_id, "name": account_name},
                      total=len(prepared), created=0, updated=0, unchanged=0, closed=0,
                      buy_trades=0, sell_trades=0, failed=0, removed=0, cash_updated=cash is not None, results=[])
        for index, row in enumerate(prepared):
            stock = _stock(db, row)
            qty, cost = row["quantity"], float(row.get("cost_price") or 0)
            if stock is None and qty:
                stock = Stock(symbol=row["symbol"], name=row.get("name") or row["symbol"], market="CN")
                db.add(stock)
                db.flush()
            position = db.query(Position).filter(Position.account_id == account.id, Position.stock_id == stock.id).first() if stock else None
            old_qty = int(position.quantity or 0) if position else 0
            delta = qty - old_qty
            item = {"row": index + 1, "symbol": row["symbol"], "name": row.get("name"), "status": row["status"]}
            if record_trades and delta:
                price = row["trade_price"]
                source = {"implied_cost": "成本差反推估算", "last_price": "现价估算", "cost": "成本价估算", "manual": "用户核对价"}.get(row.get("price_source"), "估算价")
                trade = wealth.execute_trade(
                    db, account=account, stock=stock, action="buy" if delta > 0 else "sell",
                    quantity=abs(delta), price=price, trade_date=day, source="screenshot",
                    fee_override={"gross": round(abs(delta) * price, 2), "commission": 0.0,
                                  "stamp_duty": 0.0, "transfer_fee": 0.0, "total_fee": 0.0},
                    deduct_cash=not baseline, manual_cost=cost if qty else None,
                    note=f"[截图] 持仓 {old_qty}→{qty}；{source}，费用未计，非券商逐笔成交" + ("；初次持仓建账，不回溯现金" if baseline else ""),
                )
                item["trade_id"] = trade["trade_id"]
                result["buy_trades" if delta > 0 else "sell_trades"] += 1
            elif qty == 0:
                if position:
                    db.delete(position)
            elif position is None:
                db.add(Position(account_id=account.id, stock_id=stock.id, quantity=qty,
                                cost_price=cost, invested_amount=round(qty * cost, 2)))
            else:
                position.quantity = qty
                position.cost_price = cost
                position.invested_amount = round(qty * cost, 2)
            if qty == 0 and old_qty:
                result["closed"] += 1
                result["removed"] += int(bool(row.get("missing")))
            elif qty and not old_qty:
                result["created"] += 1
            elif row["status"] == "unchanged":
                result["unchanged"] += 1
            elif qty:
                result["updated"] += 1
            result["results"].append(item)
            db.flush()
        if changes is not None:
            for item in changes["attach"]:
                stock = _stock(db, item)
                db.add(StockAgent(stock_id=stock.id, agent_name=item["agent_name"], **item["config"]))
            for item in changes["detach"]:
                db.delete(db.get(StockAgent, item["id"]))
            result["agents"] = {"attached": len(changes["attach"]), "detached": len(changes["detach"]),
                                "retained": len(changes["retained"]), "warnings": changes["warnings"]}
        if cash is not None:
            account.available_funds = round(cash, 2)
        db.commit()
        logger.info("截图持仓同步 account=%s buy=%s sell=%s closed=%s agents=%s", account_id,
                    result["buy_trades"], result["sell_trades"], result["closed"], result.get("agents"))
        return result
    except Exception:
        db.rollback()
        raise
