"""截图导入：用视觉模型把同花顺/券商截图识别成可复核的持仓或成交。

两种模式：

- ``positions``：持仓截图 → 持仓快照（数量/成本），只更新持仓，不写交易流水、不动现金；
- ``trades``：成交/交割单截图 → 交易流水，复用 ``trade_ingest`` 的费率与幂等逻辑。

识别结果先返回预览（含与当前持仓的差异），由用户确认后再提交；提交不再调用模型。
图片会发送给用户自己配置的 AI 服务（DeepSeek/opencode 等），不会外传其它数据。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import tempfile
from datetime import date as date_cls
from typing import Any

from sqlalchemy.orm import Session

from src.modules.portfolio import trade_ingest
from src.platform.persistence.models import Account, Position, Stock

logger = logging.getLogger(__name__)

MODES = {"positions", "trades"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_SIDE = 1600
AI_TIMEOUT_SEC = 120

SYSTEM_PROMPT = (
    "你是券商/同花顺截图结构化助手。只输出 JSON，不要解释、不要 markdown 代码块。"
    "看不清的字段填 null；不要编造不存在的行；忽略合计、汇总、表头行。"
)

POSITION_PROMPT = (
    "这是券商/同花顺『我的持仓』页截图。请提取两部分，输出 JSON 对象："
    '{"rows":[{"symbol":"6位代码（截图没有就 null）","name":"名称","quantity":持仓数量,'
    '"available":可用数量,"cost_price":成本价,"last_price":现价,"market_value":市值}],'
    '"summary":{"broker":"券商名称","total_assets":总资产,"market_value":总市值,'
    '"available":可用资金,"withdrawable":可取资金,"floating_pnl":浮动盈亏,'
    '"daily_pnl":当日参考盈亏,"daily_pnl_pct":当日盈亏百分比,"position_ratio":仓位百分比},'
    '"hint":"无法识别时的说明"}。'
    "要求："
    "1) 持仓数量是『持仓』不是『可用』；数量为 0 的清仓股也要保留一行（quantity=0），不要漏；"
    "2) 金额和数字不要带千分位、货币符号和百分号；看不清的字段填 null；"
    "3) 只提取当前账户，忽略已清仓列表、合计行和表头。"
)

TRADE_PROMPT = (
    "这是同花顺/券商『成交查询/交割单』截图。请逐行提取每笔成交，输出 JSON 对象："
    '{"rows":[{"trade_date":"YYYY-MM-DD","time":"HH:MM:SS","action":"buy 或 sell",'
    '"symbol":"6位代码","name":"名称","quantity":数量,"price":成交价,"amount":成交金额,'
    '"commission":佣金,"stamp_duty":印花税,"transfer_fee":过户费,"external_id":"成交编号"}],'
    '"hint":"无法识别时的说明"}。'
    "action 只能是 buy/sell；数字不要带千分位和货币符号；没有的字段填 null。"
)


class ScreenshotError(ValueError):
    """可展示给用户的识别错误。"""


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        text = str(value).replace(",", "").replace("，", "").replace("%", "").strip()
        text = re.sub(r"[^\d.\-]", "", text)
        return float(text)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(round(number)) if number is not None else None


def _normalize_symbol(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    return digits.zfill(6) if len(digits) <= 6 else digits[-6:]


def _normalize_date(value: Any, today: date_cls | None = None) -> str:
    today = today or date_cls.today()
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return today.isoformat()


def _normalize_time(value: Any) -> str:
    match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(value or ""))
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}:{int(match.group(3) or 0):02d}"


def _normalize_action(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in ("buy", "b", "买入", "买"):
        return "buy"
    if text in ("sell", "s", "卖出", "卖"):
        return "sell"
    if "买入" in text or "buy" in text:
        return "buy"
    if "卖出" in text or "sell" in text:
        return "sell"
    return None


def parse_model_json(text: str) -> dict:
    """从模型回复里抠出 JSON；兼容 ```json 包裹、前后废话、裸数组。"""
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"rows": value}
    except (TypeError, ValueError):
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(raw[start:end + 1])
            return value if isinstance(value, dict) else {"rows": value}
        except (TypeError, ValueError):
            pass
    start, end = raw.find("["), raw.rfind("]")
    if start >= 0 and end > start:
        try:
            return {"rows": json.loads(raw[start:end + 1])}
        except (TypeError, ValueError):
            pass
    return {}


def normalize_holdings(raw_rows: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        symbol = _normalize_symbol(raw.get("symbol") or raw.get("code"))
        name = str(raw.get("name") or "").strip()
        # 注意：数量 0 是合法的清仓行，不能用 `or` 兜底（0 会被当成假值）
        quantity_value = raw.get("quantity")
        if quantity_value is None:
            quantity_value = raw.get("hold_quantity")
        quantity = _to_int(quantity_value)
        if quantity is None or quantity < 0:
            continue
        if not symbol and not name:
            continue
        cost_value = raw.get("cost_price")
        if cost_value is None:
            cost_value = raw.get("cost")
        cost = _to_float(cost_value) or 0.0
        last_value = raw.get("last_price")
        if last_value is None:
            last_value = raw.get("price")
        last = _to_float(last_value)
        available_value = raw.get("available")
        if available_value is None:
            available_value = raw.get("available_quantity")
        if cost <= 0 and last:
            cost = last
        if quantity > 0 and cost <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "quantity": quantity,
                "available": _to_int(available_value),
                "cost_price": round(cost, 4),
                "last_price": round(last, 4) if last else None,
                "market_value": _to_float(raw.get("market_value")),
                "closing": quantity == 0,
                "status": "",
                "message": "",
                "current_quantity": None,
                "current_cost": None,
            }
        )
    return rows


def normalize_summary(raw: Any) -> dict:
    summary = raw if isinstance(raw, dict) else {}
    return {
        "broker": str(summary.get("broker") or "").strip(),
        "total_assets": _to_float(summary.get("total_assets")),
        "market_value": _to_float(summary.get("market_value")),
        "available": _to_float(summary.get("available")),
        "withdrawable": _to_float(summary.get("withdrawable")),
        "floating_pnl": _to_float(summary.get("floating_pnl")),
        "daily_pnl": _to_float(summary.get("daily_pnl")),
        "daily_pnl_pct": _to_float(summary.get("daily_pnl_pct")),
        "position_ratio": _to_float(summary.get("position_ratio")),
    }


def _broker_warning(broker: str, account: Account) -> str:
    if not broker or not account.name:
        return ""
    left, right = broker.strip(), account.name.strip()
    if left == right or left in right or right in left:
        return ""
    # 只在两边都像券商名时提示，避免自定义账户名误报
    if "证券" in left and "证券" in right:
        return f"截图券商是「{left}」，当前记账账户是「{right}」，请确认没有记错账户"
    return ""


def normalize_trades(raw_rows: list[dict], today: date_cls | None = None) -> list[dict]:
    rows: list[dict] = []
    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        symbol = _normalize_symbol(raw.get("symbol") or raw.get("code"))
        action = _normalize_action(raw.get("action") or raw.get("direction"))
        quantity = _to_int(raw.get("quantity") or raw.get("qty"))
        price = _to_float(raw.get("price") or raw.get("deal_price"))
        reason = ""
        if not action:
            reason = "未识别买卖方向"
        elif not symbol:
            reason = "缺少证券代码"
        elif not quantity or quantity <= 0:
            reason = "缺少成交数量"
        elif not price or price <= 0:
            reason = "缺少成交价格"
        fees = {
            "commission": abs(_to_float(raw.get("commission")) or 0.0),
            "stamp_duty": abs(_to_float(raw.get("stamp_duty")) or 0.0),
            "transfer_fee": abs(_to_float(raw.get("transfer_fee")) or 0.0),
            "other_fee": 0.0,
        }
        fees["total"] = round(sum(fees.values()), 2)
        rows.append(
            {
                "line": len(rows) + 1,
                "ok": not reason,
                "reason": reason,
                "status": "" if not reason else "invalid",
                "action": action,
                "symbol": symbol,
                "name": str(raw.get("name") or "").strip(),
                "quantity": quantity or 0,
                "price": round(price or 0.0, 4),
                "amount": _to_float(raw.get("amount")),
                "trade_date": _normalize_date(raw.get("trade_date") or raw.get("date"), today),
                "time": _normalize_time(raw.get("time")),
                "external_id": str(raw.get("external_id") or "").strip(),
                "fees": fees,
                "warnings": [],
            }
        )
    return rows


def _prepare_image(image_bytes: bytes) -> str:
    """把截图转成 PNG 并限制最长边，减小体积与模型 token。"""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        image = image.convert("RGB")
        width, height = image.size
        if max(width, height) > MAX_SIDE:
            image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        image.save(path, "PNG", optimize=True)
        return path
    except Exception as exc:  # noqa: BLE001 — 没有 PIL 就原样保存
        logger.debug("截图预处理失败，按原图上传: %s", exc)
        handle, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(handle, "wb") as file:
            file.write(image_bytes)
        return path


async def _recognize(image_bytes: bytes, mode: str, db: Session, model_id: int | None) -> dict:
    prompt = POSITION_PROMPT if mode == "positions" else TRADE_PROMPT
    path = _prepare_image(image_bytes)
    try:
        from src.platform.ai.ai_failover import get_configured_failover_client

        client = get_configured_failover_client(db, model_id)
        try:
            content = await asyncio.wait_for(
                client.chat(SYSTEM_PROMPT, prompt, images=[path], temperature=0),
                timeout=AI_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as exc:
            raise ScreenshotError("图片识别超时，请重试或换一张更清晰的截图") from exc
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if any(word in message.lower() for word in ("image", "vision", "multimodal", "content")):
                raise ScreenshotError(
                    "当前 AI 模型可能不支持图片，请在设置里换一个视觉模型，或改用文本/交割单导入"
                ) from exc
            raise ScreenshotError(f"图片识别失败：{message[:200]}") from exc
        return {"content": content, **parse_model_json(content)}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _annotate_positions(db: Session, rows: list[dict], account: Account) -> list[dict]:
    stock_cache: dict[str, Stock | None] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        name = str(row.get("name") or "")
        cache_key = symbol or f"name:{name}"
        if cache_key not in stock_cache:
            stock, _note = trade_ingest.resolve_stock(db, symbol, name, create=False)
            stock_cache[cache_key] = stock
        stock = stock_cache[cache_key]
        if stock is None:
            if symbol and not (row.get("closing") or int(row.get("quantity") or 0) == 0):
                # 有代码但不在自选：提交时自动创建
                row["symbol"] = symbol
                row["status"] = "new_stock"
                row["message"] = "股票不在自选，提交时创建"
            elif row.get("closing") or int(row.get("quantity") or 0) == 0:
                row["symbol"] = symbol
                row["status"] = "unchanged"
                row["message"] = "本来就无持仓"
            else:
                row["status"] = "unresolved"
                row["message"] = "截图没有代码，且名称无法匹配"
            continue
        row["symbol"] = stock.symbol
        row["name"] = row.get("name") or stock.name
        position = (
            db.query(Position)
            .filter(Position.account_id == account.id, Position.stock_id == stock.id)
            .first()
        )
        row["current_quantity"] = int(position.quantity or 0) if position is not None else None
        row["current_cost"] = round(float(position.cost_price or 0), 4) if position is not None else None

        if row.get("closing") or int(row.get("quantity") or 0) == 0:
            # 截图里持仓为 0：已清仓；PanWatch 有旧持仓就标记为清仓，提交时删除
            row["status"] = "close" if position is not None else "unchanged"
            row["message"] = "截图显示已清仓" if position is not None else "本来就无持仓"
            continue

        if position is None:
            row["status"] = "create"
            continue
        same_qty = int(position.quantity or 0) == int(row["quantity"])
        same_cost = abs(float(position.cost_price or 0) - float(row["cost_price"])) <= 0.0005
        row["status"] = "unchanged" if same_qty and same_cost else "update"
    return rows


def _summary(rows: list[dict]) -> dict:
    summary = {"total": len(rows)}
    for row in rows:
        status = row.get("status") or ("ok" if row.get("ok") else "invalid")
        summary[status] = summary.get(status, 0) + 1
    return summary


async def extract_from_image(
    image_bytes: bytes,
    *,
    mode: str,
    db: Session,
    account: Account,
    model_id: int | None = None,
) -> dict:
    """识别截图并返回预览（不写库）。"""
    mode = mode if mode in MODES else "positions"
    if not image_bytes:
        raise ScreenshotError("没有收到图片内容")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ScreenshotError("图片太大（上限 15MB），请压缩后再试")

    payload = await _recognize(image_bytes, mode, db, model_id)
    raw_rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    hint = str(payload.get("hint") or "").strip()

    account_summary: dict = {}
    broker_warning = ""
    if mode == "positions":
        rows = normalize_holdings(raw_rows)
        rows = _annotate_positions(db, rows, account)
        account_summary = normalize_summary(payload.get("summary"))
        broker_warning = _broker_warning(account_summary.get("broker") or "", account)
    else:
        rows = normalize_trades(raw_rows)
        rows = trade_ingest.annotate_rows(db, rows, account=account)

    warnings: list[str] = []
    if hint:
        warnings.append(hint)
    if broker_warning:
        warnings.append(broker_warning)
    if not rows:
        warnings.append("没有识别到有效数据行，请确认截图类型与清晰度")

    return {
        "ok": bool(rows),
        "mode": mode,
        "rows": rows,
        "summary": _summary(rows),
        "account_summary": account_summary,
        "broker_warning": broker_warning,
        "hint": hint,
        "warnings": warnings,
        "model": "",
        "account": {"id": account.id, "name": account.name},
    }


def apply_positions(
    db: Session,
    *,
    account: Account,
    rows: list[dict],
    remove_missing: bool = False,
    available_funds: float | None = None,
) -> dict:
    """把持仓快照写入持仓表。

    数量为 0 的行表示已清仓，会删除对应持仓；可选同步截图里的可用资金。
    不写交易流水。
    """
    created = updated = unchanged = closed = failed = 0
    seen: set[str] = set()
    results: list[dict] = []
    for index, row in enumerate(rows or []):
        symbol = _normalize_symbol(row.get("symbol"))
        name = str(row.get("name") or "").strip()
        quantity = _to_int(row.get("quantity"))
        cost = _to_float(row.get("cost_price"))
        if quantity is None or quantity < 0:
            failed += 1
            results.append({"row": index + 1, "status": "error", "message": "缺少持仓数量"})
            continue

        # 截图里数量为 0 且有名称/代码：已清仓，删除 PanWatch 旧持仓
        if quantity == 0:
            try:
                stock, _note = trade_ingest.resolve_stock(db, symbol, name, create=False)
                if stock is None:
                    failed += 1
                    results.append(
                        {"row": index + 1, "status": "error", "message": "清仓行无法匹配股票"}
                    )
                    continue
                seen.add(stock.symbol)
                position = (
                    db.query(Position)
                    .filter(Position.account_id == account.id, Position.stock_id == stock.id)
                    .first()
                )
                if position is not None:
                    db.delete(position)
                    db.commit()
                    closed += 1
                    status = "closed"
                    message = "截图显示已清仓，已删除原持仓"
                else:
                    unchanged += 1
                    status = "unchanged"
                    message = "本来就无持仓"
                results.append(
                    {"row": index + 1, "status": status, "symbol": stock.symbol,
                     "name": stock.name, "message": message}
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failed += 1
                results.append({"row": index + 1, "status": "error", "message": str(exc)[:200]})
            continue

        if not symbol and name:
            stock, _note = trade_ingest.resolve_stock(db, "", name, create=False)
            if stock is not None:
                symbol = stock.symbol
        if not symbol:
            failed += 1
            results.append({"row": index + 1, "status": "error", "message": "缺少代码且名称无法匹配"})
            continue
        if cost is None or cost <= 0:
            failed += 1
            results.append({"row": index + 1, "status": "error", "message": "缺少成本价"})
            continue
        seen.add(symbol)
        try:
            stock, note = trade_ingest.resolve_stock(db, symbol, name, create=True)
            if stock is None:
                raise ValueError(note or "股票无法创建")
            position = (
                db.query(Position)
                .filter(Position.account_id == account.id, Position.stock_id == stock.id)
                .first()
            )
            if position is None:
                position = Position(
                    account_id=account.id,
                    stock_id=stock.id,
                    quantity=int(quantity),
                    cost_price=round(float(cost), 4),
                    invested_amount=round(int(quantity) * float(cost), 2),
                    trading_style="swing",
                )
                db.add(position)
                created += 1
                status = "created"
            else:
                same = (
                    int(position.quantity or 0) == int(quantity)
                    and abs(float(position.cost_price or 0) - float(cost)) <= 0.0005
                )
                if same:
                    unchanged += 1
                    status = "unchanged"
                else:
                    position.quantity = int(quantity)
                    position.cost_price = round(float(cost), 4)
                    position.invested_amount = round(int(quantity) * float(cost), 2)
                    updated += 1
                    status = "updated"
            db.commit()
            results.append(
                {"row": index + 1, "status": status, "symbol": symbol, "name": stock.name}
            )
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            failed += 1
            results.append({"row": index + 1, "status": "error", "message": str(exc)[:200]})

    removed = 0
    if remove_missing and seen:
        try:
            stale = (
                db.query(Position)
                .filter(Position.account_id == account.id)
                .all()
            )
            for position in stale:
                stock = db.query(Stock).filter(Stock.id == position.stock_id).first()
                if stock is not None and stock.symbol not in seen:
                    db.delete(position)
                    removed += 1
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("删除截图中不存在的持仓失败: %s", exc)
            removed = 0

    cash_updated = False
    if available_funds is not None and available_funds >= 0:
        account.available_funds = round(float(available_funds), 2)
        db.commit()
        cash_updated = True

    return {
        "ok": failed == 0 or created + updated + unchanged + closed > 0,
        "mode": "positions",
        "total": len(rows or []),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "closed": closed,
        "failed": failed,
        "removed": removed,
        "cash_updated": cash_updated,
        "results": results,
    }
