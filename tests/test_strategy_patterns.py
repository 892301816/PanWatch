"""形态特征、禁买规则、买卖前体检。

用 2026-11-09 宇晶股份(002943) 的真实盘面做参照:
涨了一段后放量高开低走,次日只弱修复,第 3 天继续跌 —— 典型的「未修复弱势」。
"""
import asyncio
import json

import pytest
from sqlalchemy import text

from src.modules.strategy import gate as G
from src.modules.strategy import playbooks as P
from src.modules.strategy.backtest import single as S
from src.modules.strategy.patterns import (
    FEATURES,
    compute_features,
    diagnose,
    gap_drop_event,
    unrepaired_gap_drop,
)
from src.platform.persistence.database import engine


def _bar(date, o, h, l, c, v):
    return S.Bar(date, o, h, l, c, v)


def _trend_bars(n=60, start=10.0, step=0.01):
    """温和上涨的底仓,保证量能比/均线有定义(≥60 根才能体检/回测)。"""
    out = []
    price = start
    for i in range(n):
        price *= 1 + step
        month = 7 + i // 28
        day = i % 28 + 1
        out.append(
            _bar(f"2026-{month:02d}-{day:02d}", price * 0.995, price * 1.01, price * 0.99, price, 1000)
        )
    return out


def _yu_jing_bars():
    """复刻 002943:11-06 收 30.89,11-09 高开 33.38 收 30.00,11-10 收 30.30,11-11 收 29.09。"""
    bars = _trend_bars(60, start=22.0, step=0.012)
    bars += [
        _bar("2026-11-04", 25.66, 28.20, 24.70, 27.58, 2550),
        _bar("2026-11-05", 27.32, 30.28, 27.00, 29.38, 2250),
        _bar("2026-11-06", 28.21, 31.50, 28.12, 30.89, 2580),
        _bar("2026-11-09", 33.38, 33.98, 29.74, 30.00, 3070),
        _bar("2026-11-10", 30.02, 30.76, 29.64, 30.30, 1920),
        _bar("2026-11-11", 30.00, 30.68, 27.93, 29.09, 2650),
    ]
    return bars


def test_gap_drop_event_detected():
    bars = _yu_jing_bars()
    idx = len(bars) - 3  # 11-09
    event = gap_drop_event(bars, idx)
    assert event is not None
    assert event["date"] == "2026-11-09"
    assert event["gap_pct"] > 7
    assert event["body_pct"] < -9
    assert event["vol_ratio"] >= 1.2


def test_unrepaired_gap_drop_counts_days():
    bars = _yu_jing_bars()
    event = unrepaired_gap_drop(bars, len(bars) - 1)
    assert event is not None
    assert event["date"] == "2026-11-09"
    assert event["days"] == 2
    assert event["repair_pct"] < 0
    features = compute_features(bars, len(bars) - 1)
    assert features["gap_drop_days"] == 2
    assert features["gap_drop_flag"] == 0


def test_repair_clears_unrepaired():
    bars = _yu_jing_bars()
    # 追加一根收复开盘价 33.38 的阳线
    bars.append(_bar("2026-11-12", 29.5, 34.0, 29.4, 33.9, 3200))
    assert unrepaired_gap_drop(bars, len(bars) - 1) is None
    assert compute_features(bars, len(bars) - 1)["gap_drop_days"] == 0


def test_features_and_diagnose_cover_catalog():
    bars = _yu_jing_bars()
    idx = len(bars) - 1
    features = compute_features(bars, idx)
    assert set(features) == {f["key"] for f in FEATURES}
    assert features["runup_10d"] is not None
    assert features["gap_drop_days"] == 2
    diag = diagnose(bars, idx, features)
    assert any("未修复" in tag for tag in diag["tags"])
    assert "未修复" in diag["summary"]
    assert diag["risk_hint"]


@pytest.fixture()
def avoid_playbook():
    row = P.save_playbook(
        {
            "scope": "global",
            "name": "禁买测试",
            "buy": [{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
            "sell": [{"field": "profit_ratio", "op": "gte", "value": 101}],
            "avoid": [{"field": "gap_drop_days", "op": "gte", "value": 1}],
        }
    )
    yield row
    P.delete_playbook(row["id"])


def test_check_symbol_blocks_on_avoid(avoid_playbook, monkeypatch):
    monkeypatch.setattr(P, "_current_bars", lambda *a, **k: _yu_jing_bars())
    data = P.check_symbol("002943")
    assert data["verdict"] == "禁止买入"
    assert data["avoid_hits"] == 1
    assert any(c["ok"] for c in data["avoid_checks"])


def test_backtest_avoid_blocks_buys(monkeypatch):
    bars = _yu_jing_bars()
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: bars)
    base = S.run_single_backtest(
        "002943",
        buy=[{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
        sell=[{"field": "profit_ratio", "op": "gte", "value": 0}],
        days=120,
    )
    blocked = S.run_single_backtest(
        "002943",
        buy=[{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
        sell=[{"field": "profit_ratio", "op": "gte", "value": 0}],
        avoid=[{"field": "gap_drop_days", "op": "gte", "value": 999}],
        days=120,
    )
    assert base["trade_count"] >= 1
    # avoid 永不命中 → 与基准一致
    assert blocked["trade_count"] == base["trade_count"]
    assert blocked["rules"]["avoid"][0]["field"] == "gap_drop_days"

    # avoid 每天都命中 → 一次都不该买
    never = S.run_single_backtest(
        "002943",
        buy=[{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
        sell=[{"field": "profit_ratio", "op": "gte", "value": 0}],
        avoid=[{"field": "runup_3d", "op": "gte", "value": -999}],
        days=120,
    )
    assert never["trade_count"] == 0
    assert never["current_status"] == "观望中"
    assert "没有触发任何交易" in never["explain"]


def test_backtest_metrics_and_explain(monkeypatch):
    bars = _yu_jing_bars()
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: bars)
    result = S.run_single_backtest(
        "002943",
        buy=[{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
        sell=[{"field": "profit_ratio", "op": "gte", "value": 0}],
        days=120,
    )
    assert result["metrics"]["max_drawdown"] <= 0
    assert "rule_text" in result and "买入" in result["rule_text"]
    assert result["explain"]
    assert result["curve"] and "buy" in result["curve"][0]


def _fake_check(**overrides):
    base = {
        "symbol": "002943",
        "name": "测试策略",
        "verdict": "观察",
        "has_playbook": True,
        "buy_hits": 1,
        "buy_total": 1,
        "sell_hits": 0,
        "sell_total": 1,
        "avoid_hits": 0,
        "avoid_total": 1,
        "buy_checks": [
            {"field": "runup_3d", "label": "近3日涨跌幅", "op": "between", "expect": "-5 ~ 0%", "actual": "-1.00%", "ok": True, "text": "近3日涨跌幅 介于 -5% ~ 0%"}
        ],
        "sell_checks": [
            {"field": "profit_ratio", "label": "获利盘(近似)", "op": "gte", "expect": "≥ 95%", "actual": "+40.00%", "ok": False, "text": "获利盘(近似) ≥ 95%"}
        ],
        "avoid_checks": [
            {"field": "gap_drop_days", "label": "高开低走未修复天数", "op": "gte", "expect": "≥ 1天", "actual": "0 天", "ok": False, "text": "高开低走未修复天数 ≥ 1天"}
        ],
        "pattern": {"tags": [], "summary": "", "risk_hint": ""},
        "price": 30.0,
        "as_of": "2026-11-11",
        "estimated": False,
    }
    base.update(overrides)
    return base


def test_gate_rule_verdicts_without_ai(monkeypatch):
    monkeypatch.setattr(G.playbooks, "check_symbol", lambda *a, **k: _fake_check())
    buy = asyncio.run(G.evaluate("002943", side="buy", use_ai=False))
    assert buy["verdict"] == "可操作"
    assert buy["ai"] is None

    monkeypatch.setattr(
        G.playbooks, "check_symbol", lambda *a, **k: _fake_check(buy_hits=0, verdict="观察")
    )
    blocked = asyncio.run(G.evaluate("002943", side="buy", use_ai=False))
    assert blocked["verdict"] == "不建议"
    assert blocked["reasons"]

    blocked_check = _fake_check(verdict="禁止买入", avoid_hits=1)
    blocked_check["avoid_checks"][0]["ok"] = True
    monkeypatch.setattr(G.playbooks, "check_symbol", lambda *a, **k: blocked_check)
    risk = asyncio.run(G.evaluate("002943", side="buy", use_ai=False))
    assert risk["verdict"] == "不建议"
    assert any("风险命中" in r for r in risk["reasons"])


def test_gate_ai_can_only_downgrade(monkeypatch):
    monkeypatch.setattr(G.playbooks, "check_symbol", lambda *a, **k: _fake_check())

    async def fake_ai(*args, **kwargs):
        return {
            "enabled": True,
            "ok": True,
            "tone": "反对",
            "verdict": "反对",
            "reason": "放量高开低走未修复",
            "risk": "追高易套",
            "content": "",
            "elapsed_ms": 1,
            "error": "",
        }

    monkeypatch.setattr(G, "_ai_analyze", fake_ai)
    G._GATE_CACHE.clear()
    result = asyncio.run(G.evaluate("002943", side="buy", use_ai=True, price=30.0))
    assert result["rule_verdict"] == "可操作"
    assert result["verdict"] == "不建议"

    async def fake_ai_up(*args, **kwargs):
        return {
            "enabled": True,
            "ok": True,
            "tone": "支撑",
            "verdict": "支持操作",
            "reason": "",
            "risk": "",
            "content": "",
            "elapsed_ms": 1,
            "error": "",
        }

    monkeypatch.setattr(G, "_ai_analyze", fake_ai_up)
    monkeypatch.setattr(
        G.playbooks, "check_symbol", lambda *a, **k: _fake_check(buy_hits=0, verdict="观察")
    )
    G._GATE_CACHE.clear()
    result = asyncio.run(G.evaluate("002943", side="buy", use_ai=True, price=31.0))
    assert result["verdict"] == "不建议"  # 规则不满足,AI 不能抬上去


def test_presets_are_valid():
    keys = {f["key"] for f in FEATURES}
    assert P.PRESETS
    for preset in P.PRESETS:
        assert preset["name"] and preset["description"]
        for side in ("buy", "sell", "avoid"):
            for cond in preset[side]:
                assert cond["field"] in keys, (preset["id"], cond)
                assert cond["op"] in {"eq", "ne", "gt", "gte", "lt", "lte", "between", "in", "not_in"}
        # 可 JSON 序列化(直接存库/回传前端)
        json.dumps(preset, ensure_ascii=False)
    drip = next(p for p in P.PRESETS if p["id"] == "three_day_dip")
    assert drip["buy"][0]["field"] == "runup_3d"
    assert drip["sell"][0]["field"] == "profit_ratio"
    assert drip["sell"][0]["value"] == 95


def test_avoid_column_migrated():
    with engine.begin() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(strategy_playbooks)"))]
    assert "avoid_conditions" in columns


def test_current_bars_ignores_stale_quote(monkeypatch):
    """收盘后/周末取到的旧快照不能补成「今天」的一根K线。"""
    from datetime import date as real_date

    import src.platform.marketdata.marketdata_client as mdc

    bars = _yu_jing_bars()
    monkeypatch.setattr(P, "load_bars", lambda *a, **k: list(bars))

    class _FakeDate:
        @staticmethod
        def today():
            return real_date(2026, 11, 12)

    monkeypatch.setattr(P, "date", _FakeDate)

    # 旧快照:prev_close 是 11-10 的收盘,OHLC 与最后一根 11-11 完全一致
    stale = {
        "current_price": 29.09,
        "open_price": 30.0,
        "high_price": 30.68,
        "low_price": 27.93,
        "prev_close": 30.30,
        "volume": 265256.0,
    }
    monkeypatch.setattr(mdc, "md_quote_rows", lambda *a, **k: [stale])
    assert len(P._current_bars("002943")) == len(bars)

    # 新交易日:prev_close == 最后一根收盘,且 OHLC 不同 → 补一根今天的
    fresh = {
        "current_price": 30.10,
        "open_price": 29.50,
        "high_price": 30.40,
        "low_price": 29.20,
        "prev_close": 29.09,
        "volume": 1200.0,
    }
    monkeypatch.setattr(mdc, "md_quote_rows", lambda *a, **k: [fresh])
    got = P._current_bars("002943")
    assert len(got) == len(bars) + 1
    assert got[-1].date == "2026-11-12"
    assert got[-1].close == 30.10
