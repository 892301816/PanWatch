"""静态页面兜底只接管页面导航，未知 API 必须保留 404。"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse


def register_spa(app: FastAPI, directory: str) -> None:
    root = Path(directory).resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def serve_spa(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "接口不存在，请确认本地服务已更新并重启")
        target = (root / path).resolve()
        if target.is_relative_to(root) and target.is_file():
            return FileResponse(target)
        return FileResponse(root / "index.html")
