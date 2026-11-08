"""DeepSeek 计费：把 token 用量换成人民币。

价格来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing （2026-11 实查）
  模型                     输入(缓存命中)      输入(缓存未命中)     输出
  deepseek-flash      空闲 0.02  高峰 0.04   空闲 1   高峰 2    空闲 4  高峰 8
  deepseek-v4-pro     空闲 0.15  高峰 0.30   空闲 4.5 高峰 9    空闲 13.5 高峰 27
  （单位：元 / 百万 tokens）

高峰时段：北京时间 周一至周五 9:00–12:00、14:00–18:00，且不含法定节假日；
其余时间（含周末、节假日全天）按空闲价计。

注意：法定节假日这里没有内置日历，一律按工作日计算，因此节假日会**高估**费用。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))

# 元 / 百万 tokens
PRICES = {
    "flash": {
        "hit": {"peak": 0.04, "off": 0.02},
        "miss": {"peak": 2.0, "off": 1.0},
        "out": {"peak": 8.0, "off": 4.0},
    },
    "pro": {
        "hit": {"peak": 0.30, "off": 0.15},
        "miss": {"peak": 9.0, "off": 4.5},
        "out": {"peak": 27.0, "off": 13.5},
    },
}


def tier_of(model: str) -> str:
    m = (model or "").lower()
    return "pro" if ("pro" in m) else "flash"


def is_peak(now: datetime | None = None) -> bool:
    now = now or datetime.now(CN)
    if now.weekday() >= 5:          # 周六、周日
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 <= t < 12 * 60) or (14 * 60 <= t < 18 * 60)


def normalize(usage: dict) -> dict:
    """把 API 的 usage 整理成统一字段；缺缓存明细时按全部未命中算（偏保守）。"""
    u = usage or {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    total = int(u.get("total_tokens") or (prompt + completion))
    hit = u.get("prompt_cache_hit_tokens")
    miss = u.get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        hit, miss = 0, prompt
    else:
        hit = int(hit or 0)
        miss = int(miss or max(0, prompt - hit))
    return {"prompt": prompt, "completion": completion, "total": total,
            "hit": hit, "miss": miss}


def cost_cny(usage: dict, model: str, when: datetime | None = None,
             peak: bool | None = None) -> dict:
    """返回 {cost, peak, tier, detail}；cost 单位元。

    `peak` 显式给定时优先于按 `when` 推算——复算历史记录要用当时存下的时段，
    不能用「现在是不是高峰」。
    """
    u = normalize(usage)
    tier = tier_of(model)
    peak = is_peak(when) if peak is None else bool(peak)
    band = "peak" if peak else "off"
    p = PRICES[tier]
    c_hit = u["hit"] / 1e6 * p["hit"][band]
    c_miss = u["miss"] / 1e6 * p["miss"][band]
    c_out = u["completion"] / 1e6 * p["out"][band]
    return {
        "cost": c_hit + c_miss + c_out,
        "peak": peak,
        "tier": tier,
        "band": "高峰" if peak else "空闲",
        "detail": {"hit": c_hit, "miss": c_miss, "out": c_out},
    }


def fmt_cny(v: float) -> str:
    if v <= 0:
        return "¥0"
    if v < 0.01:
        return f"¥{v:.4f}"
    if v < 1:
        return f"¥{v:.3f}"
    return f"¥{v:.2f}"


if __name__ == "__main__":
    import json
    demo = {"prompt_tokens": 9871, "completion_tokens": 288, "total_tokens": 10159,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 9871}
    for m in ("deepseek-flash", "deepseek-v4-pro"):
        r = cost_cny(demo, m)
        print(f"{m:<18} {fmt_cny(r['cost'])}  [{r['band']}]  tier={r['tier']}")
    print("高峰判定示例：", is_peak(datetime(2026, 11, 7, 10, 30, tzinfo=CN)),
          "| 周末10:30 =", is_peak(datetime(2026, 11, 8, 10, 30, tzinfo=CN)))
