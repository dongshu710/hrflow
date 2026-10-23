"""启动 Web 服务。

用法：
    python scripts/run_server.py                 # 直接启动（用现有数据）
    python scripts/run_server.py --reset --seed   # 重置并灌入演示数据后启动
    python scripts/run_server.py --port 9000
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hrflow.api.server import run  # noqa: E402
from hrflow.console import enable_utf8  # noqa: E402
from hrflow.repository.db import DEFAULT_DB_PATH  # noqa: E402


def main():
    enable_utf8()
    parser = argparse.ArgumentParser(description="启动人事流程 mini 版 Web 服务")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--seed", action="store_true", help="写入演示数据")
    parser.add_argument("--reset", action="store_true", help="先重置数据库")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    if args.seed or args.reset:
        from scripts.seed import seed
        seed(args.db, reset=args.reset or args.seed)
        print(f"演示数据已就绪：{args.db}")

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    run(host=args.host, port=args.port, db_path=args.db)


if __name__ == "__main__":
    main()
