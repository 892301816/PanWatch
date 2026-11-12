"""成交同步：文本解析、交割单解析、幂等落库、导入预览/提交、令牌鉴权。"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import src.platform.persistence.models  # noqa: F401  注册 ORM 模型
from src.modules.portfolio import trade_import, trade_ingest
from src.modules.portfolio.trade_text import parse_trade_text
from src.platform.persistence.database import Base
from src.platform.persistence.models import Account, PersonalAccessToken, Position, TradeRecord

CSV_SAMPLE = """资金账号:12345678,姓名:测试
成交日期,成交时间,证券代码,证券名称,买卖标志,成交数量,成交价格,成交金额,佣金,印花税,过户费,成交编号
2026-11-09,09:31:05,002943,宇晶股份,买入,200,29.500,5900.00,5.00,0.00,0.02,A001
2026-11-10,10:02:11,002943,宇晶股份,卖出,100,30.200,3020.00,5.00,1.51,0.01,A002
2026-11-11,09:40:00,600000,浦发银行,证券买入,1000,10.500,10500.00,5.00,0.00,0.02,A003
2026-11-11,09:41:00,600000,浦发银行,撤单,0,0,0,0,0,0,A004
合计,3笔,,,,,,,,,,,
"""


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


@pytest.fixture()
def book():
    engine, db = _db()
    account = Account(name="测试账户", available_funds=100000.0, enabled=True)
    db.add(account)
    db.commit()
    db.refresh(account)
    yield db, account
    db.close()
    engine.dispose()


# ── 文本解析 ──────────────────────────────────────────────────────────────


def test_parse_sms_buy_with_everything():
    parsed = parse_trade_text(
        "【华泰证券】您尾号1234账户于2026年11月11日10:31:05以29.50元买入002943宇晶股份200股，"
        "成交金额5900.00元，手续费5.00元。"
    )
    assert parsed["ok"] is True
    assert parsed["action"] == "buy"
    assert parsed["symbol"] == "002943"
    assert parsed["name"] == "宇晶股份"
    assert parsed["quantity"] == 200
    assert parsed["price"] == 29.5
    assert parsed["trade_date"] == "2026-11-11"
    assert parsed["time"] == "10:31:05"
    assert parsed["fees"]["total"] == 5.0
    assert parsed["broker"] == "华泰证券"


def test_parse_sell_with_fee_components():
    parsed = parse_trade_text(
        "以30.10元卖出600000浦发银行1000股，佣金5元，印花税30.10元，过户费0.10元"
    )
    assert parsed["ok"] is True
    assert parsed["action"] == "sell"
    assert parsed["symbol"] == "600000"
    assert parsed["fees"] == {
        "commission": 5.0,
        "stamp_duty": 30.1,
        "transfer_fee": 0.1,
        "other_fee": 0.0,
        "total": 35.2,
    }


def test_parse_name_only():
    parsed = parse_trade_text("卖出 宇晶股份 200 股 成交均价 30.10")
    assert parsed["ok"] is True
    assert parsed["symbol"] == ""
    assert parsed["name"] == "宇晶股份"
    assert any("名称匹配" in w for w in parsed["warnings"])


def test_parse_cancel_is_rejected():
    parsed = parse_trade_text("您委托的002943宇晶股份已撤单")
    assert parsed["ok"] is False
    assert "非成交" in parsed["reason"]


def test_parse_missing_price_fails():
    parsed = parse_trade_text("买入 002943 宇晶股份 200 股")
    assert parsed["ok"] is False
    assert "成交价格" in parsed["reason"]


# ── 交割单 ────────────────────────────────────────────────────────────────


def test_parse_statement_csv_with_preamble_and_skips():
    rows = trade_import.parse_statement(CSV_SAMPLE, filename="statement.csv")
    assert len(rows) == 4
    ok_rows = [r for r in rows if r["ok"]]
    assert len(ok_rows) == 3
    first = ok_rows[0]
    assert first["action"] == "buy"
    assert first["symbol"] == "002943"
    assert first["external_id"] == "A001"
    assert first["fees"]["total"] == 5.02
    assert rows[3]["ok"] is False and "非成交" in rows[3]["reason"]


def test_parse_statement_xlsx():
    from io import BytesIO

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["成交日期", "证券代码", "证券名称", "买卖标志", "成交数量", "成交价格", "手续费"])
    ws.append(["2026-11-11", "002943", "宇晶股份", "买入", 300, 29.8, 5.0])
    buf = BytesIO()
    wb.save(buf)

    rows = trade_import.parse_statement(buf.getvalue(), filename="x.xlsx")
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert rows[0]["quantity"] == 300
    assert rows[0]["fees"]["total"] == 5.0


# ── 落库与幂等 ────────────────────────────────────────────────────────────


def test_ingest_buy_then_duplicate(book):
    db, account = book
    parsed = parse_trade_text(
        "2026年11月11日10:31:05以29.50元买入002943宇晶股份200股，手续费5.00元"
    )
    first = trade_ingest.ingest_trade(db, {**parsed, "account_id": account.id}, source="sms")
    assert first["ok"] is True and first["duplicate"] is False
    assert first["trade"]["cash_change"] == -5905.0
    position = db.query(Position).filter(Position.account_id == account.id).first()
    assert position.quantity == 200
    assert round(position.cost_price, 3) == 29.525

    again = trade_ingest.ingest_trade(db, {**parsed, "account_id": account.id}, source="sms")
    assert again["ok"] is True and again["duplicate"] is True
    assert db.query(TradeRecord).count() == 1


def test_ingest_external_id_wins_dedupe(book):
    db, account = book
    payload = {
        "action": "buy",
        "symbol": "002943",
        "name": "宇晶股份",
        "quantity": 100,
        "price": 30.0,
        "trade_date": "2026-11-11",
        "external_id": "A123",
        "fees": {"total": 5},
        "account_id": account.id,
    }
    first = trade_ingest.ingest_trade(db, dict(payload), source="import")
    assert first["ok"] and not first["duplicate"]
    # 改价格但成交编号相同 → 仍然算重复
    second = trade_ingest.ingest_trade(db, {**payload, "price": 30.01}, source="import")
    assert second["duplicate"] is True
    assert db.query(TradeRecord).count() == 1


def test_ingest_sell_requires_position(book):
    db, account = book
    result = trade_ingest.ingest_trade(
        db,
        {
            "action": "sell",
            "symbol": "002943",
            "name": "宇晶股份",
            "quantity": 100,
            "price": 30.0,
            "trade_date": "2026-11-11",
            "account_id": account.id,
        },
        source="sms",
    )
    assert result["ok"] is False
    assert "可卖数量不足" in result["error"]


def test_ingest_dry_run_writes_nothing(book):
    db, account = book
    result = trade_ingest.ingest_trade(
        db,
        {
            "action": "buy",
            "symbol": "600000",
            "name": "浦发银行",
            "quantity": 1000,
            "price": 10.5,
            "trade_date": "2026-11-11",
            "account_id": account.id,
        },
        source="sms",
        dry_run=True,
    )
    assert result["ok"] is True and result.get("dry_run") is True
    assert db.query(TradeRecord).count() == 0


def test_import_preview_and_commit(book):
    db, account = book
    rows = trade_import.parse_statement(CSV_SAMPLE, filename="statement.csv")
    annotated = trade_ingest.annotate_rows(db, rows, account=account)
    statuses = [r["status"] for r in annotated]
    # 股票不在自选时标 new_stock（提交时会自动创建），一样可导入
    assert statuses.count("ok") + statuses.count("new_stock") == 3
    assert statuses.count("invalid") == 1

    result = trade_ingest.commit_rows(db, annotated, account=account, source="import")
    assert result["created"] == 3
    assert db.query(TradeRecord).count() == 3
    # 买 200 卖 100 → 剩 100
    position = db.query(Position).filter(Position.account_id == account.id).first()
    assert position.quantity == 100

    # 再导一次：全部是重复
    rows2 = trade_import.parse_statement(CSV_SAMPLE, filename="statement.csv")
    annotated2 = trade_ingest.annotate_rows(db, rows2, account=account)
    assert [r["status"] for r in annotated2].count("duplicate") == 3
    result2 = trade_ingest.commit_rows(db, annotated2, account=account, source="import")
    assert result2["created"] == 0
    assert db.query(TradeRecord).count() == 3


# ── API ──────────────────────────────────────────────────────────────────


def test_api_ingest_text_and_preview(book):
    from src.modules.portfolio.api import ingest as api

    db, account = book
    response = api.ingest_trade(
        api.IngestIn(
            text="2026年11月11日10:31:05以29.50元买入002943宇晶股份200股，手续费5.00元",
            account_id=account.id,
        ),
        db=db,
        _auth=None,
    )
    assert response["ok"] is True
    assert response["trade"]["symbol"] == "002943"

    preview = api.import_preview(
        api.ImportIn(content=CSV_SAMPLE, filename="statement.csv", account_id=account.id),
        db=db,
        _auth=None,
    )
    assert preview["ok"] is True
    assert preview["summary"]["total"] == 4
    assert preview["summary"]["ok"] + preview["summary"]["new_stock"] == 3

    committed = api.import_commit(
        api.ImportIn(content=CSV_SAMPLE, filename="statement.csv", account_id=account.id),
        db=db,
        _auth=None,
    )
    assert committed["created"] == 3


# ── 路由挂载与迁移 ──────────────────────────────────────────────────────


def test_sync_routes_mounted():
    from src.bootstrap.application import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/portfolio/trades/ingest" in paths
    assert "/api/portfolio/trades/import/preview" in paths
    assert "/api/portfolio/trades/import/commit" in paths


def test_trade_external_id_column_migrated():
    from sqlalchemy import text

    from src.platform.persistence.database import init_db, engine

    init_db()
    with engine.begin() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(trade_records)"))]
    assert "external_id" in columns


# ── 令牌鉴权 ──────────────────────────────────────────────────────────────


def _request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/portfolio/trades/ingest",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", 1234),
        }
    )


def test_pat_trade_scope_allows(book):
    from src.modules.administration.pat import SCOPE_TRADE_WRITE, generate_pat
    from src.modules.administration.pat_auth import authenticate_sync

    db, _account = book
    plaintext, token_hash, prefix = generate_pat()
    db.add(
        PersonalAccessToken(
            name="手机同步",
            token_hash=token_hash,
            prefix=prefix,
            scopes_json=json.dumps([SCOPE_TRADE_WRITE]),
        )
    )
    db.commit()

    auth = authenticate_sync(
        _request(plaintext),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=plaintext),
        db,
    )
    assert auth["kind"] == "pat"


def test_jwt_also_allowed_for_sync(book):
    from src.modules.administration.api.auth import create_token
    from src.modules.administration.pat_auth import authenticate_sync

    db, _account = book
    token, _expires = create_token()
    auth = authenticate_sync(
        _request(token),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        db,
    )
    assert auth["kind"] == "jwt"


def test_pat_mcp_scope_rejected_for_trade(book):
    from src.modules.administration.pat import SCOPE_MCP_READ, generate_pat
    from src.modules.administration.pat_auth import authenticate_sync

    db, _account = book
    plaintext, token_hash, prefix = generate_pat()
    db.add(
        PersonalAccessToken(
            name="MCP",
            token_hash=token_hash,
            prefix=prefix,
            scopes_json=json.dumps([SCOPE_MCP_READ]),
        )
    )
    db.commit()

    with pytest.raises(HTTPException) as exc:
        authenticate_sync(
            _request(plaintext),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=plaintext),
            db,
        )
    assert exc.value.status_code == 403


# ── 助手工具 ──────────────────────────────────────────────────────────────


def test_assistant_record_trade_tool(book):
    from pan_agent import ModelMessage, RunRequest

    from src.modules.assistant.tools import build_panwatch_tool_registry

    db, account = book
    registry = build_panwatch_tool_registry(db)
    result = asyncio.run(
        registry.execute(
            "record_trade",
            RunRequest(run_id="t", messages=[ModelMessage(role="user", content="记一笔")]),
            {
                "text": "2026年11月11日10:31:05以29.50元买入002943宇晶股份200股，手续费5.00元",
                "account_id": account.id,
            },
        )
    )
    assert result.ok is True
    assert "已记录" in result.summary
    assert db.query(TradeRecord).count() == 1
