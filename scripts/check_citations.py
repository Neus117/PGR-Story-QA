"""出处门禁：库里每条回答的「分类／章节…」形式标签都必须能对回关卡。

    python scripts/check_citations.py

只把含 ／ 的标签当出处（正文里还有【万世铭】这种强调用法）。
模型自己写的「未核实：…」不算失败。有对不上的返回 1。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pgr.tools import Toolbox  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
CITE = re.compile(r"【([^】]{2,80})】")


def main() -> int:
    box = Toolbox()
    db = sys.argv[sys.argv.index('--db') + 1] if '--db' in sys.argv else str(DATA / "history.db")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    turns = [dict(r) for r in con.execute("SELECT id, answer FROM turns ORDER BY id")]
    con.close()

    checked = failed = 0
    samples = []
    for t in turns:
        for m in CITE.findall(t["answer"] or ""):
            if "／" not in m or m.startswith("未核实："):
                continue
            checked += 1
            if not box.resolve_row(m):
                failed += 1
                if len(samples) < 8:
                    samples.append((t["id"], m))

    print(f"检查 {len(turns)} 条回答里的 {checked} 个出处标注")
    if failed:
        print(f"对不上 {failed} 个：")
        for tid, m in samples:
            print(f"  #{tid}  {m}")
        return 1
    print("出处门禁：通过（全部能对回关卡）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
