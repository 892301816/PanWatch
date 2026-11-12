#!/usr/bin/env python3
"""交割单目录监听：新文件自动上传到 PanWatch 并导入。

适用场景：同花顺/券商 PC 端导出交割单到一个固定文件夹，电脑开着就自动同步。
只依赖标准库，不需要装 pandas。

用法::

    python scripts/sync/statement_watch.py \
        --url http://127.0.0.1:8000 \
        --token pwmcp_你的同步令牌 \
        --account 1 \
        --dir "D:/trades/statements"

    # 只跑一轮（适合 Windows 计划任务）
    python scripts/sync/statement_watch.py ... --once

令牌在 PanWatch「设置 → 交易同步」里创建（scope: trade:write）。
重复文件按内容 sha256 记录，不会重复上传；同一笔成交由服务端再按成交编号/复合键去重。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

SUPPORTED = (".csv", ".txt", ".tsv", ".xls", ".xlsx")
STATE_FILE = ".panwatch_sync_state.json"


def _post(url: str, token: str, payload: dict, timeout: int = 90) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:300]}") from exc


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_state(directory: str) -> dict:
    path = os.path.join(directory, STATE_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(directory: str, state: dict) -> None:
    path = os.path.join(directory, STATE_FILE)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
    os.replace(temp, path)


def _payload_for(path: str) -> dict:
    name = os.path.basename(path)
    with open(path, "rb") as handle:
        raw = handle.read()
    if name.lower().endswith((".xlsx", ".xls")):
        return {"filename": name, "base64": base64.b64encode(raw).decode("ascii")}
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return {"filename": name, "content": raw.decode(encoding)}
        except UnicodeDecodeError:
            continue
    return {"filename": name, "content": raw.decode("utf-8", errors="replace")}


def sync_once(args: argparse.Namespace) -> int:
    state = _load_state(args.dir)
    files = sorted(
        os.path.join(args.dir, name)
        for name in os.listdir(args.dir)
        if name.lower().endswith(SUPPORTED) and not name.startswith("~$")
    )
    if not files:
        print(f"[sync] {args.dir} 里没有新的交割单文件")
        return 0

    imported = 0
    for path in files:
        name = os.path.basename(path)
        stat = os.stat(path)
        if stat.st_size == 0:
            continue
        sha = _sha256_file(path)
        if state.get(name, {}).get("sha256") == sha:
            continue

        print(f"[sync] 上传 {name} ({stat.st_size} bytes)")
        payload = {
            **_payload_for(path),
            "account_id": args.account,
        }
        try:
            preview = _post(f"{args.url.rstrip('/')}/api/portfolio/trades/import/preview", args.token, payload)
            summary = preview.get("summary") or {}
            importable = int(summary.get("ok") or 0) + int(summary.get("new_stock") or 0)
            print(
                f"       解析 {summary.get('total', 0)} 行：可导入 {importable}，"
                f"重复 {summary.get('duplicate', 0)}，跳过 {summary.get('invalid', 0)}"
            )
            if args.dry_run:
                for row in (preview.get("rows") or [])[:5]:
                    print(
                        f"       - 第{row.get('line')}行 {row.get('trade_date')} {row.get('symbol')} "
                        f"{row.get('name')} {row.get('action')} {row.get('quantity')}@{row.get('price')} "
                        f"[{row.get('status')}]"
                    )
                continue
            if importable <= 0:
                print("       没有可导入的成交行，标记为已处理")
            else:
                result = _post(f"{args.url.rstrip('/')}/api/portfolio/trades/import/commit", args.token, payload)
                print(
                    f"       导入完成：新增 {result.get('created')}，重复 {result.get('duplicate')}，"
                    f"失败 {result.get('failed')}"
                )
                imported += int(result.get("created") or 0)
            state[name] = {"sha256": sha, "size": stat.st_size, "at": int(time.time())}
            _save_state(args.dir, state)
        except Exception as exc:  # noqa: BLE001 — 单文件失败不阻塞后续
            print(f"       [错误] {exc}")
    return imported


def main() -> int:
    parser = argparse.ArgumentParser(description="监听交割单目录并导入 PanWatch")
    parser.add_argument("--url", required=True, help="PanWatch 地址，例如 http://127.0.0.1:8000")
    parser.add_argument("--token", required=True, help="trade:write 同步令牌")
    parser.add_argument("--account", type=int, required=True, help="账户 id")
    parser.add_argument("--dir", default=".", help="交割单所在目录")
    parser.add_argument("--interval", type=int, default=60, help="轮询间隔秒数（默认 60）")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument("--dry-run", action="store_true", help="只预览不导入")
    args = parser.parse_args()

    if not os.path.isdir(args.dir):
        print(f"目录不存在：{args.dir}", file=sys.stderr)
        return 2

    print(f"[sync] 监听 {os.path.abspath(args.dir)} → {args.url}（账户 {args.account}）")
    while True:
        try:
            sync_once(args)
        except Exception as exc:  # noqa: BLE001
            print(f"[sync] 本轮失败：{exc}")
        if args.once:
            return 0
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
