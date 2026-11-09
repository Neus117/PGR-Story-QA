"""Agent 循环：把问题交给模型，由它自己决定调哪些工具、调几次，最后作答。

没有意图正则，没有路由阈值。模型看到工具和数据，自己规划。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import llm, pricing
from .tools import TOOL_SPECS, Toolbox

MAX_STEPS = 8          # 历史步数中位 7、P75 8；10 步的样本有 5 条且最慢
MAX_READS = 6          # 最多读几段原文，防止无节制通读
MAX_PROMPT_TOKENS = 240000     # 单次提问的累计预算，超了就强制收敛
TOOL_RESULT_CAP = 14000        # 单个工具结果进上下文的上限（字符）

SYSTEM = """你是《战双帕弥什》的剧情资料助手。你有一批工具可以查本地剧情库（3692 个关卡、1113 万字原文）和站外资料。

工作方式：
- **先查再答**。不确定某个角色/章节是否存在，就用工具查，不要凭印象下结论。
- 需要理解剧情经过时，用 read_chapter 读原文；需要定位某句台词时，用 search_lines。
- 一次可以连续调多个工具；读到的内容不够就再读，不要拿"没查到"当结论。
- 章节名一律照工具返回的原名使用，不要自己编号或改叫法。

检索预算（务必遵守）：
- 总共最多 6 次检索就要收口作答。多数问题 3~4 次检索已经够。
- 需要上下文时**优先 read_chapter 一次读完整章**，不要用 search_lines 反复试探。
- 拿到 2~3 条关键证据就停下来回答，不要为了"更全"继续翻。

回答要求：
0. **全文控制在 700 字以内（硬上限 800）**。角色故事类尤其注意：只写结论与几条关键依据，其余用出处覆盖，
   不要复述大段原文。宁可少写，把覆盖交给【分类／章节／关卡】标注。
1. 中文，先给结论，再给依据。
2. 事实性结论后面标注出处，格式【分类／章节／关卡】，取自工具结果里的 cite。
3. 原文没有直接记载但有线索时，明确写「推断」并列出依据；连线索都没有才说「没有相关数据」。
4. 站外检索结果只能用来确认别名与设定，**不能当作剧情原文引用**。
5. 不写「根据工具结果」这类话，直接讲内容。
"""


def _chat(messages: list[dict], tools: list[dict] | None,
          max_tokens: int = 3000) -> tuple[dict, dict, str]:
    cfg = llm.load_config()
    if not cfg["api_key"]:
        return {}, {}, "未配置 API Key"
    payload = {"model": cfg["model"], "messages": messages,
               "temperature": 0.3, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        usage = data.get("usage", {}) or {}
        usage["_model"] = data.get("model") or cfg["model"]
        return data["choices"][0]["message"], usage, ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {}, {}, f"HTTP {e.code}：{detail}"
    except Exception as e:
        return {}, {}, f"{type(e).__name__}: {e}"


def run(question: str, memories: list[dict] | None = None,
        history: list[dict] | None = None) -> dict:
    """返回 {answer, mode, steps, tool_calls, usage, note}。

    `memories` 是最近 N 轮的一句话摘要（服务端从库里取），只用于理解指代；
    细节一律重新检索。`history` 保留兼容，等价于把全文转成摘要。
    """
    box = Toolbox()
    messages: list[dict] = [{"role": "system", "content": SYSTEM}]
    mem = list(memories or [])
    if not mem and history:                     # 兼容旧调用：把全文当摘要用
        mem = [{"question": t.get("question"), "summary": (t.get("answer") or "")[:80]}
               for t in history]
    if mem:
        lines = []
        for i, it in enumerate(mem[-10:], 1):
            q = (it.get("question") or "").strip()
            sm = (it.get("summary") or "").strip()
            if q or sm:
                lines.append(f"{i}. 问：{q} → {sm}")
        if lines:
            messages.append({"role": "system", "content":
                "以下是之前几轮问答的摘要，用于理解指代和延续话题；"
                "涉及具体情节与出处时必须重新检索，不要凭摘要作答：\n"
                + "\n".join(lines)})
    messages.append({"role": "user", "content": question})

    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
    trace: list[dict] = []
    last_text = ""
    reads = 0
    nudged = False

    for step in range(MAX_STEPS):
        over_budget = total["total_tokens"] > MAX_PROMPT_TOKENS
        # 读够原文、或快到步数上限时，收走工具逼它作答——否则会一直读下去
        must_answer = over_budget or reads >= MAX_READS or step >= MAX_STEPS - 1
        if must_answer and not nudged:
            messages.append({"role": "system", "content":
                "阅读预算已用完。现在必须基于已经读到的内容直接回答；"
                "没覆盖到的部分，说明你的答案覆盖了哪些章节即可，不要再调工具。"})
            nudged = True
        msg, usage, err = _chat(messages, None if must_answer else TOOL_SPECS,
                                max_tokens=1200 if must_answer else 3000)
        if err:
            return {"answer": last_text, "mode": "error", "steps": step,
                    "tool_calls": trace, "usage": total, "note": err}
        for k, v in usage.items():
            if isinstance(v, int):
                total[k] = total.get(k, 0) + v
        total["_model"] = usage.get("_model") or total.get("_model")

        calls = msg.get("tool_calls") or []
        text = (msg.get("content") or "").strip()
        if text:
            last_text = text
        if not calls:
            final, fixes = box.verify_and_fix(text or last_text)
            note = ""
            if any(f["action"] == "unverified" for f in fixes):
                bad = [f["from"] for f in fixes if f["action"] == "unverified"]
                note = "以下出处未能在剧情库中核实，可能不准确：" + "、".join(bad[:5])
            return {"answer": final, "mode": "agent", "steps": step + 1,
                    "tool_calls": trace, "usage": total, "note": note,
                    "citation_fixes": fixes}

        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            name = fn.get("name", "")
            result = box.dispatch(name, fn.get("arguments", "{}"))
            if len(result) > TOOL_RESULT_CAP:
                result = result[:TOOL_RESULT_CAP] + '…（结果过长已截断）"}'
            if name == "read_chapter":
                reads += 1
            trace.append({"tool": name, "args": fn.get("arguments", "")[:160]})
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": result})

    # 达到步数上限：拿最后一次文本收尾
    final, fixes = box.verify_and_fix(last_text or "")
    return {"answer": final or "（工具调用达到上限，未能完成回答）",
            "mode": "max_steps", "steps": MAX_STEPS, "tool_calls": trace,
            "usage": total, "note": "达到工具调用步数上限",
            "citation_fixes": fixes}


def cost_of(result: dict) -> dict:
    usage = result.get("usage") or {}
    r = pricing.cost_cny(usage, llm.load_config()["model"])
    u = pricing.normalize(usage)
    return {"cost": round(r["cost"], 6), "cn": pricing.fmt_cny(r["cost"]),
            "band": r["band"], "tier": r["tier"], "usage": u}


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "介绍一下诺安"
    out = run(q)
    print("mode:", out["mode"], "steps:", out["steps"])
    for t in out["tool_calls"]:
        print("  ->", t["tool"], t["args"][:80])
    c = cost_of(out)
    print(f"cost={c['cn']} tok={c['usage']['total']} 缓存命中={c['usage']['hit']}")
    print()
    print(out["answer"])
