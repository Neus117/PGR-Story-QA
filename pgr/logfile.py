"""运行日志：写 logs/app.log，单文件 1 MB 轮转。

只记"判断依据的值"，不记流水账：密钥只记长度与前缀、错误记类型与摘要。
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"   # 源码运行时兜底
try:                                                         # 打包后要写到 exe 旁边
    from .paths import ROOT as _ROOT
    LOG_DIR = _ROOT / "logs"
except Exception:
    pass
_configured = False
_T0 = __import__("time").perf_counter()


def _elapsed_ms() -> float:
    return (__import__("time").perf_counter() - _T0) * 1000


def setup(level: int = logging.INFO) -> logging.Logger:
    global _configured
    log = logging.getLogger("pgr")
    if _configured:
        return log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    # 毫秒精度：启动链路各阶段只差几十毫秒，秒级时间戳量不出来
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False
    _configured = True
    log.info("=== 启动 pid=%s cwd=%s ===", os.getpid(), os.getcwd())
    log.info("日志模块就绪（自导入起 %d ms；解释器与加载器耗时在此前，需外部测量）",
             int(_elapsed_ms()))
    return log


def get() -> logging.Logger:
    return setup()
