"""Strategy center views over existing drafts, applied revisions and evidence.

A draft is the reusable library identity. Applying it copies an immutable revision
into a stock/default binding, never moving the original draft or changing history.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.modules.strategy import playbooks, workflow
from src.platform.persistence.models import BacktestRun, StrategyDraft, StrategyPlaybook, StrategyRevision


def library(db: Session, market: str = "ALL", include_deleted: bool = False) -> dict:
    query = db.query(StrategyDraft)
    if market != "ALL":
        query = query.filter(StrategyDraft.market == market)
    drafts = query.order_by(StrategyDraft.updated_at.desc(), StrategyDraft.id.desc()).all()
    active = playbooks.list_playbooks(db)["items"]
    revisions = {row.id: row for row in db.query(StrategyRevision).all()}
    reports = db.query(BacktestRun).filter(BacktestRun.strategy_code.in_(("playbook", "strategy_portfolio"))).order_by(BacktestRun.id.desc()).all()
    items = []
    for draft in drafts:
        bindings = [row for row in active if row.get("revision_id") in revisions and revisions[row["revision_id"]].source_draft_id == draft.id]
        last = next((row for row in reports if (row.input_snapshot or {}).get("draft_id") == draft.id), None)
        tested = bool(last and last.status == "succeeded" and (last.input_snapshot or {}).get("rules_hash") == draft.rules_hash)
        items.append({**workflow.draft_dict(draft), "bindings": bindings, "tested": tested, "last_backtest": report_dict(last) if last else None})
    return {"items": [item for item in items if not item.get("archived_at")], "deleted_items": [item for item in items if item.get("archived_at")] if include_deleted else [], "bindings": active, "fields": playbooks.FEATURES, "presets": playbooks.PRESETS}


def delete_library(db: Session, draft_id: int, expected_hash: str, disable_bindings: bool = False, binding_ids: list[int] | None = None) -> dict:
    """Move a draft to trash, preserving every immutable revision and report."""
    row = db.get(StrategyDraft, draft_id)
    if row is None or row.archived_at:
        raise ValueError("策略不存在或已删除")
    if row.rules_hash != expected_hash:
        raise ValueError("策略已变化，请刷新后再删除")
    bindings = db.query(StrategyPlaybook).join(StrategyRevision, StrategyPlaybook.active_revision_id == StrategyRevision.id).filter(StrategyRevision.source_draft_id == draft_id, StrategyPlaybook.enabled.is_(True)).all()
    if bindings and not disable_bindings:
        raise ValueError("策略正在使用，请确认停用所列范围后再删除")
    if disable_bindings and {binding.id for binding in bindings} != set(binding_ids or []):
        raise ValueError("策略使用范围已变化，请刷新后重新确认")
    for binding in bindings:
        binding.enabled = False
    row.archived_at = workflow.now()
    db.commit()
    return {"deleted": draft_id, "disabled_bindings": [binding.id for binding in bindings], "history_retained": True}


def restore_library(db: Session, draft_id: int) -> dict:
    row = db.get(StrategyDraft, draft_id)
    if row is None or row.archived_at is None:
        raise ValueError("回收站中没有这份策略")
    row.archived_at = None
    db.commit()
    return workflow.draft_dict(row)


def report_dict(row: BacktestRun) -> dict:
    result = row.result or {}
    return {"id": row.id, "status": row.status, "symbol": (row.input_snapshot or {}).get("symbol") or (f"组合 · {len(result.get('universe') or (row.input_snapshot or {}).get('universe') or [])} 个标的" if row.strategy_code == "strategy_portfolio" else ""), "kind": "portfolio" if row.strategy_code == "strategy_portfolio" else "single", "market": row.market, "start": row.start_date, "end": row.end_date, "draft_id": (row.input_snapshot or {}).get("draft_id"), "rules_hash": (row.input_snapshot or {}).get("rules_hash"), "total_return": result.get("total_return"), "max_drawdown": (result.get("metrics") or {}).get("max_drawdown", result.get("max_drawdown")), "error": row.error_message}


def apply_library(db: Session, draft_id: int, payload: dict) -> dict:
    draft = db.get(StrategyDraft, draft_id)
    if not draft or draft.archived_at:
        raise ValueError("策略草稿不存在")
    if draft.rules_hash != payload["expected_hash"]:
        raise ValueError("策略草稿已变化，请重新核对后应用")
    if draft.questions:
        raise ValueError("请先处理策略的待确认问题")
    symbol = str(payload.get("symbol") or "").strip().upper()
    if draft.scope == "stock" and (payload["scope"] != "stock" or symbol != draft.symbol):
        raise ValueError("这是本股草稿；请先复制为通用策略，再应用到其他范围")
    rules = workflow.validate_rules({**draft.payload, "scope": payload["scope"], "symbol": symbol, "enabled": True})
    return playbooks.save_playbook({**rules, "source_draft_id": draft.id}, db)


def scan(db: Session, draft_id: int, symbols: list[dict], expected_hash: str, allow_network: bool = False) -> dict:
    draft = db.get(StrategyDraft, draft_id)
    if not draft or draft.archived_at or draft.rules_hash != expected_hash:
        raise ValueError("策略草稿不存在或已变化，请重新选择")
    if draft.questions:
        raise ValueError("请先确认草稿问题，再检查候选")
    rules, fingerprint = dict(draft.payload), draft.rules_hash
    # Release read transaction before potentially slow market-data collection.
    db.commit()
    items, seen = [], set()
    for target in symbols:
        symbol, market = target["symbol"].strip().upper(), target.get("market", "CN").upper()
        if (symbol, market) in seen:
            continue
        seen.add((symbol, market))
        if market != rules["market"] or market != "CN" or (draft.scope == "stock" and symbol != draft.symbol):
            items.append({**target, "status": "UNKNOWN", "summary": "市场或个股适用范围不符；本期候选规则检查支持 A 股"})
            continue
        try:
            checked = playbooks.check_symbol(symbol, db, market=market, allow_network=allow_network, rules_override=rules)
            conditions = checked.get("buy_checks", []) + checked.get("avoid_checks", [])
            if checked.get("error") or checked.get("data_stale") or checked.get("timing") == "intraday_preview":
                status, summary = "UNKNOWN", checked.get("error") or "数据不足、过期或为盘中预览，不能确认入场"
            elif checked.get("avoid_hits", 0):
                status, summary = "BLOCK", "命中禁止买入条件"
            elif any(row.get("ok") is None for row in conditions):
                status, summary = "UNKNOWN", "部分条件所需数据缺失，不能确认入场"
            elif checked.get("buy_total", 0) and checked.get("buy_hits") == checked.get("buy_total"):
                status, summary = "MATCH", "符合所选策略入场条件；具体操作仍须检查账户与价量"
            else:
                status, summary = "WAIT", "入场条件尚未满足" if checked.get("buy_total", 0) else "此策略不产生买入信号"
            items.append({**checked, **target, "status": status, "summary": summary})
        except Exception:
            items.append({**target, "status": "UNKNOWN", "summary": "行情读取失败，请稍后重试"})
    db.expire_all()
    current = db.get(StrategyDraft, draft_id)
    if current is None or current.archived_at or current.rules_hash != fingerprint:
        raise ValueError("扫描期间草稿已变化，请重新检查候选")
    return {"draft_id": draft_id, "rules_hash": fingerprint, "items": items, "count": len(items), "as_of": workflow.now().isoformat() + "Z"}
