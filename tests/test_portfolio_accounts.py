"""持仓账户 HTTP 用例的持久化边界回归测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.platform.persistence.database import Base
from src.platform.persistence.models import Account, Position, Stock  # noqa: F401 - 注册关系模型


def test_delete_position_logs_relationship_names_before_commit():
    """删除持仓后仍能完成响应，日志不能访问已脱离会话的关系对象。"""
    from src.modules.portfolio.api.accounts import delete_position

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    account = Account(name="测试账户")
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    session.add_all([account, stock])
    session.flush()
    position = Position(
        account_id=account.id,
        stock_id=stock.id,
        cost_price=1500,
        quantity=100,
    )
    session.add(position)
    session.commit()

    result = delete_position(position.id, session)

    assert result == {"success": True}
    assert session.get(Position, position.id) is None
    session.close()
    engine.dispose()


def test_delete_position_does_not_read_detached_relationships_after_delete():
    """关系对象在删除提交后不可用时，删除接口仍应正常返回。"""
    from src.modules.portfolio.api.accounts import delete_position

    class Relation:
        def __init__(self, name: str, owner: "FakePosition"):
            self.name = name
            self._owner = owner

        def __getattribute__(self, attribute: str):
            if attribute == "name" and object.__getattribute__(self, "_owner").detached:
                raise AssertionError("删除提交后不应再访问懒加载关系")
            return object.__getattribute__(self, attribute)

    class FakePosition:
        id = 7

        def __init__(self):
            self.detached = False
            self.account = Relation("测试账户", self)
            self.stock = Relation("贵州茅台", self)

    class FakeQuery:
        def __init__(self, position):
            self.position = position

        def filter(self, _condition):
            return self

        def first(self):
            return self.position

    class FakeSession:
        def __init__(self, position):
            self.position = position

        def query(self, _model):
            return FakeQuery(self.position)

        def delete(self, position):
            position.detached = True

        def commit(self):
            return None

    position = FakePosition()

    assert delete_position(position.id, FakeSession(position)) == {"success": True}


def test_new_position_response_exposes_actual_trade_id_for_decision_linking():
    """The HTTP response model must preserve the saved ledger ID, not filter it out."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.modules.portfolio.api.accounts import router
    from src.platform.persistence.database import get_db
    from src.platform.persistence.models import TradeRecord

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        account = Account(name="事实记账", available_funds=10000)
        stock = Stock(symbol="600900", name="测试标的", market="CN")
        session.add_all([account, stock])
        session.commit()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: session
        with TestClient(app) as client:
            response = client.post("/positions", json={"account_id": account.id, "stock_id": stock.id, "cost_price": 10, "quantity": 100, "record_trade": True, "deduct_cash": True})
        assert response.status_code == 200
        saved = response.json()
        trade = session.get(TradeRecord, saved["trade_id"])
        assert trade is not None
        assert trade.account_id == account.id
        assert trade.stock_symbol == stock.symbol
        assert trade.quantity == 100
        assert session.query(TradeRecord).count() == 1
        assert session.get(Account, account.id).available_funds == 9000
    finally:
        session.close()
        engine.dispose()
