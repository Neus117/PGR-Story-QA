"""Volume audit over parsed dialogue.jsonl."""
from __future__ import annotations

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import json
import os
from collections import Counter, defaultdict

from pgr.paths import DATA as _DATA

DATA = str(_DATA)


def main() -> None:
    rows = []
    with open(os.path.join(DATA, "dialogue.jsonl"), encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))

    total_chars = sum(r["chars"] for r in rows)
    total_lines = sum(r["lines"] for r in rows)
    print(f"stages           : {len(rows)}")
    print(f"chapters         : {len({(r['category'], r['chapter']) for r in rows})}")
    print(f"total characters : {total_chars:,}")
    print(f"total dialogue   : {total_lines:,} lines")
    print(f"avg chars/stage  : {total_chars / max(1, len(rows)):,.0f}")
    print(f"rough tokens     : {total_chars * 0.75:,.0f} (chars*0.75)")

    print("\n-- by category (chars / stages / lines) --")
    agg = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        a = agg[r["category"] or "(none)"]
        a[0] += r["chars"]
        a[1] += 1
        a[2] += r["lines"]
    for cat, (ch, st, ln) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        print(f"  {cat:<10} {ch:>10,}  {st:>5} stages  {ln:>7,} lines")

    print("\n-- 15 largest stages --")
    for r in sorted(rows, key=lambda r: -r["chars"])[:15]:
        print(f"  {r['chars']:>7,}  {r['chapter']} / {r['stage_no'] or r['title']}")

    print("\n-- 10 largest chapters --")
    ch_agg = Counter()
    for r in rows:
        ch_agg[(r["category"], r["chapter"])] += r["chars"]
    for (cat, ch), c in ch_agg.most_common(10):
        print(f"  {c:>8,}  [{cat}] {ch}")

    print("\n-- speakers --")
    sp = Counter()
    for r in rows:
        for b in r["blocks"]:
            if b["t"] == "say" and b["who"]:
                sp[b["who"]] += 1
            elif b["t"] == "choice":
                for o in b["options"]:
                    for sb in o["blocks"]:
                        if sb["t"] == "say" and sb["who"]:
                            sp[sb["who"]] += 1
    print(f"  unique speakers: {len(sp)}")
    for name, c in sp.most_common(15):
        print(f"    {name}: {c}")

    print("\n-- empties --")
    empt = [r for r in rows if r["chars"] < 50]
    print(f"  stages with <50 chars: {len(empt)}")
    for r in empt[:15]:
        print(f"    {r['chars']:>5}  {r['chapter']} / {r['title']}")


if __name__ == "__main__":
    main()
