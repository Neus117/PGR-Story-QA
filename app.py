"""战双剧情问答 —— 启动入口。

    python app.py                 启动并打开浏览器
    python app.py --no-browser    只起服务
    python app.py --port 9000     指定端口
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pgr import server  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="战双剧情问答本地服务")
    ap.add_argument("--port", type=int, default=8712, help="监听端口（默认 8712）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    args = ap.parse_args()
    return server.main(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
