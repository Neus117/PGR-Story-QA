"""路径约定：所有模块都从这里取目录，不各自推导。

两种运行形态：
  · 源码运行：仓库根 = 本文件的上一级目录
  · 打包运行（PyInstaller）：可执行文件所在目录是"根"，
    data/、logs/、config.json 都放在 exe 旁边；web/ 打进包里，从 _MEIPASS 读。
"""
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    ROOT = Path(sys.executable).resolve().parent          # exe 所在目录
    WEB = Path(getattr(sys, "_MEIPASS", ROOT)) / "web"    # 只读资源
else:
    ROOT = Path(__file__).resolve().parent.parent
    WEB = ROOT / "web"

DATA = ROOT / "data"
CACHE = DATA / "cache"

for _d in (DATA, CACHE):
    _d.mkdir(parents=True, exist_ok=True)

CONFIG = ROOT / "config.json"
