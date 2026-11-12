"""成交同步 HTTP 接口：手机短信/通知、交割单导入。

鉴权：登录 JWT 或带 ``trade:write`` scope 的 PAT（手机自动化用后者）。
所有接口都复用 ``trade_ingest``，与手工记账同一套费用/成本/现金口径。
"""

from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.administration.pat_auth import authenticate_sync
from src.modules.portfolio import trade_import, trade_ingest
from src.modules.portfolio.trade_text import parse_trade_text
from src.platform.persistence.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_CONTENT = 4 * 1024 * 1024  # 4MB
_MAX_ROWS = 2000


class IngestIn(BaseModel):
    """一条成交：二选一——``text`` 原文，或结构化字段。"""

    text: str = Field("", max_length=20000)
    account_id: int | None = None
    account_name: str = ""
    source: str = Field("sms", description="sms / notification / import / assistant")
    market: str = "CN"
    dry_run: bool = False
    force: bool = False
    # 结构化字段（不给 text 时使用）
    action: str | None = None
    symbol: str = ""
    name: str = ""
    quantity: int | None = None
    price: float | None = None
    trade_date: str | None = None
    time: str = ""
    fees: dict | None = None
    external_id: str = ""
    note: str = ""


class ImportIn(BaseModel):
    content: str = Field("", description="CSV/TSV/HTML 文本内容")
    base64: str = Field("", description="xlsx/xls 的 base64 内容")
    filename: str = ""
    account_id: int | None = None
    account_name: str = ""
    lines: list[int] | None = Field(None, description="只提交这些行号（预览里的 line）")


def _decode_import(payload: ImportIn) -> bytes | str:
    if payload.base64:
        if len(payload.base64) > _MAX_CONTENT * 2:
            raise HTTPException(413, "文件过大")
        try:
            return base64.b64decode(payload.base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "base64 内容无法解码")
    if len(payload.content) > _MAX_CONTENT:
        raise HTTPException(413, "文件过大")
    if not payload.content.strip():
        raise HTTPException(400, "没有收到文件内容")
    return payload.content


def _resolve_or_400(db: Session, account_id: int | None, account_name: str):
    account, error = trade_ingest.resolve_account(db, account_id, account_name)
    if account is None:
        raise HTTPException(400, error)
    return account


@router.post("/portfolio/trades/ingest")
def ingest_trade(payload: IngestIn, db: Session = Depends(get_db), _auth=Depends(authenticate_sync)):
    """解析并写入一笔记账交易（短信/通知/助手/脚本共用）。"""
    source = payload.source if payload.source in ("sms", "notification", "import", "assistant") else "sms"
    parsed: dict
    if payload.text.strip():
        parsed = parse_trade_text(payload.text)
        if not parsed.get("ok"):
            return {
                "ok": False,
                "stage": "parse",
                "reason": parsed.get("reason") or "解析失败",
                "parsed": parsed,
                "hint": "可以把原文发到理财页「交易同步」里试解析，再按结构化字段提交",
            }
        data = {
            **parsed,
            "account_id": payload.account_id,
            "account_name": payload.account_name,
            "market": payload.market,
            "source": source,
        }
    else:
        if not payload.action or payload.quantity is None or payload.price is None:
            raise HTTPException(400, "缺少 action/quantity/price，或提供 text 原文")
        data = {
            "action": payload.action,
            "symbol": payload.symbol,
            "name": payload.name,
            "quantity": payload.quantity,
            "price": payload.price,
            "trade_date": payload.trade_date,
            "time": payload.time,
            "fees": payload.fees or {},
            "external_id": payload.external_id,
            "note": payload.note,
            "account_id": payload.account_id,
            "account_name": payload.account_name,
            "market": payload.market,
        }

    result = trade_ingest.ingest_trade(
        db,
        data,
        source=source,
        dry_run=payload.dry_run,
        force=payload.force,
    )
    result["parsed"] = parsed if payload.text.strip() else None
    return result


@router.post("/portfolio/trades/import/preview")
def import_preview(payload: ImportIn, db: Session = Depends(get_db), _auth=Depends(authenticate_sync)):
    """上传交割单 → 逐行解析、查重、标注状态，不写库。"""
    account = _resolve_or_400(db, payload.account_id, payload.account_name)
    content = _decode_import(payload)
    try:
        rows = trade_import.parse_statement(content, filename=payload.filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("交割单解析失败")
        raise HTTPException(400, f"交割单解析失败: {exc}")

    rows = rows[:_MAX_ROWS]
    annotated = trade_ingest.annotate_rows(db, rows, account=account)
    summary = {"total": len(annotated), "ok": 0, "duplicate": 0, "invalid": 0, "new_stock": 0}
    for row in annotated:
        status = row.get("status") or "invalid"
        summary[status] = summary.get(status, 0) + 1
    return {
        "ok": True,
        "account": {"id": account.id, "name": account.name},
        "summary": summary,
        "rows": annotated,
    }


@router.post("/portfolio/trades/import/commit")
def import_commit(payload: ImportIn, db: Session = Depends(get_db), _auth=Depends(authenticate_sync)):
    """确认导入：按成交时间顺序写入，重复行跳过。"""
    account = _resolve_or_400(db, payload.account_id, payload.account_name)
    content = _decode_import(payload)
    try:
        rows = trade_import.parse_statement(content, filename=payload.filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    rows = rows[:_MAX_ROWS]
    annotated = trade_ingest.annotate_rows(db, rows, account=account)
    if payload.lines is not None:
        wanted = {int(line) for line in payload.lines}
        annotated = [row for row in annotated if int(row.get("line") or 0) in wanted]
    result = trade_ingest.commit_rows(db, annotated, account=account, source="import")
    return {"ok": True, "account": {"id": account.id, "name": account.name}, **result}
