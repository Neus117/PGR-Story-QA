"""可调设置：步数上限与回答字数上限。

存 data/settings.json，改了立刻生效，不需要重启。
"""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
PATH = DATA / "settings.json"

# 默认值就是此前写死的常量：8 步、800 字
# 步数默认取历史中位数 7（P75 是 8，10 步的样本明显更慢）
DEFAULTS = {"max_steps": 7, "max_answer_chars": 800}

LIMITS = {"max_steps": (2, 10), "max_answer_chars": (200, 2000)}


def _clamp(name: str, value) -> int:
    lo, hi = LIMITS[name]
    try:
        v = int(value)
    except Exception:
        return DEFAULTS[name]
    return max(lo, min(hi, v))


def load() -> dict:
    out = dict(DEFAULTS)
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8"))
        for k in DEFAULTS:
            if k in raw:
                out[k] = _clamp(k, raw[k])
    except Exception:
        pass
    return out


def save(patch: dict) -> dict:
    cur = load()
    for k in DEFAULTS:
        if k in patch:
            cur[k] = _clamp(k, patch[k])
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return cur


def max_steps() -> int:
    return load()["max_steps"]


def max_answer_chars() -> int:
    return load()["max_answer_chars"]
