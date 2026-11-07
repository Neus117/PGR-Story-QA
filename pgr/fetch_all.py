"""Bulk-download every stage page's wikitext via action=query&export=1.

Resumable: each batch is cached on disk by wiki.Wiki, so re-running picks up
where it stopped.
"""
from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET

from .wiki import Wiki, BASE
from .paths import DATA as _DATA

DATA = str(_DATA)
BATCH = 50
NS = "{http://www.mediawiki.org/xml/export-0.11/}"


def main() -> int:
    with open(os.path.join(DATA, "stages.json"), encoding="utf-8") as f:
        stages = json.load(f)
    titles = [s["title"] for s in stages]
    print(f"{len(titles)} titles, {BATCH}/batch -> {-(-len(titles)//BATCH)} requests", flush=True)

    w = Wiki(min_interval=4.0)
    raw_dir = os.path.join(DATA, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    missing = [t for t in titles if not os.path.exists(os.path.join(raw_dir, t.replace("/", "_") + ".txt"))]
    print(f"still missing: {len(missing)}", flush=True)

    for i in range(0, len(missing), BATCH):
        chunk = missing[i : i + BATCH]
        idx = i // BATCH
        try:
            body = w.post(
                f"{BASE}/api.php",
                {
                    "action": "query",
                    "export": "1",
                    "exportnowrap": "1",
                    "format": "xml",
                    "titles": "|".join(chunk),
                },
                "api",
                "export50:" + "|".join(chunk),
            )
        except Exception as e:
            print(f"[batch {idx}] FAILED: {e}", flush=True)
            return 1
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            print(f"[batch {idx}] XML parse error: {e}", flush=True)
            print(body[:400], flush=True)
            return 1
        got = 0
        for page in root.iter(NS + "page"):
            t = page.findtext(NS + "title") or ""
            rev = page.find(NS + "revision")
            text = ""
            if rev is not None:
                text = rev.findtext(NS + "text") or ""
            if not t:
                continue
            with open(os.path.join(raw_dir, t.replace("/", "_") + ".txt"), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            got += 1
        print(f"[batch {idx}] {i+len(chunk)}/{len(missing)} pages={got} req#{w.n_requests}", flush=True)

    have = len(os.listdir(raw_dir))
    print(f"DONE raw files: {have}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
