"""Sentiment cycle: board rules, state machine, position caps, routes."""

from __future__ import annotations

from src.modules.market.sentiment_cycle import (
    Bar,
    Config,
    EmotionEngine,
    Signal,
    State,
    classify,
    infer_limit_pct,
)
from src.platform.persistence.migrations import MIGRATIONS


def _sig(symbol="S1", industry="Semis", *, lu=False, brk=False, ld=False,
         hf=False, sky=False, board=0, turnover=5.0):
    return Signal(
        symbol=symbol,
        name=symbol,
        industry=industry,
        board_num=board,
        limit_up=lu,
        break_up=brk,
        limit_down=ld,
        high_face=hf,
        sky_floor=sky,
        turnover=turnover,
    )


def test_limit_pct_by_board():
    assert infer_limit_pct("600900", "长江电力") == 10.0
    assert infer_limit_pct("300750", "宁德时代") == 20.0
    assert infer_limit_pct("688981", "中芯国际") == 20.0
    assert infer_limit_pct("830799", "艾融软件") == 30.0
    assert infer_limit_pct("920001", "万德股份") == 30.0
    assert infer_limit_pct("600001", "ST测试") == 5.0
    # ST on a 20% board keeps the 20% rule
    assert infer_limit_pct("300001", "ST创业") == 20.0


def test_new_listing_has_no_limit_flags():
    today = Bar("600001", "2026-11-20", 10.5, 11.0, 10.4, 11.0, 10.0, 5.0)
    signal = classify(today, [], name="新股")
    assert signal.no_limit is True
    assert signal.limit_up is False and signal.break_up is False


def test_limit_up_and_break_up_board_number():
    history = [
        Bar("600900", f"2026-11-1{i}", 10, 10.5, 9.9, 10.0, 10.0, 5.0)
        for i in range(5)
    ]
    # 5 flat bars + a limit-up today -> first board
    sealed = classify(
        Bar("600900", "2026-11-20", 10.5, 11.0, 10.4, 11.0, 10.0, 5.0),
        history,
        name="长江电力",
    )
    assert sealed.limit_up and sealed.board_num == 1

    # three sealed boards in history + a failed attempt today -> 4th board.
    # Keep at least 6 bars so the new-listing guard does not mask the signal.
    prior = [
        Bar("600900", "2026-11-01", 8.0, 8.1, 7.9, 8.0, 8.0, 5.0),
        Bar("600900", "2026-11-02", 8.0, 8.1, 7.9, 8.0, 8.0, 5.0),
    ]
    price = 8.0
    for i in range(3):
        nxt = round(price * 1.1, 2)
        prior.append(Bar("600900", f"2026-11-0{i+3}", price, nxt, price, nxt, price, 5.0))
        price = nxt
    today = Bar("600900", "2026-11-20", 10.5, 11.72, 10.4, 10.6, price, 5.0)
    broken = classify(today, prior, name="长江电力")
    assert broken.break_up and not broken.limit_up
    assert broken.board_num == 4


def test_no_main_line_is_flat():
    engine = EmotionEngine()
    snap = engine.update("2026-11-20", [
        _sig("A", lu=True, board=2),
        _sig("B", lu=True, board=2),
    ], 0.0)
    assert snap.main_sector is None
    assert snap.l2 == 0.0
    assert snap.position_tier == "T1"
    assert snap.single_position == 0.0 and snap.total_position == 0.0


def test_l1_caps_position():
    state = State(prev_drop_trigger=True)
    engine = EmotionEngine(Config(), state)
    signals = [
        _sig("S1", "Semis", lu=True, board=4),
        _sig("S2", "Semis", lu=True, board=4),
        _sig("S3", "Semis", lu=True, board=4),
    ]
    signals += [_sig(f"H{i}", "Semis", hf=True) for i in range(15)]
    signals += [_sig(f"D{i}", "Other", ld=True) for i in range(5)]
    snap = engine.update("2026-11-20", signals, 0.0)
    assert snap.l1 == 4.0
    # L2 is high enough for T4 but the L1 cap pulls it down to T2.
    assert snap.l2 >= 8.0
    assert snap.position_tier == "T2"
    assert snap.total_position == 0.10


def test_main_line_fry_rate_scope():
    engine = EmotionEngine()
    signals = [
        _sig("S1", "Semis", lu=True, board=4),
        _sig("S2", "Semis", brk=True, board=3),
        _sig("S3", "Semis", lu=True, board=1),
        _sig("S4", "Semis", lu=True, board=1),
        _sig("S5", "Semis", lu=True, board=1),
        _sig("S6", "Semis", lu=True, board=1),
    ]
    # Five broken boards in another sector must not leak into the main-line fry.
    signals += [_sig(f"O{i}", "Other", brk=True, board=3) for i in range(5)]
    snap = engine.update("2026-11-20", signals, 0.0)
    assert snap.main_sector == "Semis"
    assert snap.fry_rate == 0.5  # 1 break / 2 main-line high-board names
    assert snap.global_fry_rate > snap.fry_rate


def test_ebb_confirmation():
    state = State(prev_main_sector="Semis", prev_sector_hl=3, prev_l2=5.0,
                  leader_code="S1", leader_break_days=0)
    engine = EmotionEngine(Config(), state)
    signals = [
        _sig("S1", "Semis", brk=True, board=3),
        _sig("S2", "Semis", brk=True, board=3),
        _sig("S3", "Semis", brk=True, board=3),
    ]
    snap = engine.update("2026-11-20", signals, 0.0)
    assert snap.l2 <= 6.0
    assert snap.l3 in ("warning", "confirm")
    assert snap.ebb_confirmed is True


def test_state_roundtrip():
    state = State(last_date="2026-11-20", prev_l2=9.0,
                  main_fry_seq=[0.1, 0.2], prev_hl_codes=["600900"])
    loaded = State.from_dict(state.to_dict())
    assert loaded.last_date == "2026-11-20"
    assert loaded.prev_l2 == 9.0
    assert loaded.main_fry_seq == [0.1, 0.2]


def test_migration_139_registered():
    versions = {m.version for m in MIGRATIONS}
    assert 139 in versions


def test_sentiment_routes_mounted():
    from src.bootstrap.application import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/market/sentiment" in paths
    assert "/api/market/sentiment/refresh" in paths


def test_compute_and_store_persists_state(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.modules.market import sentiment_service
    from src.platform.persistence.database import Base
    from src.platform.persistence.models import MarketSentimentSnapshot, MarketSentimentState

    engine = create_engine(f"sqlite:///{tmp_path / 'sentiment.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    trade_date = "2026-11-20"
    bars = {
        f"600{i:03d}": [Bar(f"600{i:03d}", trade_date, 10.0, 10.1, 9.9, 10.0, 10.0, 5.0)]
        for i in range(600)
    }
    monkeypatch.setattr(sentiment_service, "_latest_bar_dates", lambda _db, limit=120: [trade_date])
    monkeypatch.setattr(sentiment_service, "_load_bars", lambda _db, _date: bars)
    monkeypatch.setattr(sentiment_service, "_cn_names", lambda: {})
    monkeypatch.setattr(
        sentiment_service, "_zt_pool_map", lambda _db, _date: ({}, True, [], "eastmoney+local")
    )
    monkeypatch.setattr(sentiment_service, "_hs300_return", lambda _date=None: 0.0)

    result = sentiment_service.compute_and_store(db, trade_date)

    assert result["ok"] is True
    snapshot = db.query(MarketSentimentSnapshot).one()
    assert snapshot.position_tier is not None
    state = db.query(MarketSentimentState).one()
    assert state.last_date == trade_date
    db.close()
