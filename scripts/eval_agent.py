"""固定题集回归：每次改动都跑一遍，报通过率，不再靠人手一个个试。

每题带自动判据（都是可机检的）：
  expect  答案里必须出现其中任一词（证明讲到了点上）
  forbid  答案里不能出现的词（常见错误：把"没查到"当结论）
  min/max 答案长度区间
  cite    是否必须带出处标记【…】
用法：
  python eval_agent.py            # 跑全部
  python eval_agent.py 3          # 只跑前 3 题
  python eval_agent.py --json out.json
"""
from __future__ import annotations

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import json
import sys
import time

from pgr import agent

CASES = [
    # ---- 人物介绍 ----
    dict(q="给我介绍一下诺安", expect=["寒羊", "枯朽为灯"], cite=True, min_len=300),
    dict(q="薇拉是谁", expect=["黑野", "21号", "三头犬"], cite=True, min_len=300),
    dict(q="21号是个什么样的角色", expect=["三头犬", "薇拉"], cite=True, min_len=250),
    dict(q="蒲牢是谁", expect=["九龙", "曲"], cite=True, min_len=200),
    dict(q="加百列是什么人", expect=["升格", "露娜"], cite=True, min_len=300),
    dict(q="赛琳娜是谁", expect=["构造体"], cite=True, min_len=200),

    # ---- 角色故事 ----
    dict(q="讲讲薇拉的故事", expect=["三头犬", "21号"], cite=True, min_len=800),
    dict(q="讲讲诺安的故事", expect=["复制", "枯朽为灯"], cite=True, min_len=800),
    dict(q="讲讲阿尔法的故事", expect=["露娜"], cite=True, min_len=600),

    # ---- 章节概述 ----
    dict(q="你能简述一下绝海星火的内容吗", expect=["亚特兰蒂斯"], cite=True, min_len=400),
    dict(q="第12章讲了什么", expect=["九龙"], cite=True, min_len=400),
    dict(q="摇篮游行这一章讲了什么", expect=["惑砂", "诺安"], cite=True, min_len=400),
    dict(q="枯朽为灯讲了个什么故事", expect=["诺安"], cite=True, min_len=600),

    # ---- 台词/事件定位 ----
    dict(q="诺安第一次出场是在哪里", expect=["枯朽为灯"], cite=True, min_len=100),
    dict(q="露西亚第一次登场是在哪里", expect=["序章", "0-1"], cite=True, min_len=100),
    dict(q="哪里提到过「筛选的时间不多了」", expect=["加百列", "筛选"], cite=True, min_len=80),
    dict(q="贝特是谁杀的", expect=["诺安"], cite=True, min_len=80),

    # ---- 关系 ----
    dict(q="露娜和阿尔法是什么关系", expect=["姐姐", "姐妹"], cite=True, min_len=150),
    dict(q="21号和薇拉是什么关系", expect=["三头犬", "队长"], cite=True, min_len=150),
    dict(q="诺安和指挥官是什么关系", expect=["信任", "同行", "指挥官"], cite=True, min_len=200),

    # ---- 设定 ----
    dict(q="帕弥什病毒是什么", expect=["感染", "帕弥什"], cite=True, min_len=200),
    dict(q="意识海是什么", expect=["意识"], cite=True, min_len=200),
    dict(q="升格者是什么", expect=["升格"], cite=True, min_len=150),

    # ---- 追问（带上下文） ----
    dict(q="莫瑞的情报来源是什么", expect=["推断", "来源"], cite=True, min_len=200),
    dict(q="你根据已知信息推断呢", history_hint="莫瑞的情报来源是什么",
         expect=["莫瑞", "推断"], cite=False, min_len=150),
    dict(q="小梅这个称呼是谁", expect=["伊什梅尔"], cite=True, min_len=100),

    # ---- 边界：不该硬编 ----
    dict(q="战双里有叫王大锤的角色吗", expect=["没有", "未出现", "找不到"], forbid=[], cite=False, min_len=20),
]


def check(case: dict, result: dict) -> tuple[bool, list[str]]:
    ans = result.get("answer") or ""
    problems = []
    if result.get("mode") in ("error", "max_steps"):
        problems.append(f"mode={result.get('mode')} {result.get('note','')[:60]}")
    if not ans:
        problems.append("答案为空")
        return False, problems
    if len(ans) < case.get("min_len", 0):
        problems.append(f"太短({len(ans)}<{case['min_len']})")
    if case.get("max_len") and len(ans) > case["max_len"]:
        problems.append(f"过长({len(ans)}>{case['max_len']})")
    expect = case.get("expect") or []
    if expect and not any(e in ans for e in expect):
        problems.append(f"未提到关键词{expect}")
    if case.get("cite") and "【" not in ans:
        problems.append("没有出处标记")
    for bad in case.get("forbid") or []:
        if bad in ans:
            problems.append(f"出现禁用词「{bad}」")
    return (not problems), problems


def main() -> int:
    limit = None
    out_json = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--json" and i + 1 < len(args):
            out_json = args[i + 1]
        elif a.isdigit():
            limit = int(a)
    cases = CASES[:limit] if limit else CASES

    rows, passed, total_cost = [], 0, 0.0
    t0 = time.time()
    for idx, case in enumerate(cases, 1):
        history = []
        if case.get("history_hint"):
            history = [{"question": case["history_hint"], "answer": "（略）"}]
        t1 = time.time()
        try:
            res = agent.run(case["q"], history)
        except Exception as e:
            res = {"answer": "", "mode": "error", "note": f"{type(e).__name__}: {e}"}
        dt = time.time() - t1
        cost = agent.cost_of(res)
        total_cost += cost["cost"]
        ok, problems = check(case, res)
        passed += ok
        rows.append({"q": case["q"], "ok": ok, "problems": problems,
                     "mode": res.get("mode"), "steps": res.get("steps"),
                     "tools": [t["tool"] for t in res.get("tool_calls", [])],
                     "secs": round(dt, 1), "cost": cost["cn"],
                     "answer": (res.get("answer") or "")[:1500]})
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {idx:>2}. {case['q']}  ({dt:.0f}s, {cost['cn']}, "
              f"{res.get('steps')} 步)")
        for p in problems:
            print(f"        × {p}")
    dt_all = time.time() - t0
    print(f"\n通过 {passed}/{len(cases)}  用时 {dt_all/60:.1f} 分钟  合计 {agent.pricing.fmt_cny(total_cost)}")
    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print("明细已写入", out_json)
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
