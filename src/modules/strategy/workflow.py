"""Draft → immutable policy → backtest/check evidence. No broker execution here."""
from __future__ import annotations

import hashlib
import json
import math
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.modules.strategy.patterns import FEATURES, VALID_FIELDS
from src.platform.persistence.models import (
    Account, BacktestRun, Position, Stock, StrategyDecision, StrategyDraft,
    StrategyPlaybook, StrategyRevision, TradeRecord,
)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def validate_rules(payload: dict) -> dict:
    """Reject invalid/unknown rules, especially avoid rules: never silently drop them."""
    scope = str(payload.get("scope") or "stock")
    if scope not in ("stock", "global"):
        raise ValueError("scope 必须是 stock 或 global")
    symbol = str(payload.get("symbol") or "").strip().upper() if scope == "stock" else ""
    if scope == "stock" and not symbol:
        raise ValueError("个股策略必须指定 symbol")
    timing = payload.get("timing") or "completed_daily"
    if timing not in ("completed_daily", "intraday_preview"):
        raise ValueError("不支持的规则判断时点")
    out = {
        "schema_version": 1, "scope": scope, "symbol": symbol,
        "market": str(payload.get("market") or "CN").upper(), "timing": timing,
        "name": str(payload.get("name") or "未命名策略")[:200],
        "note": str(payload.get("note") or "")[:8000], "enabled": bool(payload.get("enabled", True)),
    }
    if out["market"] not in ("CN", "HK", "US"):
        raise ValueError("market 必须是 CN/HK/US")
    for side in ("buy", "sell", "avoid"):
        conditions = payload.get(side) or []
        if not isinstance(conditions, list) or len(conditions) > 30:
            raise ValueError(f"{side} 条件必须是最多 30 项的列表")
        from src.modules.strategy.rule_engine import validate_condition
        out[side] = [validate_condition(item, VALID_FIELDS) for item in conditions]
    from src.modules.strategy.template_runtime import normalize_config
    raw_config=dict(payload.get('config') or {})
    if 'tf_' in json.dumps([out['buy'],out['sell'],out['avoid']]):
        from src.modules.strategy.factor_engine import CATALOG
        raw_config.setdefault('factor_version',CATALOG['version'])
    config = normalize_config(raw_config)
    if config:
        out['config'] = config
        out['schema_version'] = 2
    return out


def rules_hash(payload: dict) -> str:
    keys=("schema_version", "market", "timing", "buy", "sell", "avoid") + (("config",) if payload.get('config') else ())
    return digest({key: payload.get(key) for key in keys})


def policy_payload(row: StrategyPlaybook) -> dict:
    return validate_rules({
        "scope": row.scope, "symbol": row.symbol, "market": row.market or "CN",
        "timing": row.timing or "completed_daily", "name": row.name, "note": row.note,
        "enabled": row.enabled, "config": row.policy_config or {}, "buy": row.buy_conditions or [],
        "sell": row.sell_conditions or [], "avoid": row.avoid_conditions or [],
    })


def record_revision(db: Session, row: StrategyPlaybook, draft_id: int | None = None) -> StrategyRevision:
    payload = policy_payload(row)
    current = db.get(StrategyRevision, row.active_revision_id) if row.active_revision_id else None
    if current is not None and current.payload == payload:
        return current
    version = db.query(func.max(StrategyRevision.version)).filter_by(scope=row.scope, symbol=row.symbol, market=row.market).scalar() or 0
    revision = StrategyRevision(scope=row.scope, symbol=row.symbol, market=row.market, version=version + 1, rules_hash=rules_hash(payload), payload=payload, source_draft_id=draft_id)
    db.add(revision)
    db.flush()
    row.active_revision_id = revision.id
    return revision


def revision_dict(row: StrategyRevision) -> dict:
    return {**row.payload, "id": row.id, "revision_id": row.id, "version": row.version, "rules_hash": row.rules_hash, "source_draft_id": row.source_draft_id, "created_at": row.created_at.isoformat() if row.created_at else ""}


def draft_dict(row: StrategyDraft) -> dict:
    return {**row.payload, "id": row.id, "draft_id": row.id, "status": "draft", "archived_at": row.archived_at.isoformat() if row.archived_at else None, "rules_hash": row.rules_hash, "source_text": row.source_text or "", "questions": row.questions or [], "updated_at": row.updated_at.isoformat() if row.updated_at else ""}


def save_draft(db: Session, payload: dict) -> dict:
    rules = validate_rules(payload)
    draft_id = payload.get("draft_id")
    row = db.get(StrategyDraft, draft_id) if draft_id else StrategyDraft()
    if row is None or row.archived_at:
        raise ValueError("草稿不存在或已删除，请先恢复")
    if draft_id and payload.get("expected_hash") and payload["expected_hash"] != row.rules_hash:
        raise ValueError("草稿已变化，请重新核对后保存")
    row.scope, row.symbol, row.market = rules["scope"], rules["symbol"], rules["market"]
    row.payload, row.rules_hash = rules, rules_hash(rules)
    row.source_text = str(payload.get("source_text") or "")[:16000]
    row.questions = [str(q)[:1000] for q in payload.get("questions", []) if str(q).strip()][:20]
    db.add(row)
    db.commit()
    db.refresh(row)
    return draft_dict(row)


def activate_draft(db: Session, draft_id: int) -> dict:
    from src.modules.strategy import playbooks

    row = db.get(StrategyDraft, draft_id)
    if row is None or row.archived_at:
        raise ValueError("草稿不存在或已删除，请先恢复")
    if row.questions:
        raise ValueError("草稿存在待确认问题，请先编辑可执行条件并确认问题")
    return playbooks.save_playbook({**row.payload, "source_draft_id": row.id}, db=db)


async def generate_draft(db: Session, payload: dict) -> dict:
    from src.platform.ai.ai_failover import get_configured_failover_client

    existing = db.get(StrategyDraft, payload["draft_id"]) if payload.get("draft_id") else None
    if payload.get("draft_id") and (existing is None or existing.archived_at):
        raise ValueError("草稿不存在或已删除，请先恢复")
    expected_hash = existing.rules_hash if existing else None
    if payload.get("expected_hash") and expected_hash != payload["expected_hash"]:
        raise ValueError("草稿已变化，请刷新后重新生成")
    db.commit()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("请描述策略想法")
    system = (
        "把用户想法整理成交易策略草稿，不建议买卖，不应用策略。只输出JSON对象："
        '{"name":"名称","buy":[],"sell":[],"avoid":[],"note":"不能执行的背景解释","questions":[]}。'
        "每条条件必须是{field,op,value}；可用{field:'__group__',op:'eq',value:1,logic:'all'或'any',children:[]}表达条件组。offset为历史交易日偏移，consecutive为连续满足交易日。"
        "config.execution支持stop_loss_pct(负百分数)、take_profit_pct(正百分数)、max_hold_days(整数)。比值单位ratio输入0.05表示5%，%单位输入5。"
        "只允许以下字段及数字阈值：" + json.dumps([{k:f[k] for k in ('key','label','unit') if k in f} for f in FEATURES if f['key'] not in ('template_entry','template_exit')], ensure_ascii=False) +
        "；op只允许eq/ne/gt/gte/lt/lte/between/in/not_in。buy全部满足，sell或avoid任一满足。"
        "严格区分近N日涨跌与持仓盈亏。缺少字段或阈值、账户仓位、止损成本、情绪描述等不能执行的语义，"
        "必须逐条加入questions，不能悄悄忽略、近似成别的指标或放进备注就当已实现。备注不是执行规则。"
        "对模糊阈值向用户提问，不擅自发明。不要输出Python或可执行代码。"
    )
    content = await asyncio.wait_for(get_configured_failover_client(db, payload.get("model_id")).chat(system, text[:12000], temperature=0.1), timeout=40)
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
    except (ValueError, TypeError):
        result = {"buy": [], "sell": [], "avoid": [], "questions": ["AI 未返回可验证规则，请手工填写条件或重试。"]}
    questions = result.get("questions") if isinstance(result.get("questions"), list) else ["AI 返回的问题格式异常，请核对所有原始要求。"]
    draft = {**result, "draft_id": payload.get("draft_id"), "expected_hash": expected_hash, "scope": payload.get("scope", "stock"), "symbol": payload.get("symbol", ""), "market": payload.get("market", "CN"), "timing": "completed_daily", "source_text": text, "questions": questions}
    try:
        validate_rules(draft)
    except ValueError as exc:
        draft.update(buy=[], sell=[], avoid=[], questions=questions + [str(exc)])
    db.expire_all()
    return save_draft(db, draft)


def load_quote_snapshots(symbols: list[tuple[str, str]]) -> dict:
    """Keep quote model timestamps instead of losing them in legacy quote DTOs."""
    from src.platform.marketdata.marketdata_client import get_market_data

    out = {}
    for market in {m for _, m in symbols}:
        try:
            rows = get_market_data().quotes(list({s for s, m in symbols if m == market}), market=market)
            for quote in rows:
                timestamp = quote.timestamp
                aware = timestamp.astimezone(timezone.utc) if timestamp else None
                out[f"{market}:{quote.symbol}"] = {
                    "price": quote.current_price,
                    "timestamp": aware.isoformat() if aware else None,
                    "timestamp_kind": "quote_model_collection_time",
                    "fresh": bool(aware and abs((datetime.now(timezone.utc) - aware).total_seconds()) <= 180),
                }
        except Exception:
            continue
    return out


def account_snapshot(db: Session, account_id: int | None, symbol: str, market: str, side: str, price, quantity, quotes: dict | None = None) -> tuple[dict, list[dict]]:
    """Only deterministic checks backed by ledger data; unknown valuation is not PASS."""
    account = db.get(Account, account_id) if account_id else None
    if account is None:
        return {}, [{"field": "account", "status": "UNKNOWN", "reason": "请选择有效账户以检查资金与可卖持仓"}]
    rows = db.query(Position, Stock).join(Stock, Stock.id == Position.stock_id).filter(Position.account_id == account.id).all()
    positions = [{"stock_id": p.stock_id, "symbol": s.symbol, "market": s.market, "quantity": p.quantity, "cost_price": p.cost_price, "updated_at": str(p.updated_at), "stop_loss_price": p.stop_loss_price, "take_profit_price": p.take_profit_price} for p, s in rows]
    quotes = quotes or {}
    from src.modules.portfolio.wealth import risk_policy_for, fee_policy_for
    risk, fee = risk_policy_for(account), fee_policy_for(account)
    snap = {"id": account.id, "available_funds": account.available_funds, "risk_policy": risk, "fee_policy": fee, "updated_at": str(account.updated_at), "positions": positions, "quotes": quotes}
    checks = []
    holding = next((p for p in positions if p["symbol"] == symbol and p["market"] == market), None)
    valid_input = price is not None and quantity is not None and math.isfinite(price) and math.isfinite(quantity) and price > 0 and quantity > 0 and float(quantity).is_integer()
    checks.append({"field": "trade_input", "status": "PASS" if valid_input else "UNKNOWN" if price is None or quantity is None else "BLOCK", "reason": "拟成交价和整数数量有效" if valid_input else "请填写有效拟成交价格，数量必须为正整数"})
    if side == "buy" and valid_input:
        if market == "CN" and symbol.startswith(("000", "001", "002", "003", "600", "601", "603", "605", "300", "301", "688", "689", "15", "51", "56", "58")):
            unit = 200 if symbol.startswith(("688", "689")) else 100
            checks.append({"field": "lot_size", "status": "PASS" if quantity % unit == 0 else "BLOCK", "actual": quantity, "expected": unit, "reason": f"本期按 {unit} 股整数单位保守核查，科创板不模拟零散增量申报"})
        else:
            checks.append({"field": "lot_size", "status": "UNKNOWN", "reason": "当前标的交易单位未明确，须核对证券类型与市场规则"})
    if side == "sell" and valid_input:
        available = float((holding or {}).get("quantity") or 0)
        checks.append({"field": "held_quantity", "status": "PASS" if quantity <= available else "BLOCK", "actual": available, "expected": quantity, "reason": "账面持仓足够" if quantity <= available else "卖出数量超过账面持仓"})
        # Sellable lots are tracked by same-day trades only when this ledger is complete.
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        bought = db.query(func.sum(TradeRecord.quantity)).filter(TradeRecord.account_id == account.id, TradeRecord.stock_symbol == symbol, TradeRecord.action.in_(("buy", "dca")), TradeRecord.trade_date == today).scalar() or 0
        if market == "CN" and bought:
            sellable = max(0, available - float(bought))
            checks.append({"field": "t_plus_one", "status": "PASS" if quantity <= sellable else "BLOCK", "actual": sellable, "reason": "按今日账本检查可卖数量；账本遗漏会影响结论"})
    if side == "buy" and valid_input:
        if market != "CN":
            checks.append({"field": "cash", "status": "UNKNOWN", "reason": "外币成交成本与可用资金未统一口径，须人工核对"})
        else:
            policy = fee
            gross = price * quantity
            required = gross + max(gross * float(policy.get("commission_rate", .00025)), float(policy.get("min_commission", 5))) + gross * float(policy.get("transfer_fee_rate", .00001))
            checks.append({"field": "cash", "status": "PASS" if required <= float(account.available_funds or 0) else "BLOCK", "actual": account.available_funds, "required": round(required, 2), "reason": "按拟成交价估算资金及费用"})
        if risk.get("max_single_weight") is not None:
            needed = positions + [{"symbol": symbol, "market": market, "quantity": 0}]
            usable = all(p["market"] == "CN" and quotes.get(f"CN:{p['symbol']}", {}).get("fresh") and float(quotes.get(f"CN:{p['symbol']}", {}).get("price") or 0) > 0 for p in needed)
            if usable:
                market_value = sum(float(p["quantity"]) * float(quotes[f"CN:{p['symbol']}"]["price"]) for p in positions)
                target_price = float(quotes[f"CN:{symbol}"]["price"])
                holding_value = float((holding or {}).get("quantity") or 0) * target_price
                # Mark all positions with one batch; planned execution affects cash and fees.
                equity_after = float(account.available_funds or 0) - required + market_value + quantity * target_price
                weight = (holding_value + quantity * target_price) / equity_after if equity_after > 0 else float("inf")
                max_weight = float(risk["max_single_weight"])
                checks.append({"field": "max_single_weight", "status": "PASS" if weight <= max_weight else "BLOCK", "actual": round(weight, 6) if math.isfinite(weight) else None, "expected": max_weight, "reason": "按本次批量采集行情和拟成交费用估算买后单仓权重", "equity_after": round(equity_after, 2)})
            else:
                checks.append({"field": "max_single_weight", "status": "UNKNOWN", "reason": "持仓估值缺行情、行情过期或包含未换算外币，不能用成本价代替"})
    return snap, checks


def decision_dict(row: StrategyDecision) -> dict:
    return {**row.result, "id": row.id, "decision_id": row.id, "status": row.status, "revision_id": row.revision_id, "rules_hash": row.rules_hash, "input_hash": row.input_hash, "input": row.input_snapshot, "data_as_of": row.data_as_of, "created_at": row.created_at.isoformat() + "Z" if row.created_at else "", "expires_at": row.expires_at.isoformat() + "Z", "expired": row.expires_at < now(), "exception_reason": row.exception_reason or "", "exception_at": row.exception_at.isoformat() + "Z" if row.exception_at else None, "linked_trade_id": row.linked_trade_id}


async def create_decision(db: Session, payload: dict) -> dict:
    from src.modules.strategy import gate, playbooks

    request = {key: payload.get(key) for key in ("account_id", "symbol", "market", "side", "price", "quantity", "use_ai", "model_id")}
    request.update(symbol=str(request["symbol"] or "").strip().upper(), market=str(request["market"] or "CN").upper(), side=request["side"] or "buy")
    if not request["symbol"] or request["side"] not in ("buy", "sell"):
        raise ValueError("请提供标的与有效买卖方向")
    policy = playbooks.resolve(request["symbol"], db, market=request["market"])
    revision = record_revision(db, policy) if policy else None
    # Legacy policies receive their first immutable revision here. Release this
    # tiny write transaction before quotes/AI, which can take seconds.
    db.commit()
    held = db.query(Position, Stock).join(Stock, Stock.id == Position.stock_id).filter(Position.account_id == request["account_id"]).all() if request["account_id"] else []
    symbols = [(stock.symbol, stock.market) for _, stock in held] + [(request["symbol"], request["market"])]
    quotes = await asyncio.to_thread(load_quote_snapshots, symbols)
    snap, account_checks = account_snapshot(db, request["account_id"], request["symbol"], request["market"], request["side"], request["price"], request["quantity"], quotes)
    result = await gate.evaluate(request["symbol"], side=request["side"], market=request["market"], price=request["price"], quantity=request["quantity"], use_ai=bool(request["use_ai"]), model_id=request["model_id"], db=db, account_context=snap)
    status = result.get("status", "UNKNOWN")
    if revision and result.get("rules_hash") and result["rules_hash"] != revision.rules_hash:
        status = "UNKNOWN"
        result["summary"] = "检查期间策略发生变化，请重新检查"
    target = next((p for p in snap.get("positions", []) if p["symbol"] == request["symbol"] and p["market"] == request["market"]), None)
    policy_exit=False
    execution=(policy.policy_config or {}).get('execution',{}) if policy else {}
    if request['side']=='sell' and execution:
        from src.modules.strategy.rule_engine import execution_check
        cost=target.get('cost_price') if target else None
        # Holding start cannot be inferred from Position.updated_at (it changes on edits/adds).
        quote=quotes.get(f"{request['market']}:{request['symbol']}",{})
        risk_rows=execution_check(execution,quote.get('price') if quote.get('fresh') else None,cost,None)
        result['execution_checks']=risk_rows
        if any(c['ok'] for c in risk_rows) and quote.get('fresh'):
            policy_exit=True
            status='PASS';result.update(mandatory_exit=True,summary='策略风险退出已触发，AI 不能否决必要退出')
        elif any(c['ok'] is None for c in risk_rows) and status!='PASS':status='UNKNOWN'
    live = quotes.get(f"{request['market']}:{request['symbol']}", {})
    mandatory_exit = policy_exit or bool(request["side"] == "sell" and target and target.get("stop_loss_price") and live.get("fresh") and 0 < float(live.get("price") or 0) <= float(target["stop_loss_price"]))
    if mandatory_exit:
        status = "PASS"
        result.update(mandatory_exit=True, risk_exit={"field": "position_stop_loss", "status": "PASS", "actual": live["price"], "expected": target.get("stop_loss_price"), "timing": "live_price", "data_as_of": live.get("timestamp"), "reason": "策略或持仓风险退出已触发；AI 不能否决必要退出"})
        result["summary"] = result["risk_exit"]["reason"]
        account_checks.append(result["risk_exit"])
    if revision is None and not mandatory_exit:
        status = "NO_POLICY"
    elif any(c["status"] == "BLOCK" for c in account_checks):
        status = "BLOCK"
    elif status == "PASS" and any(c["status"] == "UNKNOWN" for c in account_checks):
        status = "UNKNOWN"
    if not snap:
        status = "UNKNOWN"
    evidence = result.get("evidence", []) + account_checks
    result.update(status=status, account_checks=account_checks, evidence=evidence)
    result["verdict"] = {"PASS": "可操作", "BLOCK": "不建议", "UNKNOWN": "无法判断", "NO_POLICY": "无策略"}[status]
    if status != "PASS":
        result["summary"] = result["verdict"] + "；" + (next((c["reason"] for c in account_checks if c["status"] != "PASS"), result.get("summary") or "请查看逐条证据"))
    # Rich input fingerprints include policy, account holdings and exact proposed trade.
    snapshot = {**request, "account": snap, "policy": revision_dict(revision) if revision else None}
    row = StrategyDecision(account_id=request["account_id"], symbol=request["symbol"], market=request["market"], side=request["side"], price=request["price"], quantity=request["quantity"], revision_id=revision.id if revision else None, rules_hash=revision.rules_hash if revision else "", input_hash=digest(snapshot), input_snapshot=snapshot, status=status, result=result, data_as_of=str(result.get("as_of") or ""), expires_at=now() + timedelta(minutes=3))
    db.expire_all()
    try:
        _assert_current_decision(db, row)
    except ValueError as exc:
        row.status = "UNKNOWN"
        row.result = {**result, "status": "UNKNOWN", "verdict": "无法判断", "summary": str(exc)}
    db.add(row)
    db.commit()
    db.refresh(row)
    return decision_dict(row)


def _assert_current_decision(db: Session, row: StrategyDecision) -> None:
    if row.expires_at < now():
        raise ValueError("检查已失效，请重新检查：已超过有效期")
    from src.modules.strategy import playbooks
    current = playbooks.resolve(row.symbol, db, market=row.market)
    if (current.active_revision_id if current else None) != row.revision_id:
        raise ValueError("检查已失效，请重新检查：生效策略已变化")
    account = db.get(Account, row.account_id) if row.account_id else None
    snapshot = (row.input_snapshot or {}).get("account", {})
    from src.modules.portfolio.wealth import risk_policy_for, fee_policy_for
    if row.account_id and (account is None or not account.enabled):
        raise ValueError("检查已失效，请重新检查：账户已删除或停用")
    if account and (str(account.updated_at) != snapshot.get("updated_at") or float(account.available_funds or 0) != float(snapshot.get("available_funds") or 0) or risk_policy_for(account) != snapshot.get("risk_policy") or fee_policy_for(account) != snapshot.get("fee_policy")):
        raise ValueError("检查已失效，请重新检查：账户资金或设置已变化")
    positions = db.query(Position).filter(Position.account_id == row.account_id).all() if row.account_id else []
    actual = {(p.stock_id, p.quantity, p.cost_price, p.stop_loss_price, p.take_profit_price, str(p.updated_at)) for p in positions}
    recorded = {(p["stock_id"], p["quantity"], p["cost_price"], p.get("stop_loss_price"), p.get("take_profit_price"), p["updated_at"]) for p in snapshot.get("positions", [])}
    if actual != recorded:
        raise ValueError("检查已失效，请重新检查：账户持仓已变化")


def add_exception(db: Session, decision_id: int, reason: str) -> dict:
    row = db.get(StrategyDecision, decision_id)
    if row is None:
        raise ValueError("检查记录不存在")
    if not reason.strip() or len(reason.strip()) < 5:
        raise ValueError("请填写至少 5 个字的例外原因")
    _assert_current_decision(db, row)
    row.exception_reason, row.exception_at = reason.strip()[:4000], now()
    db.commit()
    return decision_dict(row)


async def explain_decision(db: Session, decision_id: int, model_id: int | None = None) -> dict:
    """Attach AI to the exact saved evidence, without re-querying prices or creating a check."""
    from src.modules.strategy import gate

    row = db.get(StrategyDecision, decision_id)
    if row is None:
        raise ValueError("检查记录不存在")
    _assert_current_decision(db, row)
    result = dict(row.result or {})
    if (result.get("ai") or {}).get("ok") and result.get("ai_model_id") == model_id and result.get("ai_bound_to_input_hash") == row.input_hash:
        return decision_dict(row)
    policy = row.input_snapshot.get("policy") or {}
    check = {
        **result, "symbol": row.symbol, "name": policy.get("name", row.symbol),
        "note": policy.get("note", ""), "account_context": row.input_snapshot.get("account", {}),
    }
    for group in ("buy", "sell", "avoid"):
        items = result.get(f"{group}_checks") or []
        check[f"{group}_total"] = len(items)
        check[f"{group}_hits"] = sum(item.get("ok") is True for item in items)
    ai = await gate._ai_analyze(check, row.side, row.price, row.quantity, db=db, model_id=model_id)
    # Policy/account can change while waiting for the provider; never approve a stale snapshot.
    db.expire_all()
    _assert_current_decision(db, row)
    result.update(ai=ai, ai_model_id=model_id, ai_evaluated_at=now().isoformat() + "Z", ai_bound_to_input_hash=row.input_hash)
    if row.status == "PASS" and row.side == "buy" and ai.get("tone") in ("谨慎", "反对"):
        result["rule_status"] = "PASS"
        row.status = "BLOCK"
        result["summary"] = "买入规则满足，但 AI 复核提示风险；需人工复核：" + str(ai.get("reason") or ai.get("risk") or ai["tone"])
        result["evidence"] = list(result.get("evidence", [])) + [{"field": "ai_review", "status": "BLOCK", "reason": ai.get("reason") or ai["tone"], "source": "ai_review"}]
    result["status"] = row.status
    result["verdict"] = {"PASS": "可操作", "BLOCK": "不建议", "UNKNOWN": "无法判断", "NO_POLICY": "无策略"}[row.status]
    row.result = result
    db.commit()
    return decision_dict(row)


def link_trade(db: Session, decision_id: int, trade_id: int) -> dict:
    row, trade = db.get(StrategyDecision, decision_id), db.get(TradeRecord, trade_id)
    if row is None or trade is None:
        raise ValueError("检查或成交记录不存在；成交事实不会因此撤销")
    from zoneinfo import ZoneInfo
    decision_day = (row.created_at or now()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if str(trade.trade_date)[:10] != decision_day:
        raise ValueError("成交日期与事前检查日期不同，不把今日检查伪装为历史依据；成交记录保留")
    stock = db.get(Stock, trade.stock_id) if trade.stock_id else None
    if row.account_id != trade.account_id or row.symbol != trade.stock_symbol or (stock and stock.market != row.market) or row.side != ("sell" if trade.action == "sell" else "buy"):
        raise ValueError("成交的账户、标的、市场或方向与检查不一致")
    if row.quantity is None or abs(float(trade.quantity) - row.quantity) > 1e-6:
        raise ValueError("成交数量与检查不同，请保留未关联事实或重新核查")
    if row.price is None or abs(float(trade.price) / row.price - 1) > .01:
        raise ValueError("成交价偏离拟成交价超过 1%，不关联为同一意图")
    if row.linked_trade_id not in (None, trade_id):
        raise ValueError("此检查已关联其他成交")
    if db.query(StrategyDecision.id).filter(StrategyDecision.linked_trade_id == trade_id, StrategyDecision.id != row.id).first():
        raise ValueError("成交已有其他检查关联；不重复改变纪律依据")
    row.linked_trade_id = trade_id
    db.commit()
    return decision_dict(row)
