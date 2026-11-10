"""清理测试数据：把问答历史里由自动化测试产生的记录删掉。

    python scripts/clean_test_history.py --dry-run     # 只看会删哪些
    python scripts/clean_test_history.py               # 真删

判据：session 以 `_t:` 或 `S-` 开头（测试专用前缀）。
**不按问题文本判断**——用户也可能问同一个问题，按文本删会误删真实数据。
真删前自动备份到 data/history.db.bak-<时间戳>。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
DB = DATA / "history.db"

# 只认「测试专用 session 前缀」。**绝不按问题文本匹配**——
# 同一个问题用户也可能真的在问，按文本删会连真实提问一起删掉（已踩过）。
TEST_PREFIXES = ("_t:", "S-")


def main() -> int:
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT id, ts, session, question FROM turns ORDER BY id")]

    def is_test(r) -> bool:
        return (r["session"] or "").startswith(TEST_PREFIXES)

    drop = [r for r in rows if is_test(r)]
    print(f"共 {len(rows)} 条，判定为测试数据 {len(drop)} 条")
    for r in drop:
        print(f"  #{r['id']:>3} {r['ts'][11:16]} {r['session'] or '(空)':<14} "
              f"{r['question'][:30]}")
    if dry or not drop:
        con.close()
        return 0

    bak = DB.with_name(f"history.db.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(DB, bak)
    print("备份:", bak.name)

    con.executemany("DELETE FROM turns WHERE id=?", [(r["id"],) for r in drop])
    con.commit()
    left = con.execute("SELECT count(*) FROM turns").fetchone()[0]
    dup = con.execute(
        "SELECT count(*) FROM (SELECT question FROM turns GROUP BY trim(question) "
        "HAVING count(*) > 1)").fetchone()[0]
    print(f"删除完成：剩余 {left} 条，重复组 {dup}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
