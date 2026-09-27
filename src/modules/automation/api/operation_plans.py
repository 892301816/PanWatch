"""每日操作计划 API。

计划按 (market, stock_symbol, plan_date) 留档:同一只股票同一天只保留最新一份,
历史日期原样可查 —— "当时是怎么计划的"能回溯,便于复盘自己的执行偏差。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.platform.persistence.database import get_db
from src.platform.persistence.models import OperationPlan, PriceAlertRule

logger = logging.getLogger(__name__)
router = APIRouter()

PLAN_STATUSES = ("pending", "executing", "completed", "expired")


def _to_response(row: OperationPlan) -> dict:
    return {
        "id": row.id,
        "stock_symbol": row.stock_symbol,
        "stock_name": row.stock_name,
        "market": row.market,
        "plan_date": row.plan_date,
        "plan_end_date": row.plan_end_date,
        "agent_name": row.agent_name,
        "overall_judgment": row.overall_judgment,
        "scenarios": row.scenarios or [],
        "discipline": row.discipline or [],
        "summary": row.summary,
        "risk_warning": row.risk_warning,
        "content": row.content,
        "status": row.status,
        "remarks": row.remarks,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else "",
    }


@router.get("")
def list_operation_plans(
    symbol: str = Query("", description="股票代码;为空返回全部"),
    market: str = Query("", description="市场代码: CN/HK/US"),
    start_date: str = Query("", description="起始日期 YYYY-MM-DD(含)"),
    end_date: str = Query("", description="结束日期 YYYY-MM-DD(含)"),
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """操作计划列表(按计划日期倒序)。不传 symbol 即为"历史留档"视图。"""
    query = db.query(OperationPlan)
    if symbol:
        query = query.filter(OperationPlan.stock_symbol == symbol.strip())
    if market:
        query = query.filter(OperationPlan.market == market.strip().upper())
    if start_date:
        query = query.filter(OperationPlan.plan_date >= start_date.strip())
    if end_date:
        query = query.filter(OperationPlan.plan_date <= end_date.strip())

    rows = (
        query.order_by(OperationPlan.plan_date.desc(), OperationPlan.id.desc())
        .limit(limit)
        .all()
    )
    return [_to_response(r) for r in rows]


@router.get("/latest")
def get_latest_plan(
    symbol: str = Query(..., description="股票代码"),
    market: str = Query("CN", description="市场代码: CN/HK/US"),
    db: Session = Depends(get_db),
):
    """某只股票最近一份操作计划(个股弹窗用)。没有则返回 null,不报 404。"""
    row = (
        db.query(OperationPlan)
        .filter(
            OperationPlan.stock_symbol == symbol.strip(),
            OperationPlan.market == market.strip().upper(),
        )
        .order_by(OperationPlan.plan_date.desc(), OperationPlan.id.desc())
        .first()
    )
    if not row:
        return None
    payload = _to_response(row)
    # 带上该计划已创建的提醒数,便于界面显示"已加 N 条提醒"
    payload["alert_count"] = (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.source_plan_id == row.id)
        .count()
    )
    return payload


@router.get("/{plan_id}")
def get_operation_plan(plan_id: int, db: Session = Depends(get_db)):
    row = db.query(OperationPlan).filter(OperationPlan.id == plan_id).first()
    if not row:
        raise HTTPException(404, "操作计划不存在")
    payload = _to_response(row)
    alerts = (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.source_plan_id == plan_id)
        .all()
    )
    payload["alerts"] = [
        {
            "id": a.id,
            "name": a.name,
            "source_label": a.source_label or "",
            "enabled": bool(a.enabled),
            "condition_group": a.condition_group or {},
            "expire_at": a.expire_at.isoformat(timespec="seconds") if a.expire_at else None,
        }
        for a in alerts
    ]
    return payload


@router.delete("/{plan_id}")
def delete_operation_plan(
    plan_id: int,
    delete_alerts: bool = Query(False, description="同时删除该计划创建的提醒"),
    db: Session = Depends(get_db),
):
    """删除一份计划。默认保留提醒(提醒一旦建立即独立生效,不随计划消失)。"""
    row = db.query(OperationPlan).filter(OperationPlan.id == plan_id).first()
    if not row:
        raise HTTPException(404, "操作计划不存在")

    removed_alerts = 0
    if delete_alerts:
        from src.modules.market.price_alert_service import delete_alert_rule

        alert_ids = [
            r[0]
            for r in db.query(PriceAlertRule.id)
            .filter(PriceAlertRule.source_plan_id == plan_id)
            .all()
        ]
        for alert_id in alert_ids:
            if delete_alert_rule(db, alert_id):
                removed_alerts += 1

    db.delete(row)
    db.commit()
    return {"ok": True, "removed_alerts": removed_alerts}


@router.put("/{plan_id}/status")
def update_plan_status(
    plan_id: int,
    status: str = Query(..., description="pending/executing/completed/expired"),
    remarks: str = Query("", description="备注"),
    db: Session = Depends(get_db),
):
    """更新计划执行状态(供人工标注"已按计划执行/已放弃")。"""
    if status not in PLAN_STATUSES:
        raise HTTPException(400, f"不支持的状态: {status}")
    row = db.query(OperationPlan).filter(OperationPlan.id == plan_id).first()
    if not row:
        raise HTTPException(404, "操作计划不存在")
    row.status = status
    if remarks:
        row.remarks = remarks
    db.commit()
    db.refresh(row)
    return _to_response(row)
