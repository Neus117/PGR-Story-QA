"""运行日志：写 logs/app.log，单文件 1 MB 轮转。

只记"判断依据的值"，不记流水账：密钥只记长度与前缀、错误记类型与摘要。
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_configured = False


def setup(level: int = logging.INFO) -> logging.Logger:
    global _configured
    log = logging.getLogger("pgr")
    if _configured:
        return log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False
    _configured = True
    log.info("=== 启动 pid=%s cwd=%s ===", os.getpid(), os.getcwd())
    return log


def get() -> logging.Logger:
    return setup()
