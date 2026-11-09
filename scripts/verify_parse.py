import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import os, re, json
from collections import Counter

_ROOT = _pl.Path(__file__).resolve().parent.parent
RAW = str(_ROOT / "data" / "raw")
DATA = str(_ROOT / "data")

rows = [json.loads(l) for l in open(os.path.join(DATA, "dialogue.jsonl"), encoding="utf-8")]
by = {r["title"]: r for r in rows}

print("=== residue after v2 ===")
res = Counter()
ex = {}
for r in rows:
    for m in re.findall(r"\{\{[^{}]*\}\}", r["text"]):
        k = "tpl:" + m.split("|")[0]
        res[k] += 1
        ex.setdefault(k, (r["title"], m[:150]))
    for m in re.findall(r"\[\[[^\]]*\]\]", r["text"]):
        res["link"] += 1
        ex.setdefault("link", (r["title"], m[:150]))
    if re.search(r"</?[a-zA-Z]", r["text"]):
        res["tag"] += 1
        ex.setdefault("tag", (r["title"], re.search(r".{0,40}</?[a-zA-Z][^>]*>.{0,40}", r["text"]).group(0)))
    if "{{" in r["text"] or "}}" in r["text"]:
        res["brace"] += 1
        ex.setdefault("brace", (r["title"], r["text"][:150]))
for k, v in res.most_common(20):
    print(f"  {k}: {v}   e.g. {ex.get(k)}")

print("\n=== 提示 template usage sample ===")
seen = 0
for fn in os.listdir(RAW):
    t = open(os.path.join(RAW, fn), encoding="utf-8").read()
    for m in re.finditer(r"\{\{提示[|}]", t):
        i = m.start()
        j = t.find("}}", i)
        print(f"  [{fn}] {t[i:j+2][:260]!r}")
        seen += 1
        break
    if seen >= 5:
        break

print("\n=== parsed 1-1灰鸦过境: first 20 blocks ===")
r = by.get("1-1灰鸦过境")
if r:
    for b in r["blocks"][:20]:
        if b["t"] == "choice":
            print("  CHOICE:", [o["label"] for o in b["options"]])
        else:
            print(f"  {b['t']}: {b.get('who','')}| {b.get('text','')[:80]}")

print("\n=== 人物对话 page parsed ===")
r = by.get("冰心潜影")
if r:
    print("chars=", r["chars"])
    for b in r["blocks"][:10]:
        print(f"  {b['t']}: {b.get('who','')}| {str(b.get('text',''))[:100]}")

print("\n=== stages with 0 text (by chapter) ===")
c = Counter((r["category"], r["chapter"]) for r in rows if r["chars"] == 0)
for k, v in c.most_common():
    print(f"  {v:>3}  {k}")

print("\n=== choice / battle / extra block counts ===")
tc = Counter()
for r in rows:
    for b in r["blocks"]:
        tc[b["t"]] += 1
print(" ", dict(tc))
