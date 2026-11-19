"""交易策略卡:全局默认 + 个股覆盖,以及买入/卖出前检查。

条件格式与选股一致(``{field, op, value}``),字段来自
:data:`src.modules.strategy.patterns.FEATURES`。判定规则:

- 买入条件为「且」:全部满足才算符合买入;
- 卖出条件为「或」:任意一条命中即符合卖出;
- 禁买条件为「或」:任意一条命中即「禁止买入」(高开低走未修复等风控);
- 都不满足时,按买入条件命中比例给出「接近买入」(≥2/3)或「观察」。
"""

from __future__ import annotations

import logging
import math
from datetime import date

from sqlalchemy.orm import Session

from src.modules.strategy.backtest.single import Bar, _match, load_bars
from src.modules.strategy.patterns import (
    FEATURES,
    VALID_FIELDS,
    compute_features,
    describe_condition,
    diagnose,
)
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import StrategyPlaybook

logger = logging.getLogger(__name__)

LABELS = {f["key"]: f["label"] for f in FEATURES}
UNITS = {f["key"]: f["unit"] for f in FEATURES}
GROUPS = {f["key"]: f["group"] for f in FEATURES}

DEFAULT_BUY = [{"field": "runup_3d", "op": "between", "value": [-5, 0]}]
DEFAULT_SELL = [{"field": "profit_ratio", "op": "gte", "value": 95}]
DEFAULT_AVOID: list[dict] = []

# ── 策略模板 ───────────────────────────────────────────────────────────────
# 一键套用;条件与手动编辑完全同构,套用后仍可改。
PRESETS: list[dict] = [
    {
        "id": "three_day_dip",
        "name": "三天小跌抄底",
        "description": "小跌三天后抄底，等多数人赚钱时撤退",
        "note": "三天累计跌幅不到 5% 说明抛压释放但没崩盘，视为短期跌到位；获利盘 95% 时离场。",
        "buy": [
            {"field": "runup_3d", "op": "between", "value": [-5, 0]},
            {"field": "runup_20d", "op": "gte", "value": -15},
        ],
        "sell": [{"field": "profit_ratio", "op": "gte", "value": 95}],
        "avoid": [],
    },
    {
        "id": "gap_drop_guard",
        "name": "高开低走不追高",
        "description": "放量高开低走后没修复就不进场，等收复事件日开盘价再说",
        "note": "涨了一段后突然放量高开低走，第二天起只弱修复＝筹码松动，追高胜率低。",
        "buy": [
            {"field": "runup_3d", "op": "between", "value": [-6, 2]},
            {"field": "ma5_dev", "op": "gte", "value": -6},
            {"field": "volume_ratio_5d", "op": "lte", "value": 1.2},
        ],
        "sell": [
            {"field": "change_pct", "op": "lte", "value": -5},
            {"field": "ma5_dev", "op": "lte", "value": -6},
            {"field": "profit_ratio", "op": "gte", "value": 90},
        ],
        "avoid": [
            {"field": "gap_drop_days", "op": "gte", "value": 1},
            {"field": "gap_drop_flag", "op": "eq", "value": 1},
        ],
    },
    {
        "id": "ma5_pullback",
        "name": "5日线回踩",
        "description": "趋势没坏、缩量回踩 5 日线附近时低吸",
        "note": "20 日线上方才做，跌破 5 日线 3% 以内、缩量为宜；放量高开低走后的下跌不接。",
        "buy": [
            {"field": "ma5_dev", "op": "between", "value": [-3, 0.5]},
            {"field": "ma20_dev", "op": "gte", "value": 0},
            {"field": "volume_ratio_5d", "op": "lte", "value": 1.3},
        ],
        "sell": [
            {"field": "change_pct", "op": "lte", "value": -4},
            {"field": "profit_ratio", "op": "gte", "value": 90},
        ],
        "avoid": [{"field": "gap_drop_days", "op": "gte", "value": 1}],
    },
    {
        "id": "volume_breakout",
        "name": "放量突破20日新高",
        "description": "放量站上 20 日新高附近时跟随，破 5 日线止盈",
        "note": "要求量能至少 1.5 倍；大幅高开（>5%）不追，避免当天冲高回落。",
        "buy": [
            {"field": "high20_gap", "op": "between", "value": [-1, 2]},
            {"field": "volume_ratio_5d", "op": "gte", "value": 1.5},
        ],
        "sell": [{"field": "ma5_dev", "op": "lte", "value": -3}],
        "avoid": [{"field": "open_gap", "op": "gte", "value": 5}],
    },
    {
        "id": "hold_exit",
        "name": "持仓风控：走弱就撤",
        "description": "不设买入，只监控持仓该不该走（放量高开低走、收盘位置低、连续破线）",
        "note": "适合已经持有的票：任意一条命中就当减仓/离场信号看，不要求全满足。",
        "buy": [],
        "sell": [
            {"field": "gap_drop_flag", "op": "eq", "value": 1},
            {"field": "close_pos", "op": "lte", "value": 20},
            {"field": "change_pct", "op": "lte", "value": -4},
            {"field": "ma5_below_days", "op": "gte", "value": 3},
        ],
        "avoid": [],
    },
]


from src.modules.strategy.template_runtime import presets as tick_presets
PRESETS += tick_presets()


def _clean(conditions: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for c in conditions or []:
        if not isinstance(c, dict):
            continue
        if 'children' in c:
            out.append(c)
            continue
        field = str(c.get("field") or "").strip()
        if field not in VALID_FIELDS:
            continue
        op = str(c.get("op") or "eq").strip()
        out.append({**c, "field": field, "op": op, "value": c.get("value")})
    return out


def _row(row: StrategyPlaybook, has_global: bool) -> dict:
    from src.modules.strategy.workflow import policy_payload, rules_hash
    return {
        "id": row.id,
        "scope": row.scope,
        "symbol": row.symbol or "",
        "name": row.name or "",
        "buy": row.buy_conditions or [],
        "sell": row.sell_conditions or [],
        "avoid": row.avoid_conditions or [],
        "note": row.note or "",
        "enabled": bool(row.enabled),
        "is_override": row.scope == "stock" and has_global,
        "market": row.market or "CN",
        "timing": row.timing or "completed_daily",
        "config": row.policy_config or {},
        "revision_id": row.active_revision_id,
        "rules_hash": rules_hash(policy_payload(row)),
    }


def list_playbooks(db: Session | None = None) -> dict:
    from src.platform.persistence.models import StrategyRevision
    own = db is None
    db = db or SessionLocal()
    try:
        rows = (
            db.query(StrategyPlaybook)
            .order_by(StrategyPlaybook.scope.asc(), StrategyPlaybook.id.asc())
            .all()
        )
        has_global = any(r.scope == "global" for r in rows)
        revisions = dict(db.query(StrategyRevision.id, StrategyRevision.version).filter(StrategyRevision.id.in_([row.active_revision_id for row in rows if row.active_revision_id])).all())
        return {
            "items": [{**_row(r, has_global), **({"version": revisions[r.active_revision_id]} if r.active_revision_id in revisions else {})} for r in rows],
            "fields": FEATURES,
            "presets": PRESETS,
            "defaults": {"buy": DEFAULT_BUY, "sell": DEFAULT_SELL, "avoid": DEFAULT_AVOID},
        }
    finally:
        if own:
            db.close()


def presets() -> dict:
    """策略模板目录(含字段目录)。"""
    return {"presets": PRESETS, "fields": FEATURES}


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
    if row.id and (row.market or "CN") != (payload.get("market") or "CN"):
        raise ValueError("同一标的的市场不能直接修改，请使用正确的市场标识")
    row.market = payload.get("market") or "CN"
    row.timing = payload.get("timing") or "completed_daily"
    row.policy_config = payload.get('config') or {}
    row.buy_conditions = _clean(payload.get("buy"))
    row.sell_conditions = _clean(payload.get("sell"))
    row.avoid_conditions = _clean(payload.get("avoid"))
    row.note = str(payload.get("note") or "")
    row.enabled = bool(payload.get("enabled", True))
    db.flush()
    return row


def save_playbook(payload: dict, db: Session | None = None) -> dict:
    from src.modules.strategy.workflow import validate_rules, record_revision
    own = db is None
    db = db or SessionLocal()
    try:
        source_draft_id = payload.get("source_draft_id")
        payload = validate_rules(payload)
        scope = payload["scope"]
        symbol = payload["symbol"]
        if scope == "stock" and not symbol:
            raise ValueError("个股策略必须指定 symbol")
        row = _upsert(db, scope=scope, symbol=symbol, payload=payload)
        revision = record_revision(db, row, source_draft_id)
        db.commit()
        db.refresh(row)
        has_global = (
            db.query(StrategyPlaybook).filter(StrategyPlaybook.scope == "global").count() > 0
        )
        return {**_row(row, has_global), "version": revision.version}
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


def resolve(symbol: str, db: Session, market: str = "CN") -> StrategyPlaybook | None:
    """个股策略优先,其次全局;都关闭时返回 None。"""
    stock_row = (
        db.query(StrategyPlaybook)
        .filter(StrategyPlaybook.scope == "stock", StrategyPlaybook.symbol == symbol)
        .first()
    )
    if stock_row is not None and stock_row.enabled and (stock_row.market or "CN") == market:
        return stock_row
    global_row = (
        db.query(StrategyPlaybook).filter(StrategyPlaybook.scope == "global").first()
    )
    if global_row is not None and global_row.enabled and (global_row.market or "CN") == market:
        return global_row
    return None


def _current_bars(
    symbol: str, *, allow_network: bool = True, market: str = "CN"
) -> list[Bar]:
    """日K + 今日实时快照(若当日 K 还没入库)。"""
    bars = load_bars(symbol, days=520, allow_network=allow_network, market=market)
    if not bars or not allow_network:
        return bars
    try:
        from src.platform.marketdata.marketdata_client import md_quote_rows

        rows = md_quote_rows([symbol], market)
        quote = rows[0] if rows else None
        if not quote:
            return bars
        price = float(quote.get("current_price") or 0)
        today = date.today().isoformat()
        last = bars[-1]
        if price <= 0 or last.date >= today:
            return bars
        q_open = float(quote.get("open_price") or 0)
        q_high = float(quote.get("high_price") or 0)
        q_low = float(quote.get("low_price") or 0)
        q_prev = float(quote.get("prev_close") or 0)
        # 只有「昨收 == 最后一根K线收盘」才说明这是新一个交易日的行情;
        # 周末/盘后再取到的仍是最后一根K线的旧快照,不能补成新的一根。
        fresh_session = q_prev <= 0 or abs(q_prev - last.close) <= 0.005
        duplicate = (
            abs(price - last.close) <= 1e-6
            and q_open > 0
            and abs(q_open - last.open) <= 1e-6
            and q_high > 0
            and abs(q_high - last.high) <= 1e-6
            and q_low > 0
            and abs(q_low - last.low) <= 1e-6
        )
        if fresh_session and not duplicate and q_open > 0 and q_high >= q_low > 0:
            bars.append(
                Bar(
                    date=today,
                    open=q_open,
                    high=q_high,
                    low=q_low,
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
    if field == "gap_drop_flag":
        return "是" if value else "否"
    unit = UNITS.get(field, "")
    try:
        if unit == "%":
            return f"{float(value):+.2f}%"
        if unit == "倍":
            return f"{float(value):.2f} 倍"
        if unit == "天":
            return f"{int(round(float(value)))} 天"
    except (TypeError, ValueError):
        return str(value)
    return f"{value}"


def _expect(cond: dict) -> str:
    """条件 -> 「≥ 95%」这种紧凑写法(标签在另一列展示)。"""
    op = cond.get("op")
    value = cond.get("value")
    field = cond.get("field", "")
    unit = UNITS.get(field, "")
    if field == "gap_drop_flag":
        return "= 是" if str(op) == "eq" and int(float(value or 0)) == 1 else describe_condition(cond)
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


def _label_title(scope: str, name: str) -> str:
    if name:
        return name
    return "个股策略" if scope == "stock" else "全局策略"


def check_symbol(
    symbol: str,
    db: Session | None = None,
    *,
    allow_network: bool = True,
    market: str = "CN",
    rules_override: dict | None = None,
) -> dict:
    """当前时点的策略检查:返回逐条对照、形态诊断与结论。

    没配策略也会跑形态诊断(买卖前体检和策略页都要用),只是 ``has_playbook=False``。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        playbook = resolve(symbol, db, market=market)
        timing = (playbook.timing if playbook else None) or "completed_daily"
        revision_id = playbook.active_revision_id if playbook else None
        from src.modules.strategy.workflow import policy_payload, rules_hash
        fingerprint = rules_hash(policy_payload(playbook)) if playbook else ""
        if playbook is None:
            buy, sell, avoid = [], [], []
            name, scope, note = "", "", ""
        else:
            buy = _clean(playbook.buy_conditions)
            sell = _clean(playbook.sell_conditions)
            avoid = _clean(playbook.avoid_conditions)
            name = playbook.name or ""
            scope = playbook.scope
            note = playbook.note or ""
    finally:
        if own:
            db.close()

    has_playbook = bool(scope)
    config = playbook.policy_config or {} if playbook else {}
    if rules_override is not None:
        from src.modules.strategy.workflow import validate_rules
        policy = validate_rules(rules_override)
        buy, sell, avoid = policy["buy"], policy["sell"], policy["avoid"]
        name, note, scope = policy["name"], policy["note"], policy["scope"]
        timing, fingerprint, revision_id = policy["timing"], rules_hash(policy), None
        has_playbook = True
        config = policy.get('config',{})
    bars = _current_bars(symbol, allow_network=allow_network, market=market)
    if timing == "completed_daily":
        # Today's OHLC/volume is provisional, even if a provider already stored it.
        bars = [bar for bar in bars if bar.date < date.today().isoformat()]
    if len(bars) < 60:
        return {
            "symbol": symbol,
            "name": _label_title(scope, name) if has_playbook else "未配置策略",
            "scope": scope,
            "note": note,
            "verdict": "数据不足" if has_playbook else "未配置策略",
            "checks": [],
            "has_playbook": has_playbook,
            "buy_hits": 0,
            "buy_total": 0,
            "sell_hits": 0,
            "sell_total": 0,
            "avoid_hits": 0,
            "avoid_total": 0,
            "buy_checks": [],
            "sell_checks": [],
            "avoid_checks": [],
            "pattern": {"tags": [], "summary": "日K不足 60 根，无法判断形态", "risk_hint": ""},
            "error": "历史数据不足(需至少 60 根日K)",
        }

    from src.modules.strategy.rule_engine import evaluate
    from src.modules.strategy.factor_engine import field_rows
    from src.modules.strategy.template_runtime import attach
    rows=[compute_features(bars,i,False) for i in range(len(bars))]
    if config or any('tf_' in str(c) for c in buy+sell+avoid):
        for row,values in zip(rows,field_rows(bars)):
            for key,value in values.items():
                if key.startswith('tf_'):row[key]=value
        try:rows=attach(rows,bars,config,symbol)
        except ValueError as exc:
            return {'symbol':symbol,'has_playbook':True,'verdict':'数据不足','error':str(exc),'buy_checks':[],'sell_checks':[],'avoid_checks':[]}
    features = rows[-1]
    pattern = diagnose(bars, len(bars) - 1, features)

    def build(conds: list[dict]) -> list[dict]:
        out = []
        for c in conds:
            ok=evaluate(rows,len(rows)-1,c)
            at=len(rows)-1-c.get('offset',0)
            value = rows[at].get(c['field']) if at>=0 else None
            if c.get('children'):value=int(ok) if ok is not None else None
            known=ok is not None
            out.append(
                {
                    "field": c["field"],
                    "label": LABELS.get(c["field"], c["field"]),
                    "group": GROUPS.get(c["field"], ""),
                    "op": c.get("op"),
                    "expect": _expect(c),
                    "actual": _fmt(value, c["field"]),
                    "actual_value": value,
                    "known": known,
                    "ok": ok,
                    "text": describe_condition(c),
                }
            )
        return out

    buy_checks = build(buy)
    sell_checks = build(sell)
    avoid_checks = build(avoid)
    buy_hits = sum(1 for c in buy_checks if c["ok"])
    sell_hits = sum(1 for c in sell_checks if c["ok"])
    avoid_hits = sum(1 for c in avoid_checks if c["ok"])
    sell_hit = sell_checks and sell_hits > 0
    avoid_hit = avoid_checks and avoid_hits > 0

    if not has_playbook:
        verdict = "未配置策略"
    elif sell_hit:
        verdict = "符合卖出"
    elif avoid_hit:
        verdict = "禁止买入"
    elif buy_checks and buy_hits == len(buy_checks):
        verdict = "符合买入"
    elif buy_checks and buy_hits / len(buy_checks) >= 2 / 3:
        verdict = "接近买入"
    else:
        verdict = "观察"

    return {
        "symbol": symbol,
        "name": _label_title(scope, name) if has_playbook else "未配置策略",
        "scope": scope,
        "note": note,
        "verdict": verdict,
        "buy_hits": buy_hits,
        "buy_total": len(buy_checks),
        "sell_hits": sell_hits,
        "sell_total": len(sell_checks),
        "avoid_hits": avoid_hits,
        "avoid_total": len(avoid_checks),
        "buy_checks": buy_checks,
        "sell_checks": sell_checks,
        "avoid_checks": avoid_checks,
        "pattern": pattern,
        "price": round(bars[-1].close, 3),
        "as_of": bars[-1].date,
        "estimated": bars[-1].date == date.today().isoformat(),
        "has_playbook": has_playbook,
        "timing": timing,
        "revision_id": revision_id,
        "rules_hash": fingerprint,
        "features": features,
        "data_stale": (date.today() - date.fromisoformat(bars[-1].date)).days > 7,
    }


def board(symbols: list[str], db: Session | None = None) -> dict:
    """批量检查(首页卡片 / 列表徽标用)。不触发联网补数,避免拖慢首页。"""
    out = []
    for symbol in symbols:
        try:
            out.append(check_symbol(symbol, db=db, allow_network=False))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[策略] {symbol} 检查失败: {exc}")
    return {"items": out}
