"""自然语言 → 选股条件。

设计原则:AI 只负责「翻译」,不负责「执行」。返回的条件会逐条校验字段与算子,
非法项被丢弃并给出 warning,用户在前端看到的是结构化 chips,可改可删。
这样即使模型胡编字段,也不会把错误条件悄悄带进结果。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from src.modules.screener.constants import FIELD_KEYS, OPS

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "screener_parse.txt"

_NUMERIC_OPS = {"gt", "gte", "lt", "lte", "between"}
_LIST_OPS = {"in", "not_in"}
_DEFAULT = {
    "conditions": [],
    "logic": "all",
    "exclude": ["st"],
    "order_by": "change_pct desc",
    "limit": 100,
    "explanation": "",
}


def _extract_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:  # noqa: BLE001
            return None


def _coerce_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _validate_condition(cond: dict, warnings: list[str]) -> dict | None:
    field = str(cond.get("field") or "").strip()
    op = str(cond.get("op") or "").strip()
    value = cond.get("value")

    if field not in FIELD_KEYS:
        warnings.append(f"忽略未知字段 {field or '(空)'}")
        return None
    if op not in OPS:
        warnings.append(f"{field} 使用了不支持的算子 {op or '(空)'}")
        return None

    if field == "concept":
        if op not in ("in", "not_in", "contains"):
            op = "contains"
        if op == "contains":
            value = str(value or "").strip()
        elif isinstance(value, str):
            value = [value]
        elif not isinstance(value, list):
            value = []
        if not value:
            warnings.append("concept 条件缺少值")
            return None
        return {"field": field, "op": op, "value": value}

    if field == "industry":
        return {"field": field, "op": op if op in ("eq", "ne", "contains") else "contains",
                "value": str(value or "").strip()}

    if field == "is_st":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "是", "yes")
        return {"field": field, "op": "eq" if op not in ("eq", "ne") else op, "value": bool(value)}

    if op in _LIST_OPS:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value:
            warnings.append(f"{field} 的 in 条件缺少值")
            return None
        return {"field": field, "op": op, "value": value}

    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            warnings.append(f"{field} 的 between 需要两个数值")
            return None
        lo, hi = _coerce_number(value[0]), _coerce_number(value[1])
        if lo is None or hi is None:
            warnings.append(f"{field} 的 between 值不是数字")
            return None
        return {"field": field, "op": op, "value": [min(lo, hi), max(lo, hi)]}

    num = _coerce_number(value)
    if num is None:
        warnings.append(f"{field} 的条件值不是数字")
        return None
    return {"field": field, "op": op, "value": num}


def validate_payload(obj: dict) -> dict:
    warnings: list[str] = []
    raw_conditions = obj.get("conditions")
    if not isinstance(raw_conditions, list):
        raw_conditions = []

    conditions = []
    for cond in raw_conditions[:12]:
        if not isinstance(cond, dict):
            continue
        cleaned = _validate_condition(cond, warnings)
        if cleaned:
            conditions.append(cleaned)

    logic = "any" if str(obj.get("logic") or "").strip().lower() == "any" else "all"
    # 只有一条条件时「或」与「且」等价,统一按「且」处理,避免误把隐藏状态带到后续运行
    if logic == "any" and len(conditions) < 2:
        logic = "all"

    exclude = obj.get("exclude")
    exclude = [str(x).lower() for x in exclude] if isinstance(exclude, list) else ["st"]
    exclude = [x for x in dict.fromkeys(exclude) if x in ("st", "suspended")] or ["st"]

    order_by = str(obj.get("order_by") or "change_pct desc").strip()
    parts = order_by.split()
    field = parts[0] if parts else "change_pct"
    direction = parts[1].lower() if len(parts) > 1 else "desc"
    if field not in FIELD_KEYS:
        field = "change_pct"
    if direction not in ("asc", "desc"):
        direction = "desc"
    order_by = f"{field} {direction}"

    try:
        limit = int(obj.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 300))

    return {
        "conditions": conditions,
        "logic": logic,
        "exclude": exclude,
        "order_by": order_by,
        "limit": limit,
        "explanation": str(obj.get("explanation") or "").strip()[:120],
        "warnings": warnings,
        "ok": bool(conditions),
    }


async def parse_text(db, text: str, model_id: int | None = None) -> dict:
    """调用已配置的模型做一次翻译。失败抛异常,由 API 层转成 4xx/5xx。"""
    from src.platform.ai.ai_failover import get_configured_failover_client

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    client = get_configured_failover_client(db, model_id)
    raw = await client.chat(system_prompt, text.strip(), temperature=0.1)
    obj = _extract_json(raw)
    if obj is None:
        logger.warning(f"[选股] AI 未返回合法 JSON: {str(raw)[:200]}")
        return {**_DEFAULT, "ok": False, "warnings": ["模型返回格式无法解析"], "raw": str(raw)[:400]}
    result = validate_payload(obj)
    result["model"] = getattr(client, "used_model_label", "") or ""
    return result
