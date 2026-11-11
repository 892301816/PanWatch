"""策略卡 API、策略模板、买卖前体检与助手工具。"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.platform.persistence.models  # noqa: F401  注册 ORM 模型
from src.platform.persistence.database import Base


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _check(**overrides):
    base = {
        "symbol": "002943",
        "name": "高开低走不追高",
        "verdict": "禁止买入",
        "has_playbook": True,
        "buy_hits": 0,
        "buy_total": 1,
        "sell_hits": 0,
        "sell_total": 1,
        "avoid_hits": 1,
        "avoid_total": 2,
        "buy_checks": [
            {
                "field": "runup_3d",
                "label": "近3日涨跌幅",
                "op": "between",
                "expect": "-5 ~ 0%",
                "actual": "-5.83%",
                "ok": False,
                "text": "近3日涨跌幅 介于 -5% ~ 0%",
            }
        ],
        "sell_checks": [
            {
                "field": "profit_ratio",
                "label": "获利盘(近似)",
                "op": "gte",
                "expect": "≥ 95%",
                "actual": "+41.68%",
                "ok": False,
                "text": "获利盘(近似) ≥ 95%",
            }
        ],
        "avoid_checks": [
            {
                "field": "gap_drop_days",
                "label": "高开低走未修复天数",
                "op": "gte",
                "expect": "≥ 1天",
                "actual": "2 天",
                "ok": True,
                "text": "高开低走未修复天数 ≥ 1天",
            }
        ],
        "pattern": {
            "tags": ["高开低走未修复(2天)", "放量"],
            "summary": "2026-11-09 高开+8.1% 后收跌-2.9%，此后 2 个交易日最高只回到 -9.2%。",
            "risk_hint": "放量高开低走且至今未修复：追高筹码被套。",
        },
        "price": 29.09,
        "as_of": "2026-11-11",
        "estimated": False,
    }
    base.update(overrides)
    return base


def test_strategy_routes_mounted():
    from src.bootstrap.application import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/strategy/playbooks" in paths
    assert "/api/strategy/presets" in paths
    assert "/api/strategy/gate" in paths
    assert "/api/strategy/backtest" in paths
    assert "/api/strategy/check" in paths


def test_presets_endpoint():
    from src.modules.strategy.api import playbooks as api

    data = api.list_presets()
    assert data["presets"]
    ids = {p["id"] for p in data["presets"]}
    assert {"three_day_dip", "gap_drop_guard"} <= ids
    assert data["fields"]


def test_playbook_roundtrip_with_avoid():
    from src.modules.strategy.api import playbooks as api

    engine, db = _db()
    try:
        payload = api.PlaybookIn(
            scope="stock",
            symbol="002943",
            name="宇晶策略",
            buy=[{"field": "runup_3d", "op": "between", "value": [-5, 0]}],
            sell=[{"field": "profit_ratio", "op": "gte", "value": 95}],
            avoid=[{"field": "gap_drop_days", "op": "gte", "value": 1}],
        )
        saved = api.save_playbook(payload, db=db)
        assert saved["avoid"][0]["field"] == "gap_drop_days"

        listed = api.list_playbooks(db=db)
        assert listed["items"][0]["avoid"] == saved["avoid"]
        assert listed["defaults"]["avoid"] == []
        assert listed["presets"]
    finally:
        db.close()
        engine.dispose()


def test_playbook_requires_symbol_for_stock_scope():
    from src.modules.strategy.api import playbooks as api

    engine, db = _db()
    try:
        with_api = api.PlaybookIn(scope="stock", symbol="")
        try:
            api.save_playbook(with_api, db=db)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:  # pragma: no cover - 明确失败比静默通过好
            raise AssertionError("缺少 symbol 应当返回 400")
    finally:
        db.close()
        engine.dispose()


def test_gate_endpoint_blocks_when_rules_fail(monkeypatch):
    from src.modules.strategy import gate as gate_mod
    from src.modules.strategy.api import playbooks as api

    engine, db = _db()
    try:
        monkeypatch.setattr(gate_mod.playbooks, "check_symbol", lambda *a, **k: _check())
        result = asyncio.run(
            api.trade_gate(api.GateIn(symbol="002943", side="buy", price=29.09, use_ai=False), db=db)
        )
        assert result["verdict"] == "不建议"
        assert result["rule_verdict"] == "不建议"
        assert result["pattern"]["tags"]
        assert any("风险命中" in reason for reason in result["reasons"])
        assert result["ai"] is None
    finally:
        db.close()
        engine.dispose()


def test_gate_endpoint_ai_downgrades(monkeypatch):
    from src.modules.strategy import gate as gate_mod
    from src.modules.strategy.api import playbooks as api

    ok_check = _check(
        verdict="符合买入",
        buy_hits=1,
        avoid_hits=0,
        avoid_checks=[],
        buy_checks=[
            {
                "field": "runup_3d",
                "label": "近3日涨跌幅",
                "op": "between",
                "expect": "-5 ~ 0%",
                "actual": "-1.00%",
                "ok": True,
                "text": "近3日涨跌幅 介于 -5% ~ 0%",
            }
        ],
    )

    async def fake_ai(*args, **kwargs):
        return {
            "enabled": True,
            "ok": True,
            "tone": "反对",
            "verdict": "反对",
            "reason": "放量高开低走未修复",
            "risk": "追高易套",
            "content": "",
            "elapsed_ms": 3,
            "error": "",
        }

    engine, db = _db()
    try:
        monkeypatch.setattr(gate_mod.playbooks, "check_symbol", lambda *a, **k: ok_check)
        monkeypatch.setattr(gate_mod, "_ai_analyze", fake_ai)
        gate_mod._GATE_CACHE.clear()
        result = asyncio.run(
            api.trade_gate(api.GateIn(symbol="002943", side="buy", price=30.0), db=db)
        )
        assert result["rule_verdict"] == "可操作"
        assert result["verdict"] == "不建议"
        assert result["ai"]["tone"] == "反对"
    finally:
        db.close()
        engine.dispose()


def test_assistant_tool_includes_avoid_and_pattern(monkeypatch):
    from pan_agent import ModelMessage, RunRequest

    import src.modules.assistant.tools as assistant_tools
    from src.modules.strategy import playbooks as strategy_playbooks

    monkeypatch.setattr(strategy_playbooks, "check_symbol", lambda *a, **k: _check())
    engine, db = _db()
    try:
        registry = assistant_tools.build_panwatch_tool_registry(db)
        result = asyncio.run(
            registry.execute(
                "check_trade_strategy",
                RunRequest(run_id="t", messages=[ModelMessage(role="user", content="能买吗")]),
                {"symbol": "002943", "market": "CN"},
            )
        )
        assert result.ok is True
        assert "禁止买入" in result.summary
        assert "禁买命中" in result.summary
        assert "形态：" in result.summary
    finally:
        db.close()
        engine.dispose()
