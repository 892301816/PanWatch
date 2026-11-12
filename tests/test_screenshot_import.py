"""截图导入：模型 JSON 解析、持仓/成交归一化、持仓快照落库、图片接口。"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.platform.persistence.models  # noqa: F401
from src.modules.portfolio import screenshot_import as shot
from src.platform.persistence.database import Base
from src.platform.persistence.models import Account, Position, Stock, TradeRecord

# 1x1 透明 PNG，仅用于验证 base64 解码链路
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


@pytest.fixture()
def book():
    engine, db = _db()
    account = Account(name="东吴证券", available_funds=50000.0, enabled=True)
    stock = Stock(symbol="600900", name="长江电力", market="CN")
    db.add_all([account, stock])
    db.commit()
    db.refresh(account)
    db.refresh(stock)
    db.add(
        Position(
            account_id=account.id,
            stock_id=stock.id,
            quantity=100,
            cost_price=28.0,
            invested_amount=2800.0,
        )
    )
    db.commit()
    yield db, account, stock
    db.close()
    engine.dispose()


def test_parse_model_json_handles_fences_and_junk():
    payload = '```json\n{"rows":[{"symbol":"600900"}],"hint":null}\n```'
    assert shot.parse_model_json(payload)["rows"][0]["symbol"] == "600900"
    assert shot.parse_model_json('说明文字 {"rows":[]} 结束')["rows"] == []
    assert shot.parse_model_json('[{"symbol":"600900"}]')["rows"][0]["symbol"] == "600900"
    assert shot.parse_model_json("完全不是 JSON") == {}


def test_normalize_holdings_filters_invalid_rows_and_keeps_closing():
    rows = shot.normalize_holdings(
        [
            {"symbol": "600900", "name": "长江电力", "quantity": "200", "available": "200",
             "cost_price": "29.100", "last_price": "29.61", "market_value": "5,922.00"},
            {"symbol": "002943", "name": "宇晶股份", "quantity": 0, "cost_price": 29.5},
            {"symbol": "601728", "name": "中国电信", "quantity": 500, "cost_price": None,
             "last_price": 6.61},
            {"name": "没有代码", "quantity": 100, "cost_price": 1},
        ]
    )
    # 数量为 0 的清仓行要保留，名称-only 也保留（后续按名称匹配）
    assert len(rows) == 4
    assert rows[0]["symbol"] == "600900"
    assert rows[0]["cost_price"] == 29.1
    assert rows[0]["market_value"] == 5922.0
    assert rows[1]["symbol"] == "002943" and rows[1]["quantity"] == 0
    assert rows[1]["closing"] is True
    # 没有成本价时用现价兜底
    assert rows[2]["symbol"] == "601728"
    assert rows[2]["cost_price"] == 6.61
    assert rows[3]["symbol"] == ""
    assert rows[3]["name"] == "没有代码"


def test_normalize_summary():
    summary = shot.normalize_summary(
        {"broker": "中信建投证券", "total_assets": "11,450.07", "market_value": 2812,
         "available": "8638.07", "withdrawable": 5709.54, "floating_pnl": "-226.50",
         "daily_pnl": -58, "daily_pnl_pct": "-0.50%", "position_ratio": "24.6%"}
    )
    assert summary["broker"] == "中信建投证券"
    assert summary["total_assets"] == 11450.07
    assert summary["available"] == 8638.07
    assert summary["floating_pnl"] == -226.5
    assert summary["daily_pnl_pct"] == -0.5
    assert summary["position_ratio"] == 24.6


def test_normalize_trades_maps_action_and_fees():
    rows = shot.normalize_trades(
        [
            {"trade_date": "2026-11-11", "time": "10:31:05", "action": "买入",
             "symbol": "002943", "name": "宇晶股份", "quantity": 200, "price": 29.5,
             "commission": 5, "stamp_duty": 0, "transfer_fee": 0.02},
            {"trade_date": "2026/11/11", "action": "sell", "symbol": "600900",
             "quantity": 100, "price": 30.1},
            {"action": "buy", "symbol": "600900", "quantity": 100},
        ]
    )
    assert rows[0]["ok"] is True
    assert rows[0]["action"] == "buy"
    assert rows[0]["fees"]["total"] == 5.02
    assert rows[0]["trade_date"] == "2026-11-11"
    assert rows[1]["action"] == "sell"
    assert rows[1]["trade_date"] == "2026-11-11"
    assert rows[2]["ok"] is False
    assert "成交价格" in rows[2]["reason"]


def test_apply_positions_create_update_unchanged(book):
    db, account, _stock = book
    rows = [
        {"symbol": "600900", "name": "长江电力", "quantity": 200, "cost_price": 29.1},
        {"symbol": "002943", "name": "宇晶股份", "quantity": 200, "cost_price": 29.5},
    ]
    result = shot.apply_positions(db, account=account, rows=rows)
    assert result["created"] == 1
    assert result["updated"] == 1
    assert result["unchanged"] == 0

    positions = db.query(Position).filter(Position.account_id == account.id).all()
    assert len(positions) == 2
    by_symbol = {
        db.query(Stock).filter(Stock.id == p.stock_id).first().symbol: p for p in positions
    }
    assert by_symbol["600900"].quantity == 200
    assert by_symbol["600900"].cost_price == 29.1
    assert by_symbol["002943"].quantity == 200

    # 再同步一次 → 无变化
    again = shot.apply_positions(db, account=account, rows=rows)
    assert again["unchanged"] == 2
    assert again["created"] == 0 and again["updated"] == 0


def test_apply_positions_remove_missing(book):
    db, account, _stock = book
    rows = [{"symbol": "002943", "name": "宇晶股份", "quantity": 100, "cost_price": 30.0}]
    result = shot.apply_positions(db, account=account, rows=rows, remove_missing=True)
    assert result["removed"] == 1  # 600900 不在截图里
    symbols = [
        db.query(Stock).filter(Stock.id == p.stock_id).first().symbol
        for p in db.query(Position).filter(Position.account_id == account.id).all()
    ]
    assert symbols == ["002943"]


def test_apply_positions_closes_zero_quantity(book):
    db, account, stock = book
    # book 里 600900 有 100 股；截图显示 0/0 → 应删除
    result = shot.apply_positions(
        db,
        account=account,
        rows=[{"symbol": "600900", "name": "长江电力", "quantity": 0, "available": 0,
               "cost_price": 28.0}],
    )
    assert result["closed"] == 1
    assert result["failed"] == 0
    assert db.query(Position).filter(Position.account_id == account.id).count() == 0


def test_apply_positions_syncs_available_funds(book):
    db, account, _stock = book
    result = shot.apply_positions(
        db,
        account=account,
        rows=[{"symbol": "600900", "name": "长江电力", "quantity": 100, "cost_price": 28.0}],
        available_funds=8638.07,
    )
    assert result["cash_updated"] is True
    db.refresh(account)
    assert account.available_funds == 8638.07


def _mock_recognize(monkeypatch, payload: dict):
    async def fake(image_bytes, mode, db, model_id):
        return payload

    monkeypatch.setattr(shot, "_recognize", fake)


def test_image_preview_positions(book, monkeypatch):
    db, account, _stock = book
    _mock_recognize(
        monkeypatch,
        {
            "rows": [
                {"symbol": "600900", "name": "长江电力", "quantity": 300, "cost_price": 29.0},
                {"symbol": "002943", "name": "宇晶股份", "quantity": 0, "cost_price": 29.5},
            ],
            "summary": {
                "broker": "中信建投证券",
                "total_assets": 11450.07,
                "available": 8638.07,
            },
            "hint": "",
        },
    )
    result = asyncio.run(
        shot.extract_from_image(
            TINY_PNG, mode="positions", db=db, account=account
        )
    )
    assert result["ok"] is True
    assert result["summary"]["add"] == 1
    assert result["summary"].get("close", 0) == 0  # 截图测试账户本来没有 002943 持仓
    assert result["account_summary"]["available"] == 8638.07
    assert "中信建投证券" in result["broker_warning"]


def test_image_preview_trades(book, monkeypatch):
    db, account, _stock = book
    _mock_recognize(
        monkeypatch,
        {
            "rows": [
                {"trade_date": "2026-11-11", "action": "buy", "symbol": "002943",
                 "name": "宇晶股份", "quantity": 200, "price": 29.5, "commission": 5},
            ],
            "hint": "",
        },
    )
    result = asyncio.run(
        shot.extract_from_image(TINY_PNG, mode="trades", db=db, account=account)
    )
    assert result["ok"] is True
    assert result["rows"][0]["status"] == "new_stock"


def test_image_api_preview_and_commit_positions(book, monkeypatch):
    from src.modules.portfolio.api import ingest as api

    db, account, _stock = book
    _mock_recognize(
        monkeypatch,
        {
            "rows": [
                {"symbol": "600900", "name": "长江电力", "quantity": 300, "cost_price": 29.0},
                {"symbol": "002943", "name": "宇晶股份", "quantity": 200, "cost_price": 29.5},
            ],
            "hint": "",
        },
    )
    preview = asyncio.run(
        api.image_preview(
            api.ImagePreviewIn(
                image_base64=base64.b64encode(TINY_PNG).decode("ascii"),
                mode="positions",
                account_id=account.id,
            ),
            db=db,
            _auth=None,
        )
    )
    assert preview["ok"] is True
    assert len(preview["rows"]) == 2

    result = api.image_commit(
        api.ImageCommitIn(
            mode="positions",
            rows=preview["rows"],
            account_id=account.id,
        ),
        db=db,
        _auth=None,
    )
    assert result["created"] == 1
    assert result["updated"] == 1
    positions = db.query(Position).filter(Position.account_id == account.id).all()
    assert len(positions) == 2
    # 持仓快照不写交易流水
    assert db.query(TradeRecord).count() == 0


def test_image_api_commit_trades(book, monkeypatch):
    from src.modules.portfolio.api import ingest as api

    db, account, _stock = book
    _mock_recognize(
        monkeypatch,
        {
            "rows": [
                {"trade_date": "2026-11-11", "action": "buy", "symbol": "002943",
                 "name": "宇晶股份", "quantity": 200, "price": 29.5, "commission": 5},
            ],
            "hint": "",
        },
    )
    preview = asyncio.run(
        api.image_preview(
            api.ImagePreviewIn(
                image_base64=base64.b64encode(TINY_PNG).decode("ascii"),
                mode="trades",
                account_id=account.id,
            ),
            db=db,
            _auth=None,
        )
    )
    result = api.image_commit(
        api.ImageCommitIn(mode="trades", rows=preview["rows"], account_id=account.id),
        db=db,
        _auth=None,
    )
    assert result["created"] == 1
    record = db.query(TradeRecord).first()
    assert record.source == "screenshot"
    assert record.stock_symbol == "002943"


def test_image_routes_mounted():
    from src.bootstrap.application import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/portfolio/trades/import/image/preview" in paths
    assert "/api/portfolio/trades/import/image/commit" in paths


def test_annotate_delta_add_uses_implied_cost(book):
    db, account, _stock = book  # 600900 当前 100 @ 28.0
    rows = shot.normalize_holdings(
        [{"symbol": "600900", "name": "长江电力", "quantity": 200, "cost_price": 30.0,
          "last_price": 29.6}]
    )
    annotated = shot._annotate_positions(db, rows, account)
    row = annotated[0]
    assert row["status"] == "add"
    assert row["delta"] == 100
    assert row["trade_action"] == "buy"
    assert row["trade_quantity"] == 100
    # (200*30 - 100*28) / 100 = 32
    assert row["trade_price"] == 32.0
    assert row["price_source"] == "implied_cost"


def test_annotate_delta_reduce_uses_last_price(book):
    db, account, _stock = book
    rows = shot.normalize_holdings(
        [{"symbol": "600900", "name": "长江电力", "quantity": 50, "cost_price": 28.0,
          "last_price": 30.0}]
    )
    annotated = shot._annotate_positions(db, rows, account)
    row = annotated[0]
    assert row["status"] == "reduce"
    assert row["delta"] == -50
    assert row["trade_action"] == "sell"
    assert row["trade_quantity"] == 50
    assert row["trade_price"] == 30.0
    assert row["price_source"] == "last_price"


def test_apply_positions_record_trades_buy(book):
    db, account, _stock = book
    rows = shot.normalize_holdings(
        [{"symbol": "600900", "name": "长江电力", "quantity": 200, "cost_price": 30.0,
          "last_price": 29.6}]
    )
    annotated = shot._annotate_positions(db, rows, account)
    result = shot.apply_positions(db, account=account, rows=annotated, record_trades=True)
    assert result["buy_trades"] == 1
    assert result["updated"] == 1
    position = db.query(Position).filter(Position.account_id == account.id).first()
    assert position.quantity == 200
    assert abs(position.cost_price - 30.0) <= 0.0005
    trade = db.query(TradeRecord).first()
    assert trade is not None
    assert trade.source == "screenshot"
    assert trade.action == "buy" and trade.quantity == 100 and trade.price == 32.0


def test_apply_positions_record_trades_sell(book):
    db, account, _stock = book
    rows = shot.normalize_holdings(
        [{"symbol": "600900", "name": "长江电力", "quantity": 50, "cost_price": 28.0,
          "last_price": 30.0}]
    )
    annotated = shot._annotate_positions(db, rows, account)
    result = shot.apply_positions(db, account=account, rows=annotated, record_trades=True)
    assert result["sell_trades"] == 1
    position = db.query(Position).filter(Position.account_id == account.id).first()
    assert position.quantity == 50
    assert abs(position.cost_price - 28.0) <= 0.0005
    trade = db.query(TradeRecord).first()
    assert trade.action == "sell" and trade.quantity == 50 and trade.price == 30.0
    assert abs(trade.realized_pnl - 100.0) <= 0.01


def test_apply_positions_record_trades_close(book):
    db, account, _stock = book
    rows = shot.normalize_holdings(
        [{"symbol": "600900", "name": "长江电力", "quantity": 0, "cost_price": 28.0,
          "last_price": 30.0}]
    )
    annotated = shot._annotate_positions(db, rows, account)
    result = shot.apply_positions(db, account=account, rows=annotated, record_trades=True)
    assert result["closed"] == 1
    assert result["sell_trades"] == 1
    assert db.query(Position).filter(Position.account_id == account.id).count() == 0
    trade = db.query(TradeRecord).first()
    assert trade.action == "sell" and trade.quantity == 100 and trade.price == 30.0


def test_apply_positions_without_record_trades_creates_no_trade(book):
    db, account, _stock = book
    rows = [{"symbol": "600900", "name": "长江电力", "quantity": 50, "cost_price": 28.0}]
    result = shot.apply_positions(db, account=account, rows=rows, record_trades=False)
    assert result["updated"] == 1
    assert db.query(TradeRecord).count() == 0
    position = db.query(Position).filter(Position.account_id == account.id).first()
    assert position.quantity == 50


def test_apply_positions_record_trades_and_cash(book):
    db, account, _stock = book
    rows = shot.normalize_holdings(
        [{"symbol": "600900", "name": "长江电力", "quantity": 50, "cost_price": 28.0,
          "last_price": 30.0}]
    )
    annotated = shot._annotate_positions(db, rows, account)
    result = shot.apply_positions(
        db, account=account, rows=annotated, record_trades=True, available_funds=12345.67
    )
    assert result["cash_updated"] is True
    db.refresh(account)
    assert account.available_funds == 12345.67
