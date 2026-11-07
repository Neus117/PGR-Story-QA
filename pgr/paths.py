"""路径约定：所有模块都从这里取目录，不各自推导。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓库根
DATA = ROOT / "data"                            # 运行数据（不进仓库）
WEB = ROOT / "web"
CACHE = DATA / "cache"

for _d in (DATA, CACHE):
    _d.mkdir(parents=True, exist_ok=True)
