"""Build pgr.db from data/dialogue.jsonl.

Tables
  stages  one row per stage (metadata + flattened text)
  lines   one row per spoken line / paragraph, for retrieval
  fts     FTS5 trigram index over line bodies (>=3 chars only; 2-char falls back to LIKE)
  speakers name -> line count, used for entity detection

Run: python build_index.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time

from .paths import DATA as _DATA

DATA = str(_DATA)
DB = os.path.join(DATA, "pgr.db")
BUILD = DB + ".new"          # 先建临时库，成功后再原子替换


def main() -> int:
    src = os.path.join(DATA, "dialogue.jsonl")
    if not os.path.exists(src):
        print("missing", src)
        return 1
    if os.path.exists(BUILD):
        os.remove(BUILD)

    con = sqlite3.connect(BUILD)
    con.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE stages(
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            chapter TEXT,
            category TEXT,
            seq TEXT,
            stage_no TEXT,
            prev TEXT,
            next TEXT,
            nchars INTEGER,
            nlines INTEGER,
            txt TEXT
        );
        CREATE TABLE lines(
            id INTEGER PRIMARY KEY,
            stage_id INTEGER NOT NULL,
            ord INTEGER,
            who TEXT,
            body TEXT,
            full TEXT
        );
        CREATE TABLE speakers(name TEXT PRIMARY KEY, n INTEGER);
        CREATE TABLE bigrams(b TEXT PRIMARY KEY, n INTEGER);
        """
    )

    t0 = time.time()
    stage_rows, line_rows, spk = [], [], {}
    sid = 0
    lid = 0
    with open(src, encoding="utf-8") as f:
        for raw in f:
            r = json.loads(raw)
            sid += 1
            stage_rows.append(
                (sid, r["title"], r["chapter"], r["category"], r["seq"], r["stage_no"],
                 r["prev"], r["next"], r["chars"], r["lines"], r["text"])
            )
            ordn = 0
            for b in r["blocks"]:
                for ln in _lines_of(b):
                    ordn += 1
                    lid += 1
                    who, body = ln
                    # search text carries the speaker: names live in `who`, and a
                    # character lookup that only scans `body` finds almost nothing
                    full = f"{who}：{body}" if who else body
                    line_rows.append((lid, sid, ordn, who, body, full))
                    if who:
                        spk[who] = spk.get(who, 0) + 1
            if sid % 800 == 0:
                print(f"  {sid} stages...", flush=True)

    # 两字词频：查询时用它判断"某个两字词像不像专名"，
    # 否则每次提问都要对 42 万行做一遍 LIKE 全表扫描（实测 ~600ms）
    from collections import Counter
    bg: Counter = Counter()
    for r in stage_rows:
        txt = r[10] or ""
        for m in re.finditer(r"[\u3400-\u4dbf\u4e00-\u9fff]+", txt):
            run = m.group(0)
            for i in range(len(run) - 1):
                bg[run[i : i + 2]] += 1

    con.executemany(
        "INSERT INTO stages VALUES (?,?,?,?,?,?,?,?,?,?,?)", stage_rows)
    con.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?)", line_rows)
    con.executemany("INSERT INTO speakers VALUES (?,?)", spk.items())
    con.executemany("INSERT INTO bigrams VALUES (?,?)", bg.items())
    con.commit()
    print(f"stages={len(stage_rows)} lines={len(line_rows)} speakers={len(spk)} "
          f"bigrams={len(bg)} in {time.time()-t0:.1f}s")

    t0 = time.time()
    con.execute(
        "CREATE VIRTUAL TABLE fts USING fts5(full, content='lines', content_rowid='id', tokenize='trigram')")
    con.execute("INSERT INTO fts(fts) VALUES('rebuild')")
    con.execute("CREATE INDEX idx_lines_stage ON lines(stage_id)")
    con.execute("CREATE INDEX idx_stages_chapter ON stages(chapter)")
    con.execute("CREATE INDEX idx_stages_seq ON stages(seq)")
    con.commit()
    print(f"fts built in {time.time()-t0:.1f}s")
    con.close()
    con.close()
    os.replace(BUILD, DB)          # 原子替换：失败时旧库仍可用
    print(f"db = {os.path.getsize(DB)/1e6:.1f} MB")
    return 0


def _lines_of(block: dict):
    """Yield (who, body) for retrieval; choices are flattened in order."""
    t = block["t"]
    if t == "say":
        yield (block["who"], block["text"])
    elif t == "battle":
        yield ("", f"[战斗提示]{block['text']}")
    elif t == "choice":
        for o in block["options"]:
            if o["label"]:
                yield ("", f"[选项]{o['label']}")
            for sb in o["blocks"]:
                yield from _lines_of(sb)


if __name__ == "__main__":
    sys.exit(main())
