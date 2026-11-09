"""检索层封装成模型可调用的工具。

设计原则：
  · 返回值必须**紧凑**——模型要看得懂，但一次不能塞爆上下文；
  · 失败要**可恢复**——返回 {"error": ...} 而不是抛异常，模型可以换个参数重试；
  · 每个结果都带出处，模型才有东西可标注。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from . import websearch
from .paths import DATA as _DATA

DB = str(_DATA / "pgr.db")
WIKI = "https://wiki.biligame.com/zspms/"

READ_BUDGET = 12000      # 单次 read_chapter 返回上限（字）
LIST_BUDGET = 6000       # 单次 list_chapters 返回上限（字）

_cjk = r"\u3400-\u4dbf\u4e00-\u9fff"
CITE_RE = re.compile(r"【([^】]{2,80})】")
_NUM_PREFIX = re.compile(r"^[0-9A-Za-z]+[\s\-]*")


def bare(text: str) -> str:
    """去掉"27 碑火铸脊"里的编号，只留名字，用来容忍编号写错。"""
    return _NUM_PREFIX.sub("", (text or "").strip()).strip()


class Toolbox:
    def __init__(self, db: str = DB):
        self.con = sqlite3.connect(db, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.log: list[dict] = []

    # ---------------- 工具实现 ----------------
    def list_chapters(self, category: str = "", keyword: str = "") -> dict:
        rows = self.con.execute(
            """SELECT category, chapter, count(*) AS n, sum(nchars) AS chars
               FROM stages WHERE chapter <> '' GROUP BY category, chapter
               ORDER BY category, min(seq)""").fetchall()
        if category:
            rows = [r for r in rows if category in (r["category"] or "")]
        if keyword:
            rows = [r for r in rows if keyword in (r["chapter"] or "")]
        lines, used, total = [], 0, len(rows)
        for r in rows:
            line = f'{r["category"]}｜{r["chapter"]}｜{r["n"]}关｜{r["chars"]:,}字'
            if used + len(line) > LIST_BUDGET:
                lines.append(f"…（共 {total} 章，已达单次上限，可用 category 或 keyword 缩小范围）")
                break
            lines.append(line)
            used += len(line)
        return {"total_chapters": total, "chapters": lines}

    def find_character(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"error": "name 不能为空"}
        exact = self.con.execute("SELECT name, n FROM speakers WHERE name=?", (name,)).fetchone()
        similar = self.con.execute(
            "SELECT name, n FROM speakers WHERE name LIKE ? ORDER BY n DESC LIMIT 8",
            (f"%{name}%",)).fetchall()
        if not exact and not similar:
            return {"found": False,
                    "note": f"说话人表里没有「{name}」。可能是设定名词、也可能语料里没出现；"
                            f"可以改用 search_lines 直接搜这个词。"}
        target = exact or similar[0]
        n = target["n"]
        rows = self.con.execute("""
            SELECT s.seq, s.title, s.chapter, s.category,
                   sum(CASE WHEN l.who = :n THEN 1 ELSE 0 END) AS spoken
            FROM lines l JOIN stages s ON s.id = l.stage_id
            WHERE l.full LIKE :like GROUP BY s.id ORDER BY spoken DESC LIMIT 40
        """, {"n": target["name"], "like": f"%{target['name']}%"}).fetchall()
        chapters: dict[tuple, int] = {}
        for r in rows:
            k = (r["category"], r["chapter"])
            chapters[k] = chapters.get(k, 0) + (r["spoken"] or 0)
        top = sorted(chapters.items(), key=lambda kv: -kv[1])[:15]
        return {
            "found": True,
            "name": target["name"],
            "spoken_lines": n,
            "is_character": n >= 100,
            "similar_names": [{"name": s["name"], "n": s["n"]} for s in similar][:5],
            "stages": len(rows),
            "top_chapters": [{"category": c, "chapter": ch, "spoken": v} for (c, ch), v in top],
            "hint": "用 read_chapter 读取这些章节的原文；章节名原样传入即可。",
        }

    def read_chapter(self, chapter: str, offset: int = 0, focus: str = "") -> dict:
        chapter = (chapter or "").strip()
        if not chapter:
            return {"error": "chapter 不能为空"}
        rows = self.con.execute(
            "SELECT DISTINCT chapter, category FROM stages WHERE chapter LIKE ?",
            (f"%{chapter}%",)).fetchall()
        if not rows:
            return {"error": f"没有匹配到章节「{chapter}」", "suggestion": "先调 list_chapters 看有哪些章节"}
        if len(rows) > 1:
            names = [f'{r["category"]}｜{r["chapter"]}' for r in rows][:8]
            exact = [r for r in rows if r["chapter"] == chapter]
            if not exact:
                return {"error": f"「{chapter}」匹配到多个章节，请用更完整的名字",
                        "candidates": names}
            rows = exact
        cat, chap = rows[0]["category"], rows[0]["chapter"]
        stages = self.con.execute(
            "SELECT seq, title, stage_no, txt FROM stages WHERE chapter=? ORDER BY seq",
            (chap,)).fetchall()
        full, marks = [], []
        pos = 0
        for st in stages:
            piece = f"\n《{st['stage_no'] or st['title']}》\n{st['txt'] or ''}"
            marks.append((pos, st["title"], st["seq"]))
            full.append(piece)
            pos += len(piece)
        text = "".join(full)

        # 带 focus 时只回命中段落（前后各留一段）：整章上下文是最大的时间开销
        focus = (focus or "").strip()
        if focus:
            paras = [x for x in text.split("\n") if x.strip()]
            keys = [focus] + [focus[i:i + 2] for i in range(max(0, len(focus) - 1))]
            hit = [i for i, para in enumerate(paras) if any(k and k in para for k in keys)]
            if hit:
                picked, last = [], -2
                for i in hit[:12]:
                    for j in range(max(0, i - 1), min(len(paras), i + 2)):
                        if j != last:
                            picked.append(paras[j])
                            last = j
                body = "\n".join(picked)
                return {
                    "chapter": chap, "category": cat, "stages": len(stages),
                    "total_chars": len(text), "focus": focus,
                    "matched": len(hit), "returned_chars": len(body),
                    "text": body[:READ_BUDGET],
                    "note": "只返回命中段落的上下文；需要更多可换 focus 或不带 focus 读整章",
                }

        off = max(0, int(offset or 0))
        chunk = text[off: off + READ_BUDGET]
        return {
            "chapter": chap, "category": cat, "stages": len(stages),
            "total_chars": len(text), "offset": off,
            "has_more": off + READ_BUDGET < len(text),
            "next_offset": off + READ_BUDGET if off + READ_BUDGET < len(text) else None,
            "text": chunk,
        }

    def search_lines(self, query: str, speaker: str = "", limit: int = 10) -> dict:
        query = (query or "").strip()
        if not query:
            return {"error": "query 不能为空"}
        limit = max(1, min(int(limit or 10), 20))
        grams = [query[i: i + 3] for i in range(max(1, len(query) - 2))]
        rows = []
        if grams:
            expr = " OR ".join('"%s"' % g.replace('"', '""') for g in grams)
            try:
                rows = self.con.execute("""
                    SELECT l.who, l.full, s.title, s.chapter, s.category, s.seq
                    FROM fts JOIN lines l ON l.id = fts.rowid
                    JOIN stages s ON s.id = l.stage_id
                    WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT 200
                """, (expr,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:      # 两字词走 LIKE
            rows = self.con.execute("""
                SELECT l.who, l.full, s.title, s.chapter, s.category, s.seq
                FROM lines l JOIN stages s ON s.id = l.stage_id
                WHERE l.full LIKE ? LIMIT 300
            """, (f"%{query}%",)).fetchall()
        if speaker:
            rows = [r for r in rows if r["who"] == speaker] or rows
        out = []
        for r in rows[:limit]:
            out.append({
                "cite": f'【{r["category"]}／{r["chapter"]}／{r["title"]}】',
                "who": r["who"], "text": (r["full"] or "")[:220],
            })
        return {"hits": len(out), "results": out}

    def web_search(self, query: str, limit: int = 4) -> dict:
        rows = websearch.search(query or "", limit=max(1, min(limit, 6)))
        return {"count": len(rows),
                "note": "站外资料，只能用于确认别名/设定，不可当作剧情原文引用",
                "results": [{"title": r["title"], "url": r["url"],
                             "source": r["source"], "snippet": r["snippet"][:160]} for r in rows]}

    # ---------------- 引用校验 ----------------
    def resolve_cite(self, body: str) -> str | None:
        """把模型写的【分类／章节／关卡】对回数据库里的规范写法。

        容错点：
          · 章节编号写错（"27 摇篮游行"实际是 26）——按去掉编号后的名字匹配；
          · 关卡名带不带编号都试一次；
        对不上就返回 None，交给上层标记为未核实（多半是模型编的）。
        """
        parts = [p.strip() for p in re.split(r"[／/]", body) if p.strip()]
        if len(parts) >= 2:
            chap_raw = parts[-2] if len(parts) >= 3 else parts[-1]
            stage_raw = parts[-1] if len(parts) >= 3 else None
            chap_bare = bare(chap_raw)
            # 找章节
            rows = self.con.execute("SELECT DISTINCT category, chapter FROM stages").fetchall()
            cands = [r for r in rows if bare(r["chapter"]) == chap_bare] or \
                    [r for r in rows if chap_bare and chap_bare in r["chapter"]]
            if not cands:
                return None
            cat, chap = cands[0]["category"], cands[0]["chapter"]
            if stage_raw is None:
                return f"{cat}／{chap}"
            # 找关卡：优先本章内匹配
            in_ch = self.con.execute(
                "SELECT title, category, chapter FROM stages WHERE chapter=? AND (title=? OR title LIKE ?)",
                (chap, stage_raw, f"%{bare(stage_raw)}%")).fetchall()
            if in_ch:
                r = in_ch[0]
                return f"{r['category']}／{r['chapter']}／{r['title']}"
            # 模型常把 "ER12-2滚雪" 写成 "ER12-2 滚雪"，忽略空格再试一次
            other = self.con.execute(
                "SELECT title, category, chapter, seq FROM stages "
                "WHERE title=? OR title LIKE ? OR REPLACE(title,' ','')=?",
                (stage_raw, f"%{bare(stage_raw)}%", (stage_raw or "").replace(" ", ""))).fetchall()
            if len(other) == 1:
                r = other[0]
                return f"{r['category']}／{r['chapter']}／{r['title']}"
            return None

    def verify_and_fix(self, answer: str) -> tuple[str, list[dict]]:
        """把答案里的每条【…】对回数据库：能对上的改成规范写法，对不上的标注未核实。"""
        fixes: list[dict] = []
        if not answer:
            return answer, fixes

        def repl(m):
            body = m.group(1)
            resolved = self.resolve_cite(body)
            if resolved and resolved != body:
                fixes.append({"from": body, "to": resolved, "action": "corrected"})
                return f"【{resolved}】"
            if resolved:
                return m.group(0)
            fixes.append({"from": body, "to": "", "action": "unverified"})
            return f"【未核实：{body}】"

        out = CITE_RE.sub(repl, answer)
        return out, fixes

    # ---------------- 调度 ----------------
    def dispatch(self, name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return json.dumps({"error": f"参数不是合法 JSON：{raw_args[:100]}"}, ensure_ascii=False)
        fn = getattr(self, name, None)
        if fn is None or name not in SCHEMAS_BY_NAME:
            return json.dumps({"error": f"没有这个工具：{name}"}, ensure_ascii=False)
        try:
            result = fn(**args)
        except TypeError as e:
            return json.dumps({"error": f"参数不对：{e}", "expected": SCHEMAS_BY_NAME[name]["parameters"]},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
        self.log.append({"tool": name, "args": args})
        return json.dumps(result, ensure_ascii=False)


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "list_chapters",
            "description": "列出剧情章节目录（分类｜章节名｜关卡数｜字数）。用于不确定章节叫什么、或想通览全部章节时。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "只列某个分类，如 主线剧情/间章剧情/好感度剧情/本我回廊/活动剧情"},
                    "keyword": {"type": "string", "description": "章节名关键词过滤"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_character",
            "description": "查一个角色在剧情库中是否存在：台词行数、出场章节分布、是否有相近名字。回答任何关于某人的问题前先调它。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "角色名，如 诺安、薇拉、21号"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chapter",
            "description": "读取某一章的剧情原文。带 focus 时只返回命中段落及其上下文（快、省）；不带则分段返回整章（单次约 1.2 万字，慢）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chapter": {"type": "string", "description": "章节名，可用简称，如 烛灯刑、烬海异途、15 绝海星火"},
                    "focus": {"type": "string", "description": "想在这一章里找的关键词或人名。知道自己在找什么时务必带上"},
                    "offset": {"type": "integer", "description": "续读偏移，用上次返回的 next_offset"},
                },
                "required": ["chapter"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_lines",
            "description": "在全部台词里检索关键词，返回命中的句子与出处。用于定位某句台词、某件事发生在哪一关。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词或短语"},
                    "speaker": {"type": "string", "description": "只看某角色说的，可选"},
                    "limit": {"type": "integer", "description": "条数，默认 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "站外检索（战双 BWIKI、哔哩哔哩、玩家社区）。仅在本地查不到别名或设定时使用；结果不能当作剧情原文。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
        },
    },
]

SCHEMAS_BY_NAME = {t["function"]["name"]: t["function"] for t in TOOL_SPECS}
