"""Retrieval quality probe: a fixed question set with expected stage hints.

Each case asserts a *retrieval* property that we can check without an LLM:
  - does the entity get detected
  - does the chapter hint get detected
  - does the expected chapter appear in the top-k stages
Run: python eval_retrieval.py
"""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import re
import sys
import time

from pgr.search import Searcher

CASES = [
    # (question, expect substring in top chapter, expect intent)
    ("露西亚第一次出场", "序章", "first"),
    ("九龙环城的剧情是什么", "12 九龙环城", ""),
    ("第12章讲了什么", "12 九龙环城", ""),
    ("帕弥什病毒是什么", "", ""),
    ("灰鸦小队有哪些成员", "", ""),
    ("卡列尼娜提到奶茶", "", ""),
    ("指挥官在21章做了什么", "21 刻命螺旋", ""),
    ("阿尔法和露娜的关系", "", ""),
    ("红潮是什么", "", ""),
    ("丽芙第一次登场", "序章", "first"),
    ("加百列的结局", "", "last"),
    ("薇拉和里在永驰王座里", "永驰王座", ""),
]


def main() -> int:
    s = Searcher()
    ok = 0
    t_all = 0.0
    for q, want_ch, want_intent in CASES:
        t0 = time.time()
        ev = s.search(q)
        dt = (time.time() - t0) * 1000
        t_all += dt
        got_ch = [e["chapter"] for e in ev.excerpts]
        ch_ok = (not want_ch) or any(want_ch in c for c in got_ch)
        in_ok = (not want_intent) or ev.intent == want_intent
        ok += ch_ok and in_ok
        flag = "OK " if (ch_ok and in_ok) else "MISS"
        print(f"[{flag}] {q}")
        print(f"       {dt:6.0f} ms  ents={ev.entities} ch={ev.chapter_hint!r} "
              f"intent={ev.intent!r} chars={ev.chars}")
        for e in ev.excerpts:
            print(f"       -> {e['seq']} {e['title']}  [{e['chapter']}] score={e['score']}")
        if not ch_ok:
            print(f"       expected chapter containing {want_ch!r}")
        print()
    print(f"passed {ok}/{len(CASES)}   avg {t_all/len(CASES):.0f} ms/query")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
