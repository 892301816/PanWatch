"""买卖前 AI 体检(gate)。

把「规则检查」和「AI 对当前形态的判断」合成一个结论,给交易弹窗用:

- **规则优先**:策略卡不满足 / 禁买条件命中 → 直接不建议,AI 不能把它抬回可操作;
- **AI 只能降级,不能升级**:规则满足时,AI 可以因为形态风险(放量高开低走未修复、
  冲高回落、破位等)把结论下调为「接近」或「不建议」;
- 没配 AI 也能用:只返回规则结论 + 形态诊断,不会报错;
- 结果按「标的 + 方向 + 日期 + 价格档 + 策略指纹」缓存数分钟,重复打开弹窗不再等 AI。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from sqlalchemy.orm import Session

from src.modules.strategy import playbooks
from src.modules.strategy.patterns import compute_features, diagnose
from src.platform.marketdata.collectors.market_http import TTLCache

logger = logging.getLogger(__name__)

# 结论排序:数字越大越「可操作」;AI 只能往下调
_VERDICT_RANK = {"不建议": 0, "接近": 1, "可操作": 2}
_AI_TONE_RANK = {"反对": 0, "谨慎": 1, "支撑": 2}
_AI_TONE_VERDICT = {0: "不建议", 1: "接近", 2: "可操作"}
_RANK_VERDICT = {0: "不建议", 1: "接近", 2: "可操作"}

SIDE_LABEL = {"buy": "买入", "sell": "卖出"}

_GATE_CACHE = TTLCache(default_ttl_sec=180, max_size=512)
GATE_TTL_SEC = 180
AI_TIMEOUT_SEC = 25.0


def _cache_key(symbol: str, side: str, price: float | None, check: dict) -> str:
    fingerprint = hashlib.md5(
        json.dumps(
            {
                "buy": check.get("buy_checks"),
                "sell": check.get("sell_checks"),
                "avoid": check.get("avoid_checks"),
                "name": check.get("name"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:10]
    bucket = round(float(price or 0) * 10) / 10
    return f"{symbol}:{side}:{check.get('as_of')}:{bucket}:{fingerprint}"


def _rule_verdict(check: dict, side: str) -> str | None:
    """纯规则结论;策略缺对应方向的条件时返回 None(交给 AI / 显示无策略)。"""
    if not check.get("has_playbook"):
        return None
    buy_total = int(check.get("buy_total") or 0)
    sell_total = int(check.get("sell_total") or 0)
    buy_hits = int(check.get("buy_hits") or 0)
    sell_hits = int(check.get("sell_hits") or 0)
    avoid_total = int(check.get("avoid_total") or 0)
    avoid_hits = int(check.get("avoid_hits") or 0)

    if side == "sell":
        if sell_total == 0:
            return None
        if sell_hits > 0:
            return "可操作"
        if sell_hits / sell_total >= 0.5:
            return "接近"
        return "不建议"

    # 买入
    if avoid_total and avoid_hits > 0:
        return "不建议"
    if buy_total == 0:
        return None
    if buy_hits == buy_total:
        return "可操作"
    if buy_hits / buy_total >= 2 / 3:
        return "接近"
    return "不建议"


def _rule_reasons(check: dict, side: str) -> list[str]:
    """把逐条对照翻成人话,最多 4 条,便于弹窗直接展示。"""
    reasons: list[str] = []
    if side == "buy":
        for item in check.get("buy_checks") or []:
            if not item.get("ok"):
                reasons.append(
                    f"未满足：{item.get('text') or item.get('label')}（当前 {item.get('actual')}）"
                )
        for item in check.get("avoid_checks") or []:
            if item.get("ok"):
                reasons.append(
                    f"风险命中：{item.get('text') or item.get('label')}（当前 {item.get('actual')}）"
                )
        if not reasons and check.get("buy_checks"):
            reasons.append("买入条件全部满足")
    else:
        for item in check.get("sell_checks") or []:
            if item.get("ok"):
                reasons.append(
                    f"卖出命中：{item.get('text') or item.get('label')}（当前 {item.get('actual')}）"
                )
        if not reasons and check.get("sell_checks"):
            first = check["sell_checks"][0]
            reasons.append(
                f"卖出条件未命中（{first.get('text') or first.get('label')}，"
                f"当前 {first.get('actual')}）"
            )
    return reasons[:4]


def _ai_prompt(check: dict, side: str, price: float | None, quantity: float | None) -> tuple[str, str]:
    label = SIDE_LABEL.get(side, side)
    lines: list[str] = []
    lines.append(f"标的：{check.get('symbol')} {check.get('name') or ''}".strip())
    if price:
        qty = f" × {quantity:g} 股" if quantity else ""
        lines.append(f"拟操作：{label} @ {price:.2f}{qty}")
    else:
        lines.append(f"拟操作：{label}")

    rules: list[str] = []
    if check.get("buy_checks"):
        rules.append("买入条件（需全部满足）：")
        for item in check["buy_checks"]:
            rules.append(f"  {'✓' if item.get('ok') else '✗'} {item.get('text')}（当前 {item.get('actual')}）")
    if check.get("sell_checks"):
        rules.append("卖出条件（任一满足）：")
        for item in check["sell_checks"]:
            rules.append(f"  {'✓' if item.get('ok') else '✗'} {item.get('text')}（当前 {item.get('actual')}）")
    if check.get("avoid_checks"):
        rules.append("禁买条件（任一命中即不买）：")
        for item in check["avoid_checks"]:
            rules.append(f"  {'✗命中' if item.get('ok') else '✓未命中'} {item.get('text')}（当前 {item.get('actual')}）")
    lines.append("\n".join(rules) if rules else "策略：未配置条件")

    pattern = check.get("pattern") or {}
    if pattern.get("tags"):
        lines.append("形态标签：" + "、".join(pattern["tags"]))
    if pattern.get("summary"):
        lines.append("形态说明：" + pattern["summary"])
    if pattern.get("risk_hint"):
        lines.append("形态风险：" + pattern["risk_hint"])

    rules_verdict = _rule_verdict(check, side) or "无规则结论"
    lines.append(f"规则检查结论：{rules_verdict}（买入 {check.get('buy_hits')}/{check.get('buy_total')}，"
                 f"卖出 {check.get('sell_hits')}/{check.get('sell_total')}，"
                 f"禁买命中 {check.get('avoid_hits')}/{check.get('avoid_total')}）")

    system = (
        "你是 A 股交易前的风控检查员。用户给你一张策略卡的逐条检查结果、当前形态量价诊断，"
        f"以及他准备执行的「{label}」。请判断：当前形态是否支持这次操作。\n"
        "硬性规则：\n"
        "1. 规则结论是硬约束：规则不满足时，你的结论不能是「支撑」；你只能维持或下调。\n"
        "2. 特别警惕：放量高开低走、高开低走未修复、冲高回落、放量滞涨、连破均线等出货形态。\n"
        "3. 只依据给出的数据，不臆造消息面与基本面，不做收益承诺，不给具体买卖价格。\n"
        "只输出下面三行，不要多余内容：\n"
        "结论：支撑/谨慎/反对\n"
        "理由：（不超过 60 字，引用具体数值）\n"
        "风险：（不超过 30 字，一句话）"
    )
    return system, "\n".join(lines)


def _parse_ai(text: str) -> dict:
    """解析 AI 三行输出;解析不出来时按全文关键词兜底。"""
    content = (text or "").strip()
    verdict = ""
    reason = ""
    risk = ""
    for raw in content.splitlines():
        line = raw.strip().lstrip("-*· ").strip()
        if not line:
            continue
        if line.startswith("结论") or line.startswith("判断"):
            verdict = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith("理由"):
            reason = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith("风险"):
            risk = line.split("：", 1)[-1].split(":", 1)[-1].strip()

    tone = ""
    head = verdict or content[:120]
    if any(k in head for k in ("反对", "不建议", "不适合", "不满足", "风险大")):
        tone = "反对"
    elif any(k in head for k in ("谨慎", "观望", "部分", "中性", "小心")):
        tone = "谨慎"
    elif any(k in head for k in ("支撑", "支持", "适合", "可以", "可操作", "满足")):
        tone = "支撑"
    return {"tone": tone, "reason": reason or (content[:80] if content else ""), "risk": risk}


def _ai_configured(db: Session | None) -> bool:
    """是否配置了可用的 AI 模型(库里的模型或环境变量)。"""
    try:
        from src.platform.persistence.database import SessionLocal
        from src.platform.persistence.models import AIModel

        own = db is None
        session = db or SessionLocal()
        try:
            if session.query(AIModel).count() > 0:
                return True
        finally:
            if own:
                session.close()
    except Exception:  # noqa: BLE001 — 查库失败时退回环境变量判断
        pass
    try:
        from src.platform.runtime.config import Settings

        return bool(getattr(Settings(), "ai_api_key", "") or "")
    except Exception:  # noqa: BLE001
        return False


async def _ai_analyze(
    check: dict,
    side: str,
    price: float | None,
    quantity: float | None,
    *,
    db: Session | None,
    model_id: int | None,
) -> dict:
    started = time.perf_counter()
    if not _ai_configured(db):
        return {
            "enabled": False,
            "ok": False,
            "tone": "",
            "verdict": "未启用",
            "reason": "",
            "risk": "",
            "content": "",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": "未配置 AI 模型",
        }
    system, user = _ai_prompt(check, side, price, quantity)
    own_session = db is None
    session = db
    try:
        from src.platform.persistence.database import SessionLocal
        from src.platform.ai.ai_failover import get_configured_failover_client

        session = db or SessionLocal()
        client = get_configured_failover_client(session, model_id)
        content = await asyncio.wait_for(
            client.chat(system, user, temperature=0.2), timeout=AI_TIMEOUT_SEC
        )
        parsed = _parse_ai(content)
        return {
            "enabled": True,
            "ok": bool(parsed["tone"]),
            "tone": parsed["tone"],
            "verdict": {"支撑": "支持操作", "谨慎": "谨慎", "反对": "反对"}.get(parsed["tone"], "未识别"),
            "reason": parsed["reason"],
            "risk": parsed["risk"],
            "content": content,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": "",
        }
    except asyncio.TimeoutError:
        logger.warning("[策略体检] AI 超时 %s", check.get("symbol"))
        return {
            "enabled": True,
            "ok": False,
            "tone": "",
            "verdict": "未返回",
            "reason": "",
            "risk": "",
            "content": "",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": "AI 分析超时",
        }
    except Exception as exc:  # noqa: BLE001 — 未配置 AI 也要能出规则结论
        logger.info("[策略体检] AI 不可用 %s: %s", check.get("symbol"), exc)
        return {
            "enabled": True,
            "ok": False,
            "tone": "",
            "verdict": "未返回",
            "reason": "",
            "risk": "",
            "content": "",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc)[:120],
        }
    finally:
        if own_session and session is not None:
            session.close()


def _summary(check: dict, side: str, verdict: str, rule: str | None, ai: dict | None, reasons: list[str]) -> str:
    label = SIDE_LABEL.get(side, side)
    if verdict == "可操作":
        text = f"策略满足，可以{label}"
    elif verdict == "接近":
        text = f"接近满足，{label}可再等一等"
    elif verdict == "不建议":
        text = f"不建议{label}"
    elif not check.get("has_playbook"):
        text = "未配置策略，先按形态判断"
    else:
        text = f"{label}：策略未覆盖这个方向"
    if rule and verdict != rule:
        text += "（AI 下调）"
    pattern = check.get("pattern") or {}
    if side == "buy" and pattern.get("risk_hint"):
        text += "；" + pattern["risk_hint"]
    elif reasons:
        text += "；" + reasons[0]
    return text


async def evaluate(
    symbol: str,
    *,
    side: str = "buy",
    market: str = "CN",
    price: float | None = None,
    quantity: float | None = None,
    use_ai: bool = True,
    model_id: int | None = None,
    db: Session | None = None,
) -> dict:
    """买卖前体检:规则 + 形态 + AI,返回给前端直接可渲染的结构。"""
    symbol = str(symbol or "").strip()
    side = "sell" if str(side).lower() == "sell" else "buy"
    started = time.perf_counter()
    check = await asyncio.to_thread(
        playbooks.check_symbol, symbol, db, allow_network=True, market=market
    )

    rule = _rule_verdict(check, side)
    reasons = _rule_reasons(check, side)

    cache_key = _cache_key(symbol, side, price, check)
    ai: dict | None = None
    cached = False
    if use_ai:
        ai = _GATE_CACHE.get(cache_key)
        if ai is not None:
            cached = True
        else:
            ai = await _ai_analyze(check, side, price, quantity, db=db, model_id=model_id)
            if ai.get("ok"):
                _GATE_CACHE.set(cache_key, ai, ttl_sec=GATE_TTL_SEC)

    ai_rank = _AI_TONE_RANK.get((ai or {}).get("tone") or "")
    if rule is None:
        verdict = _AI_TONE_VERDICT.get(ai_rank, "无策略") if ai_rank is not None else "无策略"
    elif ai_rank is None:
        verdict = rule
    else:
        verdict = _RANK_VERDICT[min(_VERDICT_RANK.get(rule, 1), ai_rank)]

    bars_note = ""
    if check.get("verdict") == "数据不足":
        verdict = "无策略"
        bars_note = "日K不足 60 根，无法检查"

    return {
        "symbol": symbol,
        "name": check.get("name") or symbol,
        "market": market,
        "side": side,
        "verdict": verdict,
        "rule_verdict": rule,
        "summary": _summary(check, side, verdict, rule, ai, reasons) if not bars_note else bars_note,
        "reasons": reasons,
        "has_playbook": bool(check.get("has_playbook")),
        "playbook_name": check.get("name") or "",
        "playbook_note": check.get("note") or "",
        "buy_checks": check.get("buy_checks") or [],
        "sell_checks": check.get("sell_checks") or [],
        "avoid_checks": check.get("avoid_checks") or [],
        "pattern": check.get("pattern") or {},
        "price": price if price else check.get("price"),
        "as_of": check.get("as_of"),
        "estimated": bool(check.get("estimated")),
        "ai": ai,
        "cached": cached,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def quick_pattern(symbol: str, *, db: Session | None = None) -> dict:
    """只要形态诊断(不跑 AI、不读策略),给其它模块复用。"""
    bars = playbooks._current_bars(symbol)
    if len(bars) < 60:
        return {"tags": [], "summary": "日K不足，无法判断", "risk_hint": "", "features": {}}
    features = compute_features(bars, len(bars) - 1)
    pattern = diagnose(bars, len(bars) - 1, features)
    return {**pattern, "features": features}
