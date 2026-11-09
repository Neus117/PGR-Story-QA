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
            "affection": self.affection_index(target["name"]),
            "hint": "用 read_chapter 读取这些章节的原文；章节名原样传入即可。",
        }

    def affection_index(self, name: str) -> list:
        """某角色的好感度剧情索引。

        同一角色的好感剧情在 wiki 上可能散在两个章节名下
        （「比安卡·深痕」与「比安卡·深痕/好感剧情」），这里按角色名合并。
        """
        name = (name or "").strip()
        if not name:
            return []
        like = f"%{name}%"
        rows = self.con.execute(
            "SELECT chapter, seq, title FROM stages "
            "WHERE category='好感度剧情' AND (chapter LIKE ? OR title LIKE ?) "
            "ORDER BY seq", (like, like)).fetchall()
        merged: dict[str, dict] = {}
        for r in rows:
            base = (r["chapter"] or "").split("/")[0].strip()
            item = merged.get(base)
            if item is None:
                merged[base] = {"character": base, "chapters": [r["chapter"]],
                                "stages": 1, "seq": r["seq"], "first_title": r["title"]}
            else:
                item["stages"] += 1
                if r["chapter"] not in item["chapters"]:
                    item["chapters"].append(r["chapter"])
        out = sorted(merged.values(), key=lambda x: x["seq"])
        return out

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
    def _stage_rows(self):
        """全量关卡的一行缓存（3,692 行，构建一次即可）。"""
        rows = getattr(self, "_rows_cache", None)
        if rows is None:
            rows = self.con.execute(
                "SELECT seq, category, chapter, title FROM stages").fetchall()
            self._rows_cache = rows
        return self._rows_cache

    def resolve_cite(self, body: str) -> str | None:
        """把模型写的【分类／章节／关卡】对回数据库里的规范写法。

        用「包含匹配」而不是按分隔符切分：好感度剧情的章节名里本身就带斜杠
        （如 赛琳娜·希声/好感剧情），一旦按 ／ 切开就错位，整条引用会变成未核实。
        """
        b = (body or "").replace("／", "/").strip()
        if not b:
            return None
        rows = self._stage_rows()

        # 1) 章节名 + 关卡名都在标签里：取覆盖最长的那一行
        best, best_len = None, 0
        for r in rows:
            ch, ti = r["chapter"], r["title"]
            if ch and ti and ch.replace(" ", "") in b.replace(" ", "") \
                    and ti.replace(" ", "") in b.replace(" ", ""):
                if len(ch) + len(ti) > best_len:
                    best, best_len = r, len(ch) + len(ti)
        if best:
            return f"{best['category']}／{best['chapter']}／{best['title']}"

        # 2) 关卡名唯一命中（模型少写了章节）
        hits = [r for r in rows if r["title"] and r["title"].replace(" ", "") in b.replace(" ", "")]
        if len(hits) == 1:
            r = hits[0]
            return f"{r['category']}／{r['chapter']}／{r['title']}"

        # 3) 只写了章节：按章节最长的包含匹配
        flat = b.replace(" ", "")
        chap_hits = []
        for r in rows:
            if not r["seq"]:
                continue
            ch = (r["chapter"] or "").replace(" ", "")
            if not ch:
                continue
            if ch in flat:
                chap_hits.append((len(ch), r))
            else:
                base = ch.split("/")[0]
                if base and base in flat:
                    chap_hits.append((len(base), r))
        if chap_hits:
            chap_hits.sort(key=lambda x: -x[0])
            r = chap_hits[0][1]
            return f"{r['category']}／{r['chapter']}"
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
