"""Retrieval over pgr.db: question -> evidence excerpts with citations.

Lexical-only by design (no embedding model, no server, no dependency).

Pipeline
  1. strip question scaffolding, detect ordering intent (first/last),
     "summarise this chapter" intent, entities and chapter hints.
     `history` is folded into the probe text so follow-ups like
     "你根据已知信息推断呢" still have something to search.
  2. recall: FTS5 trigram over 'who：body' for query 3-grams AND entity names.
     Names that are not plain CJK (e.g. "21号") are handled by LIKE.
  3. multi-entity questions additionally force the stages where the entities
     co-occur into the candidate pool — those are the relationship evidence,
     and they are invisible to a 3-gram query.
  4. re-score: IDF-weighted coverage + speaker/entity/chapter bonuses.
  5. pick excerpts with a per-category cap so one long chapter cannot crowd
     out 间章 / 好感度 side stories.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field

from .paths import DATA as _DATA

DB = str(_DATA / "pgr.db")
WIKI = "https://wiki.biligame.com/zspms/"

CJK = r"\u3400-\u4dbf\u4e00-\u9fff"
GRAM_N = 3
MAX_GRAMS = 26
MAX_SHORT = 3
CANDIDATES = 900
# ~1.3万字 ≈ 9k token: stays far below the model ceiling on purpose
CTX_BUDGET = 13000
STAGE_WINDOW = 2800
PER_CATEGORY = 2
CO_STAGES = 4          # how many co-occurrence stages get forced in
SIDE_CATEGORIES = ("间章剧情", "好感度剧情", "本我回廊", "时宇漫纪")

INTENT_FIRST = re.compile(r"第一次|首次|最初|最早|初次|一开始|开头|初次登场|首先")
INTENT_LAST = re.compile(r"最后|最终|结局|末尾|临终|结尾")
SUMMARY_INTENT = re.compile(r"讲了什么|剧情是什么|是什么内容|概括|简述|概述|简介|大纲|梳理|讲了|发生了什么|怎么回事|主要内容|内容是什么|大致|的故事|的剧情")
SCAFFOLD = re.compile(
    r"请问|帮我|告诉我|一下|是什么|是什么人|怎么样|怎么|为什么|哪里|哪个|谁|"
    r"哪一章|第几章|第几关|的剧情|剧情中|剧情里|出现|提到|说过|讲过|说过吗|吗|呢|？|\?")
CHAPTER_RE = re.compile(r"第?\s*([0-9]{1,2}|[零一二三四五六七八九十]{1,3})\s*章")


@dataclass
class Hit:
    stage_id: int
    title: str
    chapter: str
    category: str
    seq: str
    stage_no: str
    score: float = 0.0
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Evidence:
    question: str
    entities: list[str]
    chapter_hint: str
    intent: str
    excerpts: list[dict]
    hit_count: int
    note: str = ""

    @property
    def chars(self) -> int:
        return sum(len(e["text"]) for e in self.excerpts)


def grams_of(text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(rf"[{CJK}]+", text):
        run = m.group(0)
        for i in range(len(run) - GRAM_N + 1):
            g = run[i : i + GRAM_N]
            if g not in out:
                out.append(g)
    for m in re.finditer(r"[A-Za-z0-9]{2,}", text):
        w = m.group(0).lower()
        if w not in out:
            out.append(w)
    return out


def short_cjk(text: str) -> list[str]:
    """CJK runs of exactly 2 chars — invisible to the trigram index."""
    out: list[str] = []
    for m in re.finditer(rf"[{CJK}]+", text):
        run = m.group(0)
        if len(run) == 2 and run not in out:
            out.append(run)
    return out


class Searcher:
    def __init__(self, db: str = DB):
        self.con = sqlite3.connect(db, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.n_lines = self.con.execute("SELECT count(*) FROM lines").fetchone()[0]
        # speaker strings include artefacts like "里？" or "？？？"; require a
        # clean name so entity detection cannot latch onto punctuation
        clean = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9·\-]{2,8}$")
        self.entities = [r["name"] for r in self.con.execute(
            "SELECT name FROM speakers WHERE n >= 15 ORDER BY n DESC")
            if clean.match(r["name"]) and r["name"] != "？？？"]
        self.chapters = [r["chapter"] for r in self.con.execute(
            "SELECT DISTINCT chapter FROM stages WHERE chapter <> ''")]
        self._df: dict[str, int] = {}

    # ---------- statistics ----------
    def df(self, gram: str) -> int:
        if gram in self._df:
            return self._df[gram]
        try:
            n = self.con.execute(
                "SELECT count(*) FROM fts WHERE fts MATCH ?",
                ('"%s"' % gram.replace('"', '""'),)).fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        self._df[gram] = n
        return n

    def idf(self, gram: str) -> float:
        n = self.df(gram)
        return 0.0 if n == 0 else math.log((self.n_lines + 1) / (n + 1)) + 0.15

    # ---------- query analysis ----------
    def analyse(self, question: str):
        intent = "last" if INTENT_LAST.search(question) else (
            "first" if INTENT_FIRST.search(question) else "")
        content = SCAFFOLD.sub(" ", question)
        content = INTENT_FIRST.sub(" ", content)
        content = INTENT_LAST.sub(" ", content)

        ents = [e for e in self.entities if len(e) >= 2 and e in question]
        ents.sort(key=len, reverse=True)
        kept: list[str] = []
        for e in ents:
            if not any(e != k and e in k for k in kept):
                kept.append(e)

        chapter = ""
        m = CHAPTER_RE.search(question)
        if m:
            chapter = m.group(1)
        if not chapter:
            for c in sorted(self.chapters, key=len, reverse=True):
                if not c:
                    continue
                bare = re.sub(r"^[0-9A-Za-z]+\s+", "", c)
                if c in question or (len(bare) >= 3 and bare in question):
                    chapter = c
                    break
        return kept, content, chapter, intent

    def _chapter_patterns(self, hint: str) -> list[str]:
        """A bare number must match "21 刻命螺旋", not "21号·XXI/好感剧情"."""
        if hint.isdigit():
            return [f"{int(hint):02d} %", f"% {int(hint):02d} %"]
        return [f"%{hint}%"]

    def _chapter_ordered(self, hint: str, limit: int) -> list[int]:
        if not hint:
            return []
        out: list[int] = []
        for p in self._chapter_patterns(hint):
            out = [r["id"] for r in self.con.execute(
                "SELECT id FROM stages WHERE chapter LIKE ? ORDER BY seq LIMIT ?", (p, limit))]
            if out:
                break
        return out[:limit]

    def _chapter_ids(self, hint: str) -> set[int]:
        if not hint:
            return set()
        ids: set[int] = set()
        for p in self._chapter_patterns(hint):
            got = {r["id"] for r in self.con.execute(
                "SELECT id FROM stages WHERE chapter LIKE ?", (p,))}
            ids |= got
            if got and hint.isdigit():
                break
        return ids

    def _hit_for(self, sid: int, score: float = 0.6) -> Hit:
        m = self.con.execute(
            "SELECT title, chapter, category, seq, stage_no FROM stages WHERE id=?", (sid,)).fetchone()
        return Hit(sid, m["title"], m["chapter"], m["category"], m["seq"], m["stage_no"], score)

    # ---------- rare-name discovery ----------
    def rare_names(self, question: str, cap: int = 2) -> list[str]:
        """Two-character terms that exist in the corpus but are rare.

        Looked up in the prebuilt bigram table, so this costs one indexed query
        instead of a full table scan. Rare usually means "proper noun"
        (小梅: 10 lines); common means "function word or ubiquitous speaker"
        (指挥官: 38k lines) and is useless as a focus term.
        """
        cands: list[str] = []
        for m in re.finditer(rf"[{CJK}]{{2,}}", question):
            run = m.group(0)
            for i in range(len(run) - 1):
                b = run[i : i + 2]
                if b not in cands:
                    cands.append(b)
        if not cands:
            return []
        rows = self.con.execute(
            "SELECT b, n FROM bigrams WHERE b IN (%s)" % ",".join("?" * len(cands)),
            cands).fetchall()
        limit = max(50, int(self.n_lines * 0.02))
        speakers = {r["name"] for r in self.con.execute("SELECT name FROM speakers")}
        pairs = []
        for r in rows:
            b, n = r["b"], r["n"]
            if not (0 < n <= limit):
                continue
            # 跨词切出来的假名字（"官抱"取自"指挥官抱有"）必须排掉：
            # 只接受说话人名、小X/阿X/老X 这类称呼，或单独成词的已知称呼
            if b in speakers or b[0] in "小阿老":
                pairs.append((b, n))
        pairs.sort(key=lambda x: x[1])
        return [b for b, _ in pairs[:cap]]

    def _rare_stage_rows(self, terms: list[str]) -> list:
        """Lines containing a rare term — forced into the candidate pool."""
        rows = []
        for t in terms:
            rows.extend(self.con.execute(
                """SELECT l.id, l.stage_id, l.who, l.full AS body FROM lines l
                   WHERE l.full LIKE ? LIMIT 300""", (f"%{t}%",)).fetchall())
        return rows

    def _entity_boost(self, name: str) -> float:
        """Ubiquitous speakers must not dominate ranking."""
        n = self.con.execute("SELECT n FROM speakers WHERE name=?", (name,)).fetchone()
        freq = (n["n"] / self.n_lines) if n else 0.2
        if freq >= 0.05:
            return 0.05
        if freq >= 0.02:
            return 0.15
        return 0.45

    # ---------- co-occurrence ----------
    def _co_occurrence(self, entities: list[str]) -> set[int]:
        """Stages listing ALL entities. Relationship questions live here and are
        unreachable through a 3-gram query when a name is short or mixed-script."""
        sets = []
        for name in entities[:3]:
            ids = {r["stage_id"] for r in self.con.execute(
                "SELECT DISTINCT stage_id FROM lines WHERE full LIKE ?", (f"%{name}%",))}
            if not ids:
                return set()
            sets.append(ids)
        out = sets[0]
        for x in sets[1:]:
            out &= x
        return out

    def _co_hits(self, co_ids: set[int], intent: str) -> list[Hit]:
        """Pick co-occurrence stages: earliest main-line ones plus the
        character side stories, which usually hold the actual origin."""
        if not co_ids:
            return []
        rows = self.con.execute(
            "SELECT id, title, chapter, category, seq, stage_no, txt FROM stages WHERE id IN (%s)"
            % ",".join("?" * len(co_ids)), tuple(co_ids)).fetchall()
        main = [r for r in rows if r["category"] not in SIDE_CATEGORIES]
        side = [r for r in rows if r["category"] in SIDE_CATEGORIES]
        key = (lambda r: r["seq"] or "")
        main.sort(key=key, reverse=(intent == "last"))
        side.sort(key=key, reverse=(intent == "last"))
        picked: list = []
        picked.extend(main[: CO_STAGES // 2])
        picked.extend(side[: CO_STAGES - CO_STAGES // 2])
        out = []
        for r in picked:
            h = Hit(r["id"], r["title"], r["chapter"], r["category"], r["seq"], r["stage_no"], 1.5)
            out.append(h)
        return out

    # ---------- main ----------
    def search(self, question: str, history: str = "", top_stages: int = 4,
               budget: int = CTX_BUDGET) -> Evidence:
        # follow-ups carry no content words of their own; fold in the previous turn
        probe = (history.strip() + " " + question).strip() if history.strip() else question
        entities, content, chapter_hint, intent = self.analyse(probe)
        grams = [g for g in grams_of(content) if self.df(g) > 0][:MAX_GRAMS]
        # entity names must be reachable even when the name is not plain CJK ("21号")
        ent_terms = [e for e in entities if len(e) >= 3 and self.df(e) > 0]
        shorts = [t for t in short_cjk(content) if t not in entities][:MAX_SHORT]

        if not grams and not ent_terms and not shorts and not (entities and intent):
            return Evidence(question, entities, chapter_hint, intent, [], 0,
                            "问题里没有可检索的内容词（可能只有虚词或不足两字）")

        # names that are not in the speaker table still matter (小梅 is an alias)
        # 排除与已识别实体互为子串的碎片（"阿尔"是"阿尔法"的前缀）
        rare = [t for t in self.rare_names(question)
                if not any(t in f or f in t for f in entities)]
        focus = entities + rare
        ent_set = set(entities)
        ch_ids = self._chapter_ids(chapter_hint)
        boosts = {e: self._entity_boost(e) for e in focus}
        # 只对稀有的两个以上名字做共同出场；"指挥官"这种 9% 的名字帮不上忙
        co_focus = [e for e in focus if boosts.get(e, 0) > 0.15]
        co_ids = self._co_occurrence(co_focus) if len(co_focus) >= 2 else set()
        scored: list[tuple[float, sqlite3.Row]] = []

        match_terms = grams + ent_terms
        if match_terms:
            weights = {g: (self.idf(g) * (1.6 if g in ent_terms else 1.0)) for g in match_terms}
            w_total = sum(weights.values()) or 1.0
            match_expr = " OR ".join('"%s"' % g.replace('"', '""') for g in match_terms)
            rows = self.con.execute(
                """SELECT l.id, l.stage_id, l.who, l.full AS body
                   FROM fts JOIN lines l ON l.id = fts.rowid
                   WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?""",
                (match_expr, CANDIDATES)).fetchall()
            for r in rows:
                body = r["body"] or ""
                got = sum(w for g, w in weights.items() if g in body)
                if got <= 0:
                    continue
                s = got / w_total
                if r["who"] in ent_set:
                    s += boosts.get(r["who"], 0.05)
                else:
                    for e in focus:
                        if e in body:
                            s += boosts.get(e, 0.05) * 0.55
                            break
                if r["stage_id"] in ch_ids:
                    s += 0.25
                if r["stage_id"] in co_ids:
                    s += 0.40
                scored.append((s, r))

        # 两字人名对 trigram 不可见（"诺安"），必须无条件做一次实体扫描并入候选，
        # 否则 FTS 里那些由"给我介绍"这类虚词切出来的噪声会把正确答案挤掉
        scan_terms: list[str] = []
        for t in [e for e in entities if len(e) == 2] + shorts:
            if t and t not in scan_terms:
                scan_terms.append(t)
        if scan_terms:
            scored.extend(self._short_term_scan(scan_terms, ent_set, intent))
        if not scored:
            return Evidence(question, entities, chapter_hint, intent, [], 0,
                            "没有匹配到任何台词")

        if not scored and not co_ids:
            return Evidence(question, entities, chapter_hint, intent, [], 0,
                            "没有匹配到任何台词")

        # force rare-name lines in: they are the whole point of the question
        for r in self._rare_stage_rows(rare):
            scored.append((1.2, r))

        scored.sort(key=lambda x: -x[0])
        by_stage: dict[int, Hit] = {}
        for s, r in scored:
            sid = r["stage_id"]
            h = by_stage.get(sid)
            if h is None:
                h = self._hit_for(sid, 0.0)
                by_stage[sid] = h
            h.score += s * (1.0 if len(h.lines) < 3 else 0.2)
            if len(h.lines) < 4:
                h.lines.append((r["who"], r["body"]))

        ranked = sorted(by_stage.values(), key=lambda h: -h.score)

        if intent in ("first", "last") and ranked and len(entities) < 2:
            cut = ranked[0].score * 0.45
            strong = [h for h in ranked if h.score >= cut]
            weak = [h for h in ranked if h.score < cut]
            strong.sort(key=lambda h: (h.seq or "9" * 9), reverse=(intent == "last"))
            ranked = strong + weak

        ranked = self._apply_chapter(question, chapter_hint, ranked, ch_ids)

        # relationship questions: co-occurrence stages are forced to the front
        co_hits = self._co_hits(co_ids, intent)
        if co_hits:
            seen = {h.stage_id for h in co_hits}
            ranked = co_hits + [h for h in ranked if h.stage_id not in seen]

        excerpts, used = [], 0
        per_cat: dict[str, int] = {}
        for h in ranked:
            if len(excerpts) >= top_stages:
                break
            if per_cat.get(h.category, 0) >= PER_CATEGORY:
                continue
            txt = self._stage_excerpt(h)
            if excerpts and used + len(txt) > budget:
                continue
            per_cat[h.category] = per_cat.get(h.category, 0) + 1
            excerpts.append({
                "stage_id": h.stage_id,
                "title": h.title,
                "chapter": h.chapter,
                "category": h.category,
                "seq": h.seq,
                "score": round(h.score, 3),
                "url": WIKI + urllib.parse.quote(h.title.replace(" ", "_")),
                "text": txt[:budget],
            })
            used += len(txt)
        return Evidence(question, entities + [t for t in rare if t not in entities],
                        chapter_hint, intent, excerpts, len(scored),
                        "" if excerpts else "没有找到相关关卡")

    # ---------- fallbacks ----------
    def _short_term_scan(self, terms, ent_set, intent):
        """Two-char words: no trigram, so scan. Ordered by stage seq when the
        question asks for the first/last occurrence, otherwise by rowid."""
        terms = [t for t in terms if t]
        if not terms:
            return []
        order = "ORDER BY s.seq ASC" if intent == "first" else (
            "ORDER BY s.seq DESC" if intent == "last" else "ORDER BY s.seq ASC")
        # 先要"这个人自己说话"的行，再要"别人提到他"的行；否则光秃秃一个名字
        # 的问题会被大量旁白提及占满
        sql = (f"SELECT l.id, l.stage_id, l.who, l.full AS body FROM lines l "
               f"JOIN stages s ON s.id = l.stage_id WHERE l.full LIKE ? "
               f"ORDER BY (l.who = ?) DESC, {order.split('ORDER BY ')[1]} LIMIT ?")
        speaker = next(iter(ent_set), "")
        rows = self.con.execute(sql, (f"%{terms[0]}%", speaker, CANDIDATES)).fetchall()
        out = []
        for r in rows:
            body = r["body"] or ""
            n = sum(1 for t in terms if t in body)
            if n == 0:
                continue
            s = n / len(terms)
            if r["who"] in ent_set:
                s += 0.45
            out.append((s, r))
        return out

    CATEGORY_RANK = {"主线剧情": 0, "隐藏剧情": 1, "外篇剧情": 2,
                     "间章剧情": 3, "好感度剧情": 4}

    # 角色选关的权重（都归一化到 0~1 再加权，方便以后调）
    W_SPOKEN, W_SHARE, W_HOT, W_CHOICE, W_BATTLE = 3.0, 2.0, 1.0, 0.5, 0.3

    # 类别分成三档并各给预算比例：人物弧光 > 主线经历 > 活动点缀
    CORE_CATS = {"间章剧情", "本我回廊", "好感度剧情"}
    MAIN_CATS = {"主线剧情", "隐藏剧情", "外篇剧情"}
    CORE_QUOTA, MAIN_QUOTA, SIDE_QUOTA = 0.50, 0.35, 0.15
    SIDE_MAX_STAGES = 3

    def is_character(self, name: str, min_lines: int = 100) -> bool:
        """够不够格按"人物"处理。帕弥什这种在说话人表里是 0 行的，不能当角色讲故事。"""
        row = self.con.execute("SELECT n FROM speakers WHERE name=?", (name,)).fetchone()
        return bool(row and row["n"] >= min_lines)

    def character_stages(self, name: str, budget_chars: int = 120000) -> list[dict]:
        """某角色"讲了什么"所需的关卡集合。

        选关依据是"她在这关有没有戏"（见 score()），但**光有得分不够**：
        角色生平和弧光大多在间章/本我回廊/好感剧情里，而主线章节多、活动章节更多，
        按章节平均发名额会把专属线挤掉。所以按类别分预算：

            角色专属线（间章/本我回廊/好感度）  40%   —— 人物弧光，允许同章多取
            主线线（主线/隐藏/外篇）           45%   —— 经历，每章取最高分
            其他（活动/联动/H5）               15%   —— 点缀，最多 3 关
        """
        if not name:
            return []
        rows = self.con.execute(
            """
            SELECT s.id, s.seq, s.title, s.chapter, s.category, s.stage_no, s.txt,
                   sum(CASE WHEN l.who = :n THEN 1 ELSE 0 END) AS spoken,
                   sum(CASE WHEN l.full LIKE :like THEN 1 ELSE 0 END) AS mentioned,
                   sum(CASE WHEN l.who = :n AND (l.full LIKE '%！%' OR l.full LIKE '%？%')
                            THEN 1 ELSE 0 END) AS hot,
                   sum(CASE WHEN l.full LIKE '[选项]%' THEN 1 ELSE 0 END) AS choices,
                   sum(CASE WHEN l.full LIKE '[战斗提示]%' THEN 1 ELSE 0 END) AS battles,
                   count(*) AS total
            FROM lines l JOIN stages s ON s.id = l.stage_id
            GROUP BY s.id
            """, {"n": name, "like": f"%{name}%"}).fetchall()
        cand = [r for r in rows if r["mentioned"] > 0]
        if not cand:
            return []

        def score(r) -> float:
            spoken = r["spoken"] or 0
            total = max(1, r["total"] or 1)
            return (self.W_SPOKEN * min(spoken, 40) / 40
                    + self.W_SHARE * (spoken / total)
                    + self.W_HOT * min(r["hot"] or 0, 12) / 12
                    + self.W_CHOICE * (1.0 if (r["choices"] or 0) > 0 else 0.0)
                    + self.W_BATTLE * (0.0 if (r["battles"] or 0) > 0 else 1.0))

        core, main, side = [], [], []
        for r in cand:
            if score(r) < 0.5:          # 只是被捎带提一句
                continue
            cat = r["category"]
            if cat in self.CORE_CATS:
                core.append(r)
            elif cat in self.MAIN_CATS:
                main.append(r)
            else:
                side.append(r)

        def take(groups, quota_chars, per_chapter=1, hard_max=None, min_score=0.0):
            """按章节取高分（per_chapter=None 表示整章全取），直到本档预算用完。"""
            groups = [r for r in groups if score(r) >= min_score]
            by_ch: dict[tuple, list] = {}
            for r in groups:
                by_ch.setdefault((r["category"], r["chapter"]), []).append(r)
            chosen = []
            for key, items in by_ch.items():
                items.sort(key=lambda r: (-score(r), r["seq"] or ""))
                chosen.extend(items if per_chapter is None else items[:per_chapter])
            chosen.sort(key=lambda r: (-score(r), r["seq"] or ""))
            out, used = [], 0
            for r in chosen:
                txt = r["txt"] or ""
                if out and used + len(txt) > quota_chars:
                    continue
                out.append(r)
                used += len(txt)
                if hard_max and len(out) >= hard_max:
                    break
            return out

        # 专属线（间章/本我回廊/好感度）是角色自己的连续故事，整章全取；
        # 主线每章取最高分；活动类只是点缀，门槛提高且最多 3 关
        picked = (take(core, budget_chars * self.CORE_QUOTA,
                       per_chapter=None, min_score=0.8)
                  + take(main, budget_chars * self.MAIN_QUOTA,
                         per_chapter=1, min_score=0.8)
                  + take(side, budget_chars * self.SIDE_QUOTA,
                         per_chapter=1, hard_max=self.SIDE_MAX_STAGES, min_score=1.5))

        picked.sort(key=lambda r: (self.CATEGORY_RANK.get(r["category"], 9), r["seq"] or ""))
        out = []
        for r in picked:
            out.append({"seq": r["seq"], "title": r["title"], "chapter": r["chapter"],
                        "category": r["category"], "stage_no": r["stage_no"],
                        "txt": r["txt"] or "", "score": round(score(r), 2),
                        "spoken": r["spoken"],
                        "share": round((r["spoken"] or 0) / max(1, r["total"]), 2)})
        return out

    def chapter_stages(self, hint: str) -> list[dict]:
        """整章全部关卡，按序号排序。用于"简述本章"这类需要全章的提问。"""
        out = []
        for pat in self._chapter_patterns(hint):
            rows = self.con.execute(
                "SELECT seq, title, chapter, category, stage_no, txt FROM stages "
                "WHERE chapter LIKE ? ORDER BY seq", (pat,)).fetchall()
            if rows:
                out = [dict(r) for r in rows]
                break
        return out

    def _apply_chapter(self, question, hint, ranked, ch_ids):
        if not hint:
            return ranked
        if SUMMARY_INTENT.search(question) or not ranked:
            pack = [self._hit_for(sid) for sid in self._chapter_ordered(hint, 4)]
            return pack or ranked
        inside = [h for h in ranked if h.stage_id in ch_ids]
        outside = [h for h in ranked if h.stage_id not in ch_ids]
        if inside:
            return inside + outside
        pack = [self._hit_for(sid, 0.5) for sid in self._chapter_ordered(hint, 1)]
        return pack + ranked

    def _stage_excerpt(self, h: Hit) -> str:
        txt = self.con.execute("SELECT txt FROM stages WHERE id=?", (h.stage_id,)).fetchone()[0] or ""
        head = f"【{h.category}／{h.chapter}／{h.stage_no or h.title}】序号 {h.seq}\n"
        if len(txt) <= STAGE_WINDOW:
            return head + txt
        anchor = 0
        for _, body in h.lines:
            i = txt.find(body[:20])
            if i >= 0:
                anchor = i
                break
        start = max(0, anchor - STAGE_WINDOW // 2)
        end = min(len(txt), start + STAGE_WINDOW)
        start = max(0, end - STAGE_WINDOW)
        return (head + ("…（前略）\n" if start else "") + txt[start:end]
                + ("\n…（后略）" if end < len(txt) else ""))


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "露西亚第一次出场"
    s = Searcher()
    ev = s.search(q)
    print(f"Q: {ev.question}")
    print(f"entities={ev.entities} chapter={ev.chapter_hint!r} intent={ev.intent!r} "
          f"hits={ev.hit_count} chars={ev.chars} note={ev.note}")
    for e in ev.excerpts:
        print(f"  {e['seq']} {e['title']}  [{e['category']}/{e['chapter']}] score={e['score']}")
