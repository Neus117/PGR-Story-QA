"""花费审计：用历史里存下的用量拆分复算每次提问的开销，并与当时的记录值对照。

    python scripts/audit_cost.py

只统计 prompt_tokens 有值的记录（本功能上线后才写入）；老记录会单独计数并跳过。
复算用的是**当前**价目表与当时记录的时段（peak 列），所以调价后跑这个就能看出差额。
"""
from __future__ import annotations

import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pgr import pricing                      # noqa: E402
from pgr.history import History              # noqa: E402


def quantile(vals: list[float], q: float) -> float:
    v = sorted(vals)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def main() -> int:
    h = History()
    rows = [dict(r) for r in h.con.execute(
        "SELECT * FROM turns WHERE mode='agent' ORDER BY id")]
    full = [r for r in rows if r.get("prompt_tokens") is not None]
    old = [r for r in rows if r.get("prompt_tokens") is None]

    print(f"agent 记录 {len(rows)} 条：可复算 {len(full)} 条，旧记录（无用量拆分）{len(old)} 条")
    if old:
        print(f"  旧记录合计 ¥{sum(r['cost_cny'] or 0 for r in old):.4f}，无法复算，已跳过")

    if not full:
        print("没有可复算的记录：跑几次提问后再看")
        return 0

    print(f"\n{'#':>4} {'prompt':>8} {'hit':>8} {'miss':>8} {'out':>6} {'时段':>6} "
          f"{'记录¥':>8} {'复算¥':>8} {'差':>8}")
    diffs = []
    for r in full:
        usage = {"prompt_tokens": r["prompt_tokens"],
                 "completion_tokens": r["completion_tokens"],
                 "prompt_cache_hit_tokens": r["cache_hit_tokens"],
                 "prompt_cache_miss_tokens": r["cache_miss_tokens"]}
        now = pricing.cost_cny(usage, r["model"] or "", peak=bool(r["peak"]))
        diff = now["cost"] - (r["cost_cny"] or 0)
        diffs.append(diff)
        print(f"{r['id']:>4} {r['prompt_tokens']:>8,} {r['cache_hit_tokens'] or 0:>8,} "
              f"{r['cache_miss_tokens'] or 0:>8,} {r['completion_tokens'] or 0:>6,} "
              f"{'高峰' if r['peak'] else '空闲':>6} "
              f"{r['cost_cny']:>8.4f} {now['cost']:>8.4f} {diff:>+8.4f}")

    costs = [r["cost_cny"] for r in full]
    print(f"\n记录值：均值 ¥{st.mean(costs):.4f} · 中位 ¥{st.median(costs):.4f} · "
          f"P90 ¥{quantile(costs, .9):.4f}")
    worst = max(abs(d) for d in diffs)
    print(f"复算与记录的偏差：最大 ¥{worst:.4f}，均值 ¥{st.mean(diffs):+.5f}")
    if worst > 0.0005:
        print("  偏差偏大：可能是价目表更新过，或记录时的时段判断与现在不同")
    else:
        print("  偏差在价目取整范围内，记录可信")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
