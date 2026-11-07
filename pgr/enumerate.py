"""Enumerate every stage page carrying the 剧情分类 property and save it as JSON."""
from __future__ import annotations

import json
import os

from .wiki import Wiki
from .paths import DATA as _DATA

OUT = str(_DATA)
PAGE_LIMIT = 5000


def main() -> None:
    w = Wiki(min_interval=4.0)
    rows: dict[str, dict] = {}
    offset = 0
    while True:
        res = w.ask("[[剧情分类::+]]|?标题|?章节|?序号|?分类", limit=PAGE_LIMIT, offset=offset)
        if "error" in res:
            raise SystemExit("ASK ERROR: " + json.dumps(res["error"], ensure_ascii=False)[:800])
        query = res["query"]
        results = query.get("results", {})
        meta = query.get("meta", {})
        for title, data in results.items():
            po = data.get("printouts", {})
            cats = [c.get("fulltext", "").replace("分类:", "") for c in po.get("分类", []) if isinstance(c, dict)]
            rows[title] = {
                "title": title,
                "title_prop": (po.get("标题") or [""])[0] if po.get("标题") else "",
                "chapter": (po.get("章节") or [""])[0] if po.get("章节") else "",
                "seq": (po.get("序号") or [None])[0] if po.get("序号") else None,
                "cats": cats,
            }
        total = meta.get("count")
        print(f"offset={offset} got={len(results)} accumulated={len(rows)} total={total}", flush=True)
        if not results or total is None or offset + len(results) >= total:
            break
        offset += len(results)

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "stages.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(sorted(rows.values(), key=lambda r: (r["seq"] is None, r["seq"] or 0, r["title"])), f, ensure_ascii=False, indent=1)
    print(f"wrote {path}: {len(rows)} stages")

    from collections import Counter
    print("\nby chapter count:", len({r['chapter'] for r in rows.values()}))
    for name, n in Counter(c for r in rows.values() for c in r["cats"]).most_common(30):
        print(f"  {name}: {n}")


if __name__ == "__main__":
    main()
