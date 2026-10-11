import asyncio
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from src.platform.runtime.config import Settings
from src.modules.market import price_alert_service
from src.modules.market.price_alert_engine import ENGINE
from src.platform.persistence.database import get_db
from src.platform.persistence.models import PriceAlertHit, PriceAlertRule, Stock

logger = logging.getLogger(__name__)
router = APIRouter()


def _format_datetime(dt) -> str:
    """格式化时间为当前时区的 ISO 格式（naive datetime 视为 UTC）。"""
    if not dt:
        return ""
    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tzinfo).isoformat(timespec="seconds")


class AlertConditionItem(BaseModel):
    type: str = Field(..., description="price/change_pct/turnover/volume/volume_ratio")
    op: str = Field(..., description=">=/<=/>/</==/between")
    value: float | list[float] = Field(..., description="阈值")


class AlertConditionGroup(BaseModel):
    op: str = Field(default="and", description="and/or")
    items: list[AlertConditionItem] = Field(default_factory=list)


class PriceAlertCreate(BaseModel):
    stock_id: int
    name: str = ""
    enabled: bool = True
    condition_group: AlertConditionGroup
    market_hours_mode: str = "trading_only"
    cooldown_minutes: int = 30
    max_triggers_per_day: int = 3
    repeat_mode: str = "repeat"
    expire_at: str | None = None
    notify_channel_ids: list[int] = []


class PriceAlertUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    condition_group: AlertConditionGroup | None = None
    market_hours_mode: str | None = None
    cooldown_minutes: int | None = None
    max_triggers_per_day: int | None = None
    repeat_mode: str | None = None
    expire_at: str | None = None
    notify_channel_ids: list[int] | None = None


class ToggleBody(BaseModel):
    enabled: bool


def _to_response(rule: PriceAlertRule) -> dict:
    stock = rule.stock
    return {
        "id": rule.id,
        "stock_id": rule.stock_id,
        "stock_symbol": stock.symbol if stock else "",
        "stock_name": stock.name if stock else "",
        "market": stock.market if stock else "",
        "name": rule.name,
        "enabled": rule.enabled,
        "condition_group": rule.condition_group or {},
        "market_hours_mode": rule.market_hours_mode,
        "cooldown_minutes": rule.cooldown_minutes,
        "max_triggers_per_day": rule.max_triggers_per_day,
        "repeat_mode": rule.repeat_mode,
        "expire_at": _format_datetime(rule.expire_at) or None,
        "notify_channel_ids": rule.notify_channel_ids or [],
        "last_trigger_at": _format_datetime(rule.last_trigger_at) or None,
        "last_trigger_price": rule.last_trigger_price,
        "trigger_count_today": rule.trigger_count_today or 0,
        "trigger_date": rule.trigger_date or "",
        "created_at": _format_datetime(rule.created_at),
        "updated_at": _format_datetime(rule.updated_at),
    }


@router.get("")
def list_alert_rules(db: Session = Depends(get_db)):
    rows = price_alert_service.list_alert_rules(db, limit=None)
    return [_to_response(r) for r in rows]


@router.post("")
def create_alert_rule(body: PriceAlertCreate, db: Session = Depends(get_db)):
    try:
        row = price_alert_service.create_alert_rule(
            db,
            stock_id=body.stock_id,
            name=body.name,
            enabled=body.enabled,
            condition_group=body.condition_group.model_dump(),
            market_hours_mode=body.market_hours_mode,
            cooldown_minutes=body.cooldown_minutes,
            max_triggers_per_day=body.max_triggers_per_day,
            repeat_mode=body.repeat_mode,
            expire_at=body.expire_at,
            notify_channel_ids=body.notify_channel_ids,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _to_response(row)


@router.put("/{rule_id}")
def update_alert_rule(rule_id: int, body: PriceAlertUpdate, db: Session = Depends(get_db)):
    updates = body.model_dump(exclude_unset=True)
    try:
        row = price_alert_service.update_alert_rule(db, rule_id, updates)
    except LookupError as exc:
        raise HTTPException(404, "规则不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _to_response(row)


@router.post("/{rule_id}/toggle")
def toggle_alert_rule(rule_id: int, body: ToggleBody, db: Session = Depends(get_db)):
    row = db.query(PriceAlertRule).filter(PriceAlertRule.id == rule_id).first()
    if not row:
        raise HTTPException(404, "规则不存在")
    row.enabled = bool(body.enabled)
    db.commit()
    db.refresh(row)
    return _to_response(row)


@router.delete("/{rule_id}")
def delete_alert_rule(rule_id: int, db: Session = Depends(get_db)):
    try:
        price_alert_service.delete_alert_rule(db, rule_id)
    except LookupError as exc:
        raise HTTPException(404, "规则不存在") from exc
    return {"ok": True}


class BulkDeleteBody(BaseModel):
    scope: str = Field(
        "expired",
        description="all=全部 / expired=只删已过期 / disabled=只删已停用",
    )
    source: str | None = Field(
        None, description="可选，只删某个来源（如 operation_plan）；None 表示不限"
    )


@router.post("/bulk-delete")
def bulk_delete_alert_rules(body: BulkDeleteBody, db: Session = Depends(get_db)):
    """批量删除提醒规则。**不可恢复**，前端必须二次确认并展示条数。

    默认 scope=expired（而不是 all）—— 默认值决定了误点的后果，过期规则本就不会触发，
    删错了损失最小；真要清空必须显式传 all。
    """
    scope = (body.scope or "").strip().lower()
    if scope not in ("all", "expired", "disabled"):
        raise HTTPException(400, f"不支持的 scope: {body.scope}")

    query = db.query(PriceAlertRule)
    if body.source:
        query = query.filter(PriceAlertRule.source == body.source.strip())
    if scope == "expired":
        # expire_at 以 naive UTC 存库（见 price_alert_engine._can_trigger 的同款处理）
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        query = query.filter(
            PriceAlertRule.expire_at.isnot(None), PriceAlertRule.expire_at < now
        )
    elif scope == "disabled":
        query = query.filter(PriceAlertRule.enabled == False)  # noqa: E712

    targets = query.all()
    ids = [r.id for r in targets]
    if not ids:
        return {"ok": True, "deleted": 0, "scope": scope}
    # 同步清掉这些规则的命中记录，否则 alert-hits 会留下孤儿行。
    db.query(PriceAlertHit).filter(PriceAlertHit.rule_id.in_(ids)).delete(
        synchronize_session=False
    )
    for row in targets:
        db.delete(row)
    db.commit()
    logger.info("[价格提醒] 批量删除 %d 条 (scope=%s source=%s)", len(ids), scope, body.source or "-")
    return {"ok": True, "deleted": len(ids), "scope": scope}


@router.get("/hits/today")
def list_today_hits(limit: int = 50, db: Session = Depends(get_db)):
    """今日(本地时区)全部命中,跨规则聚合 —— 供首页"今日要紧事"。"""
    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc
    local_midnight = datetime.now(tzinfo).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)

    hits = (
        db.query(PriceAlertHit)
        .filter(PriceAlertHit.trigger_time >= start_utc)
        .order_by(PriceAlertHit.trigger_time.desc(), PriceAlertHit.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    if not hits:
        return []
    rule_map = {r.id: r for r in db.query(PriceAlertRule).all()}
    stock_ids = {h.stock_id for h in hits}
    stock_map = {s.id: s for s in db.query(Stock).filter(Stock.id.in_(stock_ids)).all()}
    out = []
    for h in hits:
        stock = stock_map.get(h.stock_id)
        rule = rule_map.get(h.rule_id)
        out.append(
            {
                "rule_id": h.rule_id,
                "rule_name": (rule.name if rule else "") or "提醒",
                "symbol": stock.symbol if stock else "",
                "name": stock.name if stock else "",
                "market": stock.market if stock else "CN",
                "trigger_time": _format_datetime(h.trigger_time),
                "snapshot": h.trigger_snapshot or {},
            }
        )
    return out


@router.get("/{rule_id}/hits")
def list_alert_hits(rule_id: int, limit: int = 50, db: Session = Depends(get_db)):
    _ = db.query(PriceAlertRule).filter(PriceAlertRule.id == rule_id).first()
    rows = (
        db.query(PriceAlertHit)
        .filter(PriceAlertHit.rule_id == rule_id)
        .order_by(PriceAlertHit.trigger_time.desc(), PriceAlertHit.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return [
        {
            "id": r.id,
            "rule_id": r.rule_id,
            "stock_id": r.stock_id,
            "trigger_time": _format_datetime(r.trigger_time),
            "trigger_snapshot": r.trigger_snapshot or {},
            "notify_success": bool(r.notify_success),
            "notify_error": r.notify_error or "",
        }
        for r in rows
    ]


@router.post("/{rule_id}/test")
async def test_alert_rule(rule_id: int):
    result = await ENGINE.scan_once(
        only_rule_id=rule_id, dry_run=True, bypass_market_hours=True
    )
    return result


@router.post("/scan")
async def scan_alert_rules(dry_run: bool = False, bypass_market_hours: bool = True):
    try:
        from server import price_alert_scheduler

        if price_alert_scheduler:
            # 手动扫描默认绕过交易时段门禁，便于即时验证规则
            if bypass_market_hours:
                return await price_alert_scheduler.trigger_once(dry_run=dry_run)
            return await ENGINE.scan_once(dry_run=dry_run, bypass_market_hours=False)
    except Exception:
        pass
    return await ENGINE.scan_once(
        dry_run=dry_run, bypass_market_hours=bypass_market_hours
    )


# ── 从「每日操作计划」一键加入提醒 ──────────────────────────────────────
#
# 设计要点:
# - 止损价与目标价的语义不同,校验策略也不同。止损"已经被跌破"是允许的 —— 那正是
#   最需要被叫醒的时刻;而目标价"已经达到"再加提醒只会产生噪声,故直接拒绝。
# - 计划里的价位是 AI 给的,可能在生成后价格已经走远;创建前拿现价核一次,把
#   "已失效/已达成"的项挡在门外,避免用户一键加完发现全是立刻触发的废提醒。
# - expire_at 默认取计划当天收盘,避免昨天的计划今天还在提醒。

_PLAN_LEVEL_META: dict[str, dict] = {
    "stop_loss": {"label": "止损", "op": "<="},
    "target1": {"label": "第一目标", "op": ">="},
    "target2": {"label": "第二目标", "op": ">="},
    "entry": {"label": "买入区间", "op": "between"},
}


class FromPlanItem(BaseModel):
    scenario_index: int = Field(..., ge=0, description="情景下标(从 0 开始)")
    level: str = Field(..., description="stop_loss / target1 / target2 / entry")


class PriceAlertFromPlan(BaseModel):
    plan_id: int
    items: list[FromPlanItem] = Field(default_factory=list)
    cooldown_minutes: int = 30
    max_triggers_per_day: int = 3
    notify_channel_ids: list[int] = Field(default_factory=list)
    expire_at: str = Field(default="", description="留空=计划当天 23:59")


def _scenario_tag(index: int, title: str) -> str:
    """情景的短标识,用于提醒名称。

    同一个价位常在不同情景里以不同身份出现(如 8.25 既是情景一的目标卖出价、又是
    情景二的买入区间下沿),只写价位会让提醒列表里出现一堆看起来重复的条目。带上
    情景标识才能分辨"这条到底是让我卖还是让我买"。

    优先用模型自己写的情景序号(标题多以"情景一：…"开头),取不到再退回下标。
    """
    match = re.match(r"^\s*(情景\s*[0-9一二三四五六七八九十百]+)", str(title or ""))
    if match:
        return re.sub(r"\s+", "", match.group(1))
    return f"情景{index + 1}"


def _condition_already_true(condition: dict, current_price: float | None) -> bool:
    """该条件的判定当前是否已经成立。

    提醒引擎是"状态判定"而非"穿越判定"(见 price_alert_engine._eval_condition),
    所以条件一成立,下一轮扫描(60s 内)就会触发。这对某些情景是对的(价格已经跌穿
    止损,正需要被叫醒),对另一些是误导(如"突破后回踩"的移动止损 8.25,在股价还
    没突破 8.25 时条件已然成立)。这里只做标注,交由用户判断,不擅自拒绝。
    """
    if current_price is None or not isinstance(condition, dict):
        return False
    try:
        item = (condition.get("items") or [{}])[0]
        op, value = item.get("op"), item.get("value")
        price = float(current_price)
        if op == "<=":
            return price <= float(value)
        if op == ">=":
            return price >= float(value)
        if op in ("between", "in"):
            lo, hi = float(value[0]), float(value[1])
            return lo <= price <= hi
    except (TypeError, ValueError, IndexError, KeyError):
        return False
    return False


def _plan_level_condition(level: str, scenario: dict) -> tuple[dict | None, str, str]:
    """把情景里的某个价位转成提醒条件。

    返回 (condition_group, 展示用数值描述, 拒绝原因)。拒绝原因非空表示该项不可创建。
    """
    if level == "entry":
        low = float(scenario.get("entry_low") or 0)
        high = float(scenario.get("entry_high") or 0)
        if low <= 0 or high <= 0:
            return None, "", "该情景未给出买入区间"
        return (
            {"op": "and", "items": [{"type": "price", "op": "between", "value": [low, high]}]},
            f"{low:g}-{high:g}",
            "",
        )

    if level == "stop_loss":
        price = float(scenario.get("stop_loss_num") or 0)
        if price <= 0:
            return None, "", "该情景未给出止损价"
        return (
            {"op": "and", "items": [{"type": "price", "op": "<=", "value": price}]},
            f"{price:g}",
            "",
        )

    # target1 / target2:区间取下沿作为"进入目标区"的判定价
    lo = float(scenario.get(f"{level}_low") or 0)
    hi = float(scenario.get(f"{level}_high") or 0)
    price = lo or hi
    if price <= 0:
        return None, "", "该情景未给出该目标价"
    desc = f"{lo:g}-{hi:g}" if (lo and hi) else f"{price:g}"
    return (
        {"op": "and", "items": [{"type": "price", "op": ">=", "value": price}]},
        desc,
        "",
    )


@router.post("/from-plan")
async def create_alerts_from_plan(
    payload: PriceAlertFromPlan, db: Session = Depends(get_db)
):
    """把操作计划里的价位批量加成提醒(原子:任一项非法则整批不创建)。

    返回 created / rejected 两份清单:rejected 会说明原因(未给出该价位、已达成等),
    前端据此提示用户哪些项被跳过,而不是静默少建几条。
    """
    from src.modules.automation.operation_plan import QUANT_FIELDS  # noqa: F401
    from src.platform.marketdata.marketdata_client import md_quote_rows
    from src.platform.persistence.models import OperationPlan

    plan = db.query(OperationPlan).filter(OperationPlan.id == payload.plan_id).first()
    if not plan:
        raise HTTPException(404, "操作计划不存在")
    if not payload.items:
        raise HTTPException(400, "未选择任何要加入提醒的价位")

    stock = (
        db.query(Stock)
        .filter(Stock.symbol == plan.stock_symbol, Stock.market == plan.market)
        .first()
    )
    if not stock:
        raise HTTPException(400, f"{plan.stock_name}({plan.stock_symbol}) 不在自选列表中，请先加入自选")

    scenarios = plan.scenarios or []
    if not isinstance(scenarios, list):
        raise HTTPException(400, "计划数据异常，无法解析情景")

    # 现价用于"是否已经达成"的校验;取不到就跳过校验(不因行情抖动阻断建提醒)
    current_price: float | None = None
    try:
        rows = await asyncio.to_thread(md_quote_rows, [plan.stock_symbol], plan.market)
        if rows:
            current_price = rows[0].get("current_price")
    except Exception as e:
        logger.warning(f"取现价失败，跳过价位有效性校验: {e}")

    # 不传则用"下一个交易日收盘"(本地时区语义),而不是计划日期当天 23:59 ——
    # 后者在收盘后加提醒时已经过期,且会被当成 UTC 存成次日凌晨,对不上开盘。
    expire_value = payload.expire_at.strip() or price_alert_service.default_alert_expiry()

    created: list[dict] = []
    rejected: list[dict] = []

    for item in payload.items:
        meta = _PLAN_LEVEL_META.get(item.level)
        if not meta:
            rejected.append({"level": item.level, "reason": "未知的价位类型"})
            continue
        if item.scenario_index >= len(scenarios):
            rejected.append({"level": item.level, "reason": "情景下标越界"})
            continue

        scenario = scenarios[item.scenario_index] or {}
        title = str(scenario.get("title") or f"情景{item.scenario_index + 1}")
        condition, desc, reason = _plan_level_condition(item.level, scenario)
        if not condition:
            rejected.append({"level": item.level, "title": title, "reason": reason})
            continue

        # 目标价已达成 → 拒绝(再加只会立刻触发一条"已达成"的噪声提醒)
        if item.level in ("target1", "target2") and current_price:
            target = condition["items"][0]["value"]
            if current_price >= float(target):
                rejected.append({
                    "level": item.level,
                    "title": title,
                    "reason": f"现价 {current_price:g} 已达成该目标 {target:g}，无需重复提醒",
                })
                continue

        tag = _scenario_tag(item.scenario_index, title)
        label = f"{title} · {meta['label']} {desc}"
        try:
            rule = price_alert_service.create_alert_rule(
                db,
                stock_id=stock.id,
                name=f"【计划】{plan.stock_name} {tag}·{meta['label']} {desc}",
                condition_group=condition,
                market_hours_mode="trading_only",
                cooldown_minutes=payload.cooldown_minutes,
                max_triggers_per_day=payload.max_triggers_per_day,
                repeat_mode="repeat",
                expire_at=expire_value,
                notify_channel_ids=payload.notify_channel_ids,
                source="operation_plan",
                source_plan_id=plan.id,
                source_label=label,
            )
        except (ValueError, LookupError) as e:
            rejected.append({"level": item.level, "title": title, "reason": str(e)})
            continue

        created.append(
            {
                **_to_response(rule),
                "source_label": label,
                "current_price": current_price,
                "already_true": _condition_already_true(condition, current_price),
            }
        )

    return {
        "created": created,
        "rejected": rejected,
        "current_price": current_price,
        # 回显本地时间(created 里的 expire_at 已由 _to_response 格式化过)
        "expire_at": (created[0].get("expire_at") or "") if created else "",
    }
