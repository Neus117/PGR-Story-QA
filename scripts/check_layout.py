"""页脚几何门禁：按钮与输入框是否齐平、复选框是否与输入框同左缘、版本号是否居中。

    python scripts/check_layout.py

用 headless Edge 打开 ?measure=1，读页面自己算出的 getBoundingClientRect，
容差 1px。失败退出码 1。

依赖：本机装了 Edge（或用 --edge 指定）、服务已在跑（PGR_BASE，默认 8712）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("PGR_BASE", "http://127.0.0.1:8712")
EDGE_DEFAULT = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
TOL = 1.0

CHECKS = [
    ("提问按钮与输入框顶边齐平", "button", "y", "textarea", "y"),
    ("提问按钮与输入框底边齐平", "button", "bottom", "textarea", "bottom"),
    ("复选框与输入框同左缘", "checkbox", "x", "textarea", "x"),
    ("版本号在按钮正下方居中", "version", "cx", "button", "cx"),
]


def find_edge() -> str:
    if "--edge" in sys.argv:
        return sys.argv[sys.argv.index("--edge") + 1]
    for p in (EDGE_DEFAULT,
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return p
    return "msedge"


def measure(edge: str, theme: str) -> dict:
    url = f"{BASE}/?nosse=1&measure=1&theme={theme}"
    # 不要用文件句柄接 stdout：子进程会继承句柄，随后读取会撞“文件被占用”
    out = subprocess.run([edge, "--headless=new", "--disable-gpu",
                          "--window-size=1000,700", "--virtual-time-budget=15000",
                          "--dump-dom", url],
                         capture_output=True, timeout=120)
    dom = out.stdout.decode("utf-8", "replace")
    m = re.search(r'id="measureOut">(.*?)</pre>', dom, re.S)
    if not m:
        raise RuntimeError("页面没有输出几何数据（?measure=1 钩子可能被删了）")
    return json.loads(m.group(1))


def main() -> int:
    try:
        urllib.request.urlopen(f"{BASE}/api/health", timeout=5).read()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"服务不可达（{BASE}）：{e}")
        return 2

    edge = find_edge()
    bad = 0
    for theme in ("dark", "light"):
        print(f"=== {theme} ===")
        try:
            d = measure(edge, theme)
        except Exception as e:
            print(f"  量测失败：{e}")
            return 2
        for name, ea, ka, eb, kb in CHECKS:
            a, b = d.get(ea), d.get(eb)
            if not a or not b:
                print(f"  [FAIL] {name}：元素缺失")
                bad += 1
                continue
            diff = a[ka] - b[kb]
            ok = abs(diff) <= TOL
            if not ok:
                bad += 1
            print(f"  {'OK  ' if ok else 'FAIL'} {name}：{ea}.{ka}={a[ka]:.2f} "
                  f"{eb}.{kb}={b[kb]:.2f} 差 {diff:+.2f}px")

    print()
    if bad:
        print(f"布局门禁：{bad} 项不达标")
        return 1
    print("布局门禁：全部通过（容差 1px）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
