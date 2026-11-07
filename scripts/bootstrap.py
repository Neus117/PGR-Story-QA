"""一键重建全部本地数据：枚举 → 抓取 → 解析 → 建索引。

    python scripts/bootstrap.py              # 全流程（首次约 6~8 分钟）
    python scripts/bootstrap.py --skip-fetch # 已有 data/raw/ 时只重解析并建索引（约 1 分钟）

抓取阶段对站点限速 4 秒/请求，遇到 HTTP 567 会按分钟级冷却重试；已抓过的页有缓存，可断点续跑。
"""
from __future__ import annotations

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pgr import enumerate as enum_mod   # noqa: E402
from pgr import fetch_all, index, parse  # noqa: E402


def step(name: str, fn) -> bool:
    print(f"\n=== {name} ===", flush=True)
    t0 = time.time()
    try:
        rc = fn()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        print(f"失败: {type(e).__name__}: {e}")
        return False
    ok = (rc in (0, None))
    print(f"=== {name}: {'OK' if ok else 'FAILED'}  ({time.time()-t0:.0f}s) ===", flush=True)
    return ok


def main() -> int:
    skip_fetch = "--skip-fetch" in sys.argv
    if not skip_fetch:
        if not step("1/4 枚举章节（SMW 查询）", enum_mod.main):
            return 1
        if not step("2/4 抓取关卡原文（约 74 次请求）", fetch_all.main):
            return 1
    else:
        print("跳过抓取，直接使用 data/raw/ 现有内容")
    if not step("3/4 解析 wikitext 并清洗", parse.main):
        return 1
    if not step("4/4 建立检索索引", index.main):
        return 1
    print("\n完成。启动：python app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
