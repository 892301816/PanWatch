from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.response import ResponseWrapperMiddleware
from src.web.spa import register_spa


def test_spa_keeps_unknown_apis_as_json_404_and_preserves_page_routes(tmp_path):
    (tmp_path / "index.html").write_text("<html>PanWatch</html>", encoding="utf-8")
    (tmp_path / "asset.js").write_text("console.log('ok')", encoding="utf-8")
    app = FastAPI()
    app.add_middleware(ResponseWrapperMiddleware)

    @app.get("/api/strategy/library")
    def library():
        return {"items": [{"id": 1}]}

    register_spa(app, str(tmp_path))
    client = TestClient(app)
    for path in ("/api/unknown", "/api"):
        response = client.get(path)
        assert response.status_code == 404
        assert "application/json" in response.headers["content-type"]
        assert "接口不存在" in response.text
    assert client.get("/api/strategy/library").json()["data"]["items"] == [{"id": 1}]
    assert client.get("/opportunities").text == "<html>PanWatch</html>"
    assert client.get("/portfolio").text == "<html>PanWatch</html>"
    assert client.get("/asset.js").text == "console.log('ok')"
