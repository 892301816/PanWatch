"""Serve the locally built PanWatch web app for desktop and phone acceptance testing."""
import argparse
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lan", action="store_true", help="allow phones on the local network")
    parser.add_argument("--port", type=int, default=5183)
    args = parser.parse_args()
    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        parser.error("请先在 frontend 目录运行 corepack pnpm build")
    from src.bootstrap.application import app
    from src.platform.persistence.database import init_db
    from src.web.spa import register_spa

    init_db()
    register_spa(app, str(dist))
    uvicorn.run(app, host="0.0.0.0" if args.lan else "127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

