import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.platform.persistence.database import get_db
from src.platform.persistence.models import (
    Account,
    AgentConfig,
    AIModel,
    AIService,
    AppSettings,
    DataSource,
    NotifyChannel,
    Position,
    PriceAlertRule,
    ScreenerRule,
    Stock,
    StockAgent,
)
from src.modules.automation.agent_catalog import AGENT_KIND_CAPABILITY, infer_agent_kind


logger = logging.getLogger(__name__)
router = APIRouter()

# 只导出「用户配置」。认证口令 / JWT 密钥不导出(新机器自行设密码),
# 内部标记(market_source_priority_v2)也不导出。
_EXPORT_SETTINGS_KEYS = {
    "http_proxy",
    "notify_quiet_hours",
    "notify_retry_attempts",
    "notify_retry_backoff_seconds",
    "notify_dedupe_ttl_overrides",
    "panwatch_base_url",
    "stock_link_platform",
    "dataset_sync_schedule",
}

_IMPORTABLE_SETTINGS_KEYS = set(_EXPORT_SETTINGS_KEYS)

PACKAGE_VERSION = 2


class TemplateAgent(BaseModel):
    name: str
    kind: str | None = None
    visible: bool | None = None
    lifecycle_status: str | None = None
    replaced_by: str | None = None
    display_order: int | None = None
    enabled: bool = True
    schedule: str = ""
    execution_mode: str = "batch"
    ai_model_id: int | None = None
    notify_channel_ids: list[int] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


# ── 导出 ──────────────────────────────────────────────────────────────
def _export_settings(db: Session) -> dict[str, str]:
    rows = (
        db.query(AppSettings)
        .filter(AppSettings.key.in_(sorted(_EXPORT_SETTINGS_KEYS)))
        .all()
    )
    return {r.key: (r.value or "") for r in rows}


def _export_agents(db: Session, include_internal: bool) -> list[dict]:
    query = db.query(AgentConfig)
    if not include_internal:
        query = query.filter(AgentConfig.visible == True)  # noqa: E712
    rows = query.order_by(AgentConfig.display_order.asc(), AgentConfig.name.asc()).all()
    out = []
    for a in rows:
        out.append(
            {
                "name": a.name,
                "kind": (a.kind or "").strip() or infer_agent_kind(a.name),
                "visible": bool(a.visible),
                "lifecycle_status": a.lifecycle_status or "active",
                "replaced_by": a.replaced_by or "",
                "display_order": int(a.display_order or 0),
                "enabled": bool(a.enabled),
                "schedule": a.schedule or "",
                "execution_mode": a.execution_mode or "batch",
                "ai_model_id": a.ai_model_id,
                "notify_channel_ids": a.notify_channel_ids or [],
                "config": a.config or {},
            }
        )
    return out


def _export_stocks(db: Session) -> list[dict]:
    out = []
    for s in db.query(Stock).order_by(Stock.market.asc(), Stock.symbol.asc()).all():
        sa_rows = (
            db.query(StockAgent)
            .filter(StockAgent.stock_id == s.id)
            .order_by(StockAgent.agent_name.asc())
            .all()
        )
        out.append(
            {
                "symbol": s.symbol,
                "name": s.name,
                "market": s.market,
                "group_name": s.group_name or "",
                "sort_order": int(s.sort_order or 0),
                "agents": [
                    {
                        "agent_name": sa.agent_name,
                        "schedule": sa.schedule or "",
                        "ai_model_id": sa.ai_model_id,
                        "notify_channel_ids": sa.notify_channel_ids or [],
                    }
                    for sa in sa_rows
                ],
            }
        )
    return out


def _export_ai(db: Session, include_secrets: bool) -> dict:
    services = []
    for svc in db.query(AIService).order_by(AIService.id.asc()).all():
        models = (
            db.query(AIModel)
            .filter(AIModel.service_id == svc.id)
            .order_by(AIModel.id.asc())
            .all()
        )
        services.append(
            {
                "id": svc.id,
                "name": svc.name,
                "base_url": svc.base_url,
                "api_key": (svc.api_key or "") if include_secrets else "",
                "models": [
                    {
                        "id": m.id,
                        "name": m.name,
                        "model": m.model,
                        "is_default": bool(m.is_default),
                    }
                    for m in models
                ],
            }
        )
    return {"services": services}


def _export_channels(db: Session, include_secrets: bool) -> list[dict]:
    out = []
    for c in db.query(NotifyChannel).order_by(NotifyChannel.id.asc()).all():
        config = dict(c.config or {})
        if not include_secrets:
            config = {k: ("" if any(x in k.lower() for x in ("token", "key", "secret", "webhook")) else v)
                      for k, v in config.items()}
        out.append(
            {
                "id": c.id,
                "name": c.name,
                "type": c.type,
                "config": config,
                "enabled": bool(c.enabled),
                "is_default": bool(c.is_default),
            }
        )
    return out


def _export_sources(db: Session, include_secrets: bool) -> list[dict]:
    out = []
    for r in db.query(DataSource).order_by(DataSource.type.asc(), DataSource.priority.asc()).all():
        config = dict(r.config or {})
        if not include_secrets:
            config.pop("api_key", None)
            config.pop("token", None)
            config.pop("cookies", None)
        out.append(
            {
                "name": r.name,
                "type": r.type,
                "provider": r.provider,
                "config": config,
                "enabled": bool(r.enabled),
                "priority": int(r.priority or 0),
                "supports_batch": bool(r.supports_batch),
                "test_symbols": list(r.test_symbols or []),
            }
        )
    return out


def _export_accounts(db: Session) -> list[dict]:
    out = []
    for acc in db.query(Account).order_by(Account.id.asc()).all():
        positions = []
        for p in db.query(Position).filter(Position.account_id == acc.id).all():
            stock = db.query(Stock).filter(Stock.id == p.stock_id).first()
            if not stock:
                continue
            positions.append(
                {
                    "symbol": stock.symbol,
                    "market": stock.market,
                    "cost_price": p.cost_price,
                    "quantity": p.quantity,
                    "invested_amount": p.invested_amount,
                    "sort_order": int(p.sort_order or 0),
                    "trading_style": p.trading_style or "swing",
                }
            )
        out.append(
            {
                "name": acc.name,
                "available_funds": acc.available_funds or 0,
                "enabled": bool(acc.enabled),
                "positions": positions,
            }
        )
    return out


def _export_alerts(db: Session) -> list[dict]:
    out = []
    for r in db.query(PriceAlertRule).order_by(PriceAlertRule.id.asc()).all():
        stock = db.query(Stock).filter(Stock.id == r.stock_id).first()
        if not stock:
            continue
        out.append(
            {
                "symbol": stock.symbol,
                "market": stock.market,
                "name": r.name or "",
                "enabled": bool(r.enabled),
                "condition_group": r.condition_group or {},
                "market_hours_mode": r.market_hours_mode or "trading_only",
                "cooldown_minutes": int(r.cooldown_minutes or 30),
                "max_triggers_per_day": int(r.max_triggers_per_day or 3),
                "repeat_mode": r.repeat_mode or "repeat",
                "expire_at": r.expire_at.isoformat() if r.expire_at else "",
                "notify_channel_ids": r.notify_channel_ids or [],
                "source": r.source or "",
                "source_label": r.source_label or "",
            }
        )
    return out


def _export_screener_rules(db: Session) -> list[dict]:
    return [
        {
            "name": r.name,
            "market": r.market or "CN",
            "conditions": r.conditions or {},
            "order_by": r.order_by or "change_pct desc",
            "limit_n": int(r.limit_n or 100),
            "sort_order": int(r.sort_order or 0),
        }
        for r in db.query(ScreenerRule)
        .filter(ScreenerRule.is_preset.is_(False))
        .order_by(ScreenerRule.id.asc())
        .all()
    ]


@router.get("/export")
def export_template(
    include_internal: bool = Query(default=True),
    include_secrets: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """导出完整配置包(含数据源/模型/通知渠道/账户持仓/提醒/选股分组)。"""
    return {
        "version": PACKAGE_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "settings": _export_settings(db),
        "agents": _export_agents(db, include_internal),
        "stocks": _export_stocks(db),
        "ai": _export_ai(db, include_secrets),
        "notify_channels": _export_channels(db, include_secrets),
        "data_sources": _export_sources(db, include_secrets),
        "accounts": _export_accounts(db),
        "price_alerts": _export_alerts(db),
        "screener_rules": _export_screener_rules(db),
    }


# ── 导入 ──────────────────────────────────────────────────────────────
def _upsert_settings(db: Session, settings: dict) -> int:
    n = 0
    for k, v in (settings or {}).items():
        if k not in _IMPORTABLE_SETTINGS_KEYS:
            continue
        row = db.query(AppSettings).filter(AppSettings.key == k).first()
        if row:
            row.value = str(v or "")
        else:
            db.add(AppSettings(key=k, value=str(v or ""), description=""))
        n += 1
    return n


def _import_ai(db: Session, ai: dict) -> tuple[dict[int, int], dict[int, int], int]:
    """返回 (旧服务id→新id, 旧模型id→新id, 导入数)。"""
    svc_map: dict[int, int] = {}
    model_map: dict[int, int] = {}
    count = 0
    for svc in (ai or {}).get("services") or []:
        name = str(svc.get("name") or "").strip()
        base_url = str(svc.get("base_url") or "").strip()
        if not name or not base_url:
            continue
        row = (
            db.query(AIService)
            .filter(AIService.name == name, AIService.base_url == base_url)
            .first()
        )
        if row is None:
            row = AIService(name=name, base_url=base_url, api_key=svc.get("api_key") or "")
            db.add(row)
            db.flush()
        else:
            if svc.get("api_key"):
                row.api_key = svc.get("api_key") or row.api_key
        if svc.get("id") is not None:
            svc_map[int(svc["id"])] = row.id
        count += 1

        for m in svc.get("models") or []:
            model = str(m.get("model") or "").strip()
            if not model:
                continue
            mr = (
                db.query(AIModel)
                .filter(AIModel.service_id == row.id, AIModel.model == model)
                .first()
            )
            if mr is None:
                mr = AIModel(
                    service_id=row.id,
                    name=m.get("name") or model,
                    model=model,
                    is_default=bool(m.get("is_default")),
                )
                db.add(mr)
                db.flush()
            else:
                mr.name = m.get("name") or mr.name
                if m.get("is_default"):
                    mr.is_default = True
            if m.get("id") is not None:
                model_map[int(m["id"])] = mr.id
            count += 1
    return svc_map, model_map, count


def _import_channels(db: Session, rows: list[dict]) -> tuple[dict[int, int], int]:
    id_map: dict[int, int] = {}
    n = 0
    for c in rows or []:
        name = str(c.get("name") or "").strip()
        type_ = str(c.get("type") or "").strip()
        if not name or not type_:
            continue
        row = (
            db.query(NotifyChannel)
            .filter(NotifyChannel.name == name, NotifyChannel.type == type_)
            .first()
        )
        if row is None:
            row = NotifyChannel(name=name, type=type_, config=c.get("config") or {})
            db.add(row)
            db.flush()
        else:
            if c.get("config"):
                row.config = {**(row.config or {}), **(c.get("config") or {})}
        row.enabled = bool(c.get("enabled", True))
        if c.get("is_default"):
            # 只能有一个默认渠道
            db.query(NotifyChannel).update({NotifyChannel.is_default: False})
            row.is_default = True
        if c.get("id") is not None:
            id_map[int(c["id"])] = row.id
        n += 1
    return id_map, n


def _import_sources(db: Session, rows: list[dict]) -> int:
    n = 0
    for s in rows or []:
        type_ = str(s.get("type") or "").strip()
        provider = str(s.get("provider") or "").strip()
        name = str(s.get("name") or "").strip() or f"{type_}-{provider}"
        if not type_ or not provider:
            continue
        row = (
            db.query(DataSource)
            .filter(DataSource.type == type_, DataSource.provider == provider)
            .first()
        )
        if row is None:
            row = DataSource(type=type_, provider=provider, name=name, config={})
            db.add(row)
        row.name = name
        if s.get("config"):
            row.config = {**(row.config or {}), **(s.get("config") or {})}
        row.enabled = bool(s.get("enabled", True))
        row.priority = int(s.get("priority", 0) or 0)
        row.supports_batch = bool(s.get("supports_batch", False))
        if s.get("test_symbols"):
            row.test_symbols = list(s["test_symbols"])
        n += 1
    return n


def _import_accounts(db: Session, rows: list[dict]) -> tuple[int, int]:
    accs = 0
    pos = 0
    for a in rows or []:
        name = str(a.get("name") or "").strip()
        if not name:
            continue
        acc = db.query(Account).filter(Account.name == name).first()
        if acc is None:
            acc = Account(name=name, available_funds=float(a.get("available_funds") or 0), enabled=True)
            db.add(acc)
            db.flush()
        else:
            acc.available_funds = float(a.get("available_funds") or acc.available_funds or 0)
            acc.enabled = bool(a.get("enabled", acc.enabled))
        accs += 1

        for p in a.get("positions") or []:
            stock = (
                db.query(Stock)
                .filter(
                    Stock.symbol == p.get("symbol"),
                    Stock.market == (p.get("market") or "CN"),
                )
                .first()
            )
            if stock is None:
                continue
            row = (
                db.query(Position)
                .filter(Position.account_id == acc.id, Position.stock_id == stock.id)
                .first()
            )
            if row is None:
                row = Position(
                    account_id=acc.id,
                    stock_id=stock.id,
                    cost_price=float(p.get("cost_price") or 0),
                    quantity=int(p.get("quantity") or 0),
                )
                db.add(row)
            row.cost_price = float(p.get("cost_price") or row.cost_price or 0)
            row.quantity = int(p.get("quantity") or row.quantity or 0)
            row.invested_amount = p.get("invested_amount")
            row.sort_order = int(p.get("sort_order") or 0)
            row.trading_style = p.get("trading_style") or row.trading_style or "swing"
            pos += 1
    return accs, pos


def _import_stocks(
    db: Session, rows: list[dict]
) -> tuple[dict[tuple[str, str], int], int, int]:
    """返回 (自然键→id, 新建数, 更新数)。"""
    id_map: dict[tuple[str, str], int] = {}
    created = updated = 0
    for s in rows or []:
        symbol = str(s.get("symbol") or "").strip()
        market = str(s.get("market") or "CN").strip() or "CN"
        if not symbol:
            continue
        stock = db.query(Stock).filter(Stock.symbol == symbol, Stock.market == market).first()
        if stock is None:
            stock = Stock(symbol=symbol, name=s.get("name") or symbol, market=market)
            db.add(stock)
            db.flush()
            created += 1
        else:
            stock.name = s.get("name") or stock.name
            updated += 1
        stock.group_name = s.get("group_name") or stock.group_name or ""
        if s.get("sort_order") is not None:
            stock.sort_order = int(s["sort_order"])
        id_map[(symbol, market)] = stock.id
    return id_map, created, updated


def _import_alerts(db: Session, rows: list[dict], channel_map: dict[int, int]) -> int:
    n = 0
    for r in rows or []:
        stock = (
            db.query(Stock)
            .filter(
                Stock.symbol == r.get("symbol"),
                Stock.market == (r.get("market") or "CN"),
            )
            .first()
        )
        if stock is None:
            continue
        name = str(r.get("name") or "")
        row = (
            db.query(PriceAlertRule)
            .filter(PriceAlertRule.stock_id == stock.id, PriceAlertRule.name == name)
            .first()
        )
        if row is None:
            row = PriceAlertRule(stock_id=stock.id, name=name)
            db.add(row)
        row.enabled = bool(r.get("enabled", True))
        row.condition_group = r.get("condition_group") or {}
        row.market_hours_mode = r.get("market_hours_mode") or "trading_only"
        row.cooldown_minutes = int(r.get("cooldown_minutes") or 30)
        row.max_triggers_per_day = int(r.get("max_triggers_per_day") or 3)
        row.repeat_mode = r.get("repeat_mode") or "repeat"
        row.source = r.get("source") or ""
        row.source_label = r.get("source_label") or ""
        expire = r.get("expire_at")
        if expire and isinstance(expire, str):
            try:
                row.expire_at = datetime.fromisoformat(expire)
            except ValueError:
                row.expire_at = None
        row.notify_channel_ids = [
            channel_map.get(int(x), int(x))
            for x in (r.get("notify_channel_ids") or [])
        ]
        n += 1
    return n


def _import_screener_rules(db: Session, rows: list[dict]) -> int:
    n = 0
    for r in rows or []:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        row = db.query(ScreenerRule).filter(ScreenerRule.name == name).first()
        if row is None:
            row = ScreenerRule(name=name)
            db.add(row)
        row.market = r.get("market") or "CN"
        row.conditions = r.get("conditions") or {}
        row.order_by = r.get("order_by") or "change_pct desc"
        row.limit_n = int(r.get("limit_n") or 100)
        row.sort_order = int(r.get("sort_order") or 0)
        n += 1
    return n


def _import_agents(
    db: Session,
    agents: list[dict],
    model_map: dict[int, int],
    channel_map: dict[int, int],
    mode: str,
    has_maps: bool,
) -> tuple[int, int]:
    created = updated = 0
    for a in agents or []:
        row = db.query(AgentConfig).filter(AgentConfig.name == a.get("name")).first()
        if not row:
            row = AgentConfig(name=a.get("name"), display_name=a.get("name"), description="")
            db.add(row)
            created += 1
        else:
            updated += 1

        row.kind = (a.get("kind") or "").strip() or infer_agent_kind(a.get("name") or "")
        row.visible = (
            bool(a.get("visible"))
            if a.get("visible") is not None
            else (row.kind != AGENT_KIND_CAPABILITY)
        )
        row.lifecycle_status = a.get("lifecycle_status") or (
            "deprecated" if row.kind == AGENT_KIND_CAPABILITY else "active"
        )
        row.replaced_by = a.get("replaced_by") or row.replaced_by or ""
        row.display_order = int(a.get("display_order") or row.display_order or 0)
        row.enabled = bool(a.get("enabled", True))
        row.schedule = a.get("schedule") or ""
        row.execution_mode = a.get("execution_mode") or "batch"

        old_model = a.get("ai_model_id")
        if has_maps:
            row.ai_model_id = model_map.get(int(old_model)) if old_model else None
        else:
            row.ai_model_id = old_model

        old_channels = a.get("notify_channel_ids") or []
        row.notify_channel_ids = (
            [channel_map.get(int(x), int(x)) for x in old_channels]
            if has_maps
            else old_channels
        )

        if row.kind == AGENT_KIND_CAPABILITY:
            row.enabled = False
            row.schedule = ""

        cfg = row.config or {}
        if mode == "replace":
            row.config = a.get("config") or {}
        elif isinstance(cfg, dict) and isinstance(a.get("config"), dict):
            cfg.update(a.get("config") or {})
            row.config = cfg
        else:
            row.config = a.get("config") or {}
    return created, updated


def _import_stock_agents(
    db: Session,
    stocks_payload: list[dict],
    stock_map: dict[tuple[str, str], int],
    model_map: dict[int, int],
    channel_map: dict[int, int],
    mode: str,
    has_maps: bool,
) -> tuple[int, int]:
    created = updated = 0
    for s in stocks_payload or []:
        key = (str(s.get("symbol")), str(s.get("market") or "CN"))
        stock_id = stock_map.get(key)
        if stock_id is None:
            continue
        existing = db.query(StockAgent).filter(StockAgent.stock_id == stock_id).all()
        existing_map = {x.agent_name: x for x in existing}
        desired_names = {x.get("agent_name") for x in (s.get("agents") or [])}
        if mode == "replace":
            for x in existing:
                if x.agent_name not in desired_names:
                    db.delete(x)
        for sa in s.get("agents") or []:
            name = sa.get("agent_name")
            if not name:
                continue
            row = existing_map.get(name)
            old_model = sa.get("ai_model_id")
            model_id = model_map.get(int(old_model)) if (has_maps and old_model) else old_model
            channels = sa.get("notify_channel_ids") or []
            channel_ids = (
                [channel_map.get(int(x), int(x)) for x in channels] if has_maps else channels
            )
            if not row:
                row = StockAgent(
                    stock_id=stock_id,
                    agent_name=name,
                    schedule=sa.get("schedule") or "",
                    ai_model_id=model_id,
                    notify_channel_ids=channel_ids,
                )
                db.add(row)
                created += 1
            else:
                row.schedule = sa.get("schedule") or ""
                row.ai_model_id = model_id
                row.notify_channel_ids = channel_ids
                updated += 1
    return created, updated


@router.post("/import")
async def import_template(
    payload: dict,
    mode: str = Query(
        "merge", description="merge=合并更新, replace=替换(仅对 payload 涵盖的数据)"
    ),
    db: Session = Depends(get_db),
):
    """导入配置包。支持 v1(旧模板)与 v2(完整配置)。"""
    version = int(payload.get("version") or 1)
    if version not in (1, 2):
        raise HTTPException(400, f"不支持的配置包版本: {version}")
    if mode not in ("merge", "replace"):
        raise HTTPException(400, "mode 仅支持 merge/replace")

    updated_settings = _upsert_settings(db, payload.get("settings") or {})

    # v2:先导入被引用的实体,拿到新旧 id 映射
    model_map: dict[int, int] = {}
    channel_map: dict[int, int] = {}
    has_maps = version >= 2
    ai_count = channels_count = sources_count = 0
    accounts_count = positions_count = alerts_count = rules_count = 0

    if has_maps:
        _, model_map, ai_count = _import_ai(db, payload.get("ai") or {})
        channel_map, channels_count = _import_channels(db, payload.get("notify_channels") or [])
        sources_count = _import_sources(db, payload.get("data_sources") or [])

    stock_map, created_stocks, updated_stocks = _import_stocks(
        db, payload.get("stocks") or []
    )

    if has_maps:
        accounts_count, positions_count = _import_accounts(db, payload.get("accounts") or [])
        alerts_count = _import_alerts(db, payload.get("price_alerts") or [], channel_map)
        rules_count = _import_screener_rules(db, payload.get("screener_rules") or [])

    created_agents, updated_agents = _import_agents(
        db, payload.get("agents") or [], model_map, channel_map, mode, has_maps
    )
    created_stock_agents, updated_stock_agents = _import_stock_agents(
        db, payload.get("stocks") or [], stock_map, model_map, channel_map, mode, has_maps
    )

    db.commit()
    logger.info(
        f"导入配置包(v{version}): settings={updated_settings} agents(+{created_agents}/~{updated_agents}) "
        f"stocks={len(stock_map)} accounts={accounts_count} positions={positions_count} "
        f"ai={ai_count} channels={channels_count} sources={sources_count} "
        f"alerts={alerts_count} screener_rules={rules_count}"
    )

    reloaded = False
    try:
        from server import reload_scheduler

        reloaded = bool(reload_scheduler())
    except Exception:
        reloaded = False

    return {
        "ok": True,
        "version": version,
        "mode": mode,
        "scheduler_reloaded": reloaded,
        "summary": {
            "updated_settings": updated_settings,
            "created_agents": created_agents,
            "updated_agents": updated_agents,
            "created_stocks": created_stocks,
            "updated_stocks": updated_stocks,
            "created_stock_agents": created_stock_agents,
            "updated_stock_agents": updated_stock_agents,
            "ai_services_models": ai_count,
            "notify_channels": channels_count,
            "data_sources": sources_count,
            "accounts": accounts_count,
            "positions": positions_count,
            "price_alerts": alerts_count,
            "screener_rules": rules_count,
        },
    }
