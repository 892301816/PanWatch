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
        # 入口 HTML 不缓存：发版后浏览器若拿旧 HTML 去要已删除的旧 hash 包，会白屏
        headers = (
            {"Cache-Control": "public, max-age=31536000, immutable"}
            if path.startswith("assets/")
            else {"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
        if target.is_relative_to(root) and target.is_file():
            return FileResponse(target, headers=headers)
        # 带扩展名的静态资源缺失时直接 404；
        # 回退成 index.html 会让浏览器把 HTML 当 JS 解析，只报 "Invalid or unexpected token"，
        # 反而掩盖了「这个 chunk 已不存在」的真实原因。
        suffix = target.suffix.lower()
        if suffix and suffix not in {".html", ".htm"}:
            raise HTTPException(404, "静态资源不存在，可能是旧版本缓存，请强制刷新")
        return FileResponse(
            root / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
