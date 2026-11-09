"""Grounded answer generation via DeepSeek chat, with a hard context budget.

Design constraints:
  * single call, small context — the evidence pack is capped upstream at
    search.CTX_BUDGET (12k chars); here we additionally clamp the whole prompt
    to MAX_PROMPT_CHARS so a long question can never blow the request up.
  * no key -> evidence mode: we still return the exact excerpts and citations,
    so the product is useful (and testable) without any credential.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from .paths import DATA as _DATA

CONFIG = str(_DATA.parent / "config.json")

# An API key is "sk-" + 32 chars. A bare UUID (8-4-4-4-12) is what the DeepSeek
# console shows for *other* fields, and pasting it looks exactly like "no key
# configured" once the API answers 401.
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def key_shape_warning(key: str) -> str:
    """Return a human explanation when the key cannot possibly be an API key."""
    if not key:
        return ""
    if UUID_RE.match(key):
        return ("这串是标准 UUID（8-4-4-4-12 分段），不是 API Key。"
                "控制台里 UUID 是资源/账号 ID，API Key 是另一栏，形如 sk- 开头。")
    if not key.startswith("sk-"):
        return f"密钥不以 sk- 开头（当前长度 {len(key)}）。DeepSeek 官方密钥是 sk- + 32 位。"
    if len(key) < 30:
        return f"密钥只有 {len(key)} 位，短于正常的 35 位，可能没复制完整。"
    return ""

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
# ~6k Chinese chars of overhead is plenty for instructions; evidence is the rest.
MAX_PROMPT_CHARS = 16000
MAX_OUTPUT_TOKENS = 900

SYSTEM = """你是《战双帕弥什》剧情问答助手。你会拿到从 wiki 原文检索出的【证据】，
以及可选的【对话上下文】（此前的问题与回答）。

先判断手里的证据强度，再决定怎么答：

1. 证据里有直接记载 → 直接回答，每条结论后标注出处。
2. 证据里没有直接记载、但有相关线索 → **必须推断**，不要把"没有直接记载"当成"无法回答"：
   - 写出推断结论，并在最前面明确标「推断」；
   - 逐条列出你依据的线索，每条都要标出处；
   - 说清这个推断的可信度来自什么（例如"二人以队长与队员相称，可确认上下级关系"）。
3. 连相关线索都找不到 → 才说「没有相关数据」，并指出你检索到的最接近的关卡。

硬性规则：
- 出处格式：【分类／章节／关卡】，内容取自每段证据开头的【…／…／…】。
- **推断与原文记载必须分开写**，绝不许把推断说成原文。
- 证据里存在剧情分支时，要说明此处有分支。
- 引用关键台词用「」。
- 若用户追问（如"你推断呢""那她们呢"），沿用【对话上下文】里的上一个话题，不要另起炉灶。
- 中文，先给结论再给依据，控制在 400 字以内，不要复述整段原文。"""


def _chat(messages: list[dict], max_tokens: int = MAX_OUTPUT_TOKENS,
          temperature: float = 0.2) -> tuple[str, dict, str]:
    """返回 (文本, usage, 错误)。callers 自行决定怎么处理错误。"""
    cfg = load_config()
    if not cfg["api_key"]:
        return "", {}, "未配置 API Key"
    payload = {"model": cfg["model"], "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        usage = data.get("usage", {}) or {}
        # 回包里的 model 才是真正在服务的那个，和请求名可能不同
        usage["_model"] = data.get("model") or cfg["model"]
        return (data["choices"][0]["message"]["content"].strip(), usage, "")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return "", {}, f"HTTP {e.code}：{detail}"
    except Exception as e:
        return "", {}, f"{type(e).__name__}: {e}"


def _chat_stream(messages: list[dict], on_delta, max_tokens: int = 1200,
                 temperature: float = 0.3, tools: list | None = None
                 ) -> tuple[str, dict, str, list]:
    """流式调用：正文边收边回调 on_delta(text)，同时累积 tool_calls。

    返回 (全文, usage, 错误, tool_calls)。tool_calls 结构与非流式回包一致。
    """
    cfg = load_config()
    if not cfg["api_key"]:
        return "", {}, "未配置 API Key", []
    payload = {"model": cfg["model"], "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    parts, usage = [], {}
    calls: dict[int, dict] = {}
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except Exception:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                if obj.get("model"):
                    usage["_model"] = obj["model"]
                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        parts.append(piece)
                        try:
                            on_delta(piece)
                        except Exception:
                            pass
                    for tc in delta.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        cur = calls.setdefault(i, {"id": "", "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            cur["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            cur["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            cur["function"]["arguments"] += fn["arguments"]
        return ("".join(parts).strip(), usage, "",
                [calls[k] for k in sorted(calls)])
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return "".join(parts).strip(), usage, f"HTTP {e.code}：{detail}", []
    except Exception as e:
        return "".join(parts).strip(), usage, f"{type(e).__name__}: {e}", []


SUMMARIZE_BATCH = """你在为《战双帕弥什》玩家整理剧情梗概。下面是同一章里连续的几段关卡原文。

要求：
- 用连贯的叙述写这一段发生了什么，按原文顺序推进；
- 保留关键人物、冲突、转折、重要的台词信息；
- 不要写「证据显示」「关卡名」，不要逐条列出处；
- 150~250 字。"""

SUMMARIZE_MERGE = """下面是同一章按顺序分段的剧情梗概。请合并成一条完整、连贯的故事概述。

要求：
- **总长必须控制在 600 字以内**，超了就是不合格，宁可删细节也不要超；
- 这是压缩重写，不是逐段转述：删掉重复与次要支线，只留主线冲突、关键转折、人物动机；
- 不要出现「段落一」「证据」「出处」这类词；
- 结尾一句点明这一章留下的悬念或后果。"""


SUMMARIZE_CHAR_BATCH = """你在为《战双帕弥什》玩家整理某角色的人物线。下面是按剧情顺序排列的若干关卡原文片段（只含该角色出场较多的关卡）。

要求：
- 用叙述交代这一段里这个角色做了什么、遇到谁、关系或立场发生了什么变化；
- **写清时间和地点锚点**（哪一章、哪场战役、什么组织/地点），不要只写事件本身；
- 保留关键台词与大转折，忽略战斗过程细节；
- 不要写「证据」字样；但章节名要照每段开头的《章节／关卡》如实引用，不要自己编章节编号；
- 200~300 字。"""

SUMMARIZE_CHAR_MERGE = """下面是同一角色按剧情顺序分段的人物线梗概。请合并成一篇完整的角色故事。

要求：
- **1000~1600 字**，按时间顺序讲这个角色经历了什么，像一篇人物小传；
- **开篇必须交代身份与背景**：她是谁、属于哪个势力/组织、故事发生时世界处在什么状态；
- 每个阶段都要有**时间或章节锚点**，且**必须照证据里出现的原名引用**（如"烬海异途"、"烛灯刑"），不要自己编号或猜章节序号；
- 突出：起点与身份、关键抉择、与他人的关系变化、代价与结局走向；
- **每一段梗概都必须在成文里有落点**，不许整段跳过；宁可压缩措辞，也不要丢事件；
- 不要出现「段落一」「证据」「出处」这类词；
- 若不同剧情线（主线/间章/好感度）对同一角色有不同侧面，用一句自然过渡说明，不要罗列。"""


SUMMARIZE_BIO_MERGE = """下面是某角色按剧情顺序分段整理的材料。请写成一段**人物介绍**。

要求：
- **400~600 字**，回答"这个人是谁"：身份、所属势力、性格、与主角的关系、最关键的经历；
- 开篇一句话点明身份与所处时代背景；
- 不要写成编年史，抓最能定义这个人的三四件事；
- 章节名照材料里的原名引用，不要自己编号。"""


def summarize_character(name: str, stages: list[dict], batch_chars: int = 15000,
                        style: str = "story") -> dict:
    """角色线 map-reduce：分段叙述 → 合并成人物小传。"""
    cfg = load_config()
    if not cfg["api_key"]:
        return {"mode": "evidence", "answer": "", "note": "未配置 API Key", "batches": 0}
    batches, cur, size = [], [], 0
    for st in stages:
        txt = (st.get("txt") or "").strip()
        if not txt:
            continue
        piece = f"《{st.get('chapter')}／{st.get('stage_no') or st.get('title')}》\n{txt}"
        if cur and size + len(piece) > batch_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(piece)
        size += len(piece)
    if cur:
        batches.append(cur)

    partials, used = [], {}
    for i, b in enumerate(batches, 1):
        text, usage, err = _chat([
            {"role": "system", "content": SUMMARIZE_CHAR_BATCH},
            {"role": "user", "content": f"角色：{name}\n\n" + "\n\n".join(b)},
        ], max_tokens=700)
        if err:
            return {"mode": "error", "answer": "", "note": f"第 {i} 段失败：{err}", "batches": len(batches)}
        partials.append(text)
        used["_model"] = usage.get("_model") or cfg["model"]
        for k, v in usage.items():
            if isinstance(v, int):
                used[k] = used.get(k, 0) + v

    merge_prompt = SUMMARIZE_BIO_MERGE if style == "bio" else SUMMARIZE_CHAR_MERGE
    merged, usage2, err = _chat([
        {"role": "system", "content": merge_prompt},
        {"role": "user", "content": f"角色：{name}\n\n" + "\n\n".join(
            f"【第{i}段】{t}" for i, t in enumerate(partials, 1))},
    ], max_tokens=(1000 if style == "bio" else 3000), temperature=0.3)
    if err:
        return {"mode": "partial", "answer": "\n\n".join(partials),
                "note": f"合并失败：{err}", "batches": len(batches)}
    used["_model"] = usage2.get("_model") or used.get("_model") or cfg["model"]
    for k, v in usage2.items():
        if isinstance(v, int):
            used[k] = used.get(k, 0) + v
    return {"mode": "story", "answer": merged, "note": "", "batches": len(batches),
            "usage": used, "stage_count": len(stages),
            "chars": sum(len(st.get("txt") or "") for st in stages)}


def summarize_chapter(question: str, stages: list[dict], history=None,
                      batch_chars: int = 12000) -> dict:
    """整章 map-reduce：分段归纳 → 合并成故事。"""
    cfg = load_config()
    if not cfg["api_key"]:
        return {"mode": "evidence", "answer": "", "note": "未配置 API Key", "batches": 0}

    batches, cur, size = [], [], 0
    for st in stages:
        txt = (st.get("txt") or "").strip()
        if not txt:
            continue
        piece = f"《{st.get('stage_no') or st.get('title')}》\n{txt}"
        if cur and size + len(piece) > batch_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(piece)
        size += len(piece)
    if cur:
        batches.append(cur)

    partials, used = [], {}
    for i, b in enumerate(batches, 1):
        text, usage, err = _chat([
            {"role": "system", "content": SUMMARIZE_BATCH},
            {"role": "user", "content": "\n\n".join(b)},
        ], max_tokens=600)
        if err:
            return {"mode": "error", "answer": "", "note": f"第 {i} 段归纳失败：{err}", "batches": len(batches)}
        partials.append(text)
        used["_model"] = usage.get("_model") or cfg["model"]
        for k, v in usage.items():
            if isinstance(v, int):
                used[k] = used.get(k, 0) + v

    merged, usage2, err = _chat([
        {"role": "system", "content": SUMMARIZE_MERGE},
        {"role": "user", "content": "\n\n".join(
            f"【第{i}段】{t}" for i, t in enumerate(partials, 1))},
    ], max_tokens=1600, temperature=0.3)
    if err:
        return {"mode": "partial", "answer": "\n\n".join(partials),
                "note": f"合并失败：{err}", "batches": len(batches)}
    used["_model"] = usage2.get("_model") or used.get("_model") or cfg["model"]
    for k, v in usage2.items():
        if isinstance(v, int):
            used[k] = used.get(k, 0) + v
    return {"mode": "summary", "answer": merged, "note": "",
            "batches": len(batches), "usage": used,
            "stage_count": len(stages), "chars": sum(len(st.get("txt") or "") for st in stages)}


def load_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            # utf-8-sig: PowerShell 5.1 writes a BOM that plain utf-8 chokes on
            with open(CONFIG, encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    key = (os.environ.get("PGR_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
           or (cfg.get("api_key") or "").strip())
    return {
        "base": os.environ.get("PGR_LLM_BASE") or cfg.get("base") or DEFAULT_BASE,
        "model": os.environ.get("PGR_LLM_MODEL") or cfg.get("model") or DEFAULT_MODEL,
        "api_key": key,
        "timeout": int(cfg.get("timeout") or 90),
    }


def available() -> bool:
    return bool(load_config()["api_key"])


def diagnose() -> dict:
    """Explain *why* we are (or are not) able to call the model.

    A wrong-file mistake (key pasted into config.example.json) used to look
    identical to "no key at all", which wasted a round trip. Say it out loud.
    """
    info = {"config_path": CONFIG, "exists": os.path.exists(CONFIG),
            "source": "", "key_len": 0, "parse_error": "", "hint": ""}
    env_key = os.environ.get("PGR_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    file_key = ""
    if info["exists"]:
        try:
            with open(CONFIG, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            file_key = (cfg.get("api_key") or "").strip()
        except Exception as e:
            info["parse_error"] = f"{type(e).__name__}: {e}"
    example = CONFIG.replace("config.json", "config.example.json")
    example_key = ""
    if os.path.exists(example):
        try:
            with open(example, encoding="utf-8-sig") as f:
                example_key = ((json.load(f) or {}).get("api_key") or "").strip()
        except Exception:
            pass

    if env_key:
        info["source"] = "环境变量 PGR_LLM_API_KEY/DEEPSEEK_API_KEY"
        info["key_len"] = len(env_key.strip())
    elif file_key:
        info["source"] = "config.json"
        info["key_len"] = len(file_key)
    else:
        info["source"] = "无"
        if info["parse_error"]:
            info["hint"] = f"config.json 解析失败（{info['parse_error']}）"
        elif not info["exists"]:
            info["hint"] = ("没有 config.json" + (
                "；config.example.json 里倒是填了密钥，但程序只读 config.json"
                if example_key else ""))
        else:
            info["hint"] = "config.json 里 api_key 是空的"
    info["ok"] = bool(info["key_len"])
    key = (env_key or file_key or "").strip()
    info["format_warning"] = key_shape_warning(key)
    if info["format_warning"]:
        info["hint"] = info["format_warning"]
    return info


def _external_block(external: list[dict] | None) -> str:
    if not external:
        return ""
    lines = ["\n【外部资料】（来自站外检索，不是剧情原文，只能用于确认别名/设定，不可当作台词引用）"]
    for i, r in enumerate(external, 1):
        lines.append(f"{i}. {r['title']}  —— {r['source']}  {r['url']}")
        if r.get("snippet"):
            lines.append(f"   摘要：{r['snippet'][:200]}")
    return "\n".join(lines) + "\n"


def build_prompt(question: str, evidence, history: list[dict] | None = None,
                 external: list[dict] | None = None) -> str:
    parts = []
    for e in evidence.excerpts:
        parts.append(f"【{e['category']}／{e['chapter']}／{e['title']}】序号 {e['seq']}\n{e['text']}")
    body = "\n\n".join(parts)

    hist = ""
    for turn in (history or [])[-3:]:
        q = (turn.get("question") or "").strip()
        a = (turn.get("answer") or "").strip()
        if not q:
            continue
        hist += f"\n问：{q}\n答：{a[:300]}\n"

    header = "【证据】\n"
    if hist:
        header = "【对话上下文】" + hist + "\n【证据】\n"
    ext = _external_block(external)
    question_block = (f"{ext}\n\n【问题】\n{question}\n\n按规则回答：有直接记载就答并标出处；"
                      f"没有就直接记载但有线索，就明确标「推断」并列出依据。")
    room = MAX_PROMPT_CHARS - len(SYSTEM) - len(header) - len(question_block) - 200
    if room < 500:
        room = 500
    if len(body) > room:
        body = body[:room] + "\n…（证据过长，已截断）"
    return header + body + question_block


def answer(question: str, evidence, history: list[dict] | None = None,
           external: list[dict] | None = None) -> dict:
    cfg = load_config()
    prompt = build_prompt(question, evidence, history, external)
    if not cfg["api_key"]:
        return {
            "mode": "evidence",
            "answer": "",
            "prompt_chars": len(prompt),
            "note": "未配置 API Key，仅返回检索证据。把密钥写入 config.json 的 api_key 即可生成回答。",
        }

    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {}) or {}
        usage["_model"] = data.get("model") or cfg["model"]
        return {"mode": "llm", "answer": text, "prompt_chars": len(prompt),
                "usage": usage, "note": ""}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        hint = key_shape_warning(cfg["api_key"])
        if e.code == 401 and hint:
            detail = f"{hint}\n原始返回：{detail}"
        return {"mode": "error", "answer": "", "prompt_chars": len(prompt),
                "note": f"模型接口返回 HTTP {e.code}：{detail}"}
    except Exception as e:
        return {"mode": "error", "answer": "", "prompt_chars": len(prompt),
                "note": f"调用模型失败：{e}"}
