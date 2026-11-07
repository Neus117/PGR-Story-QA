"""问答历史：每条问答落盘保存，重启不丢。

独立于 pgr.db —— 那个库在重建索引时会被整个删除，历史不能跟着没。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime

from .paths import DATA as _DATA

DB = str(_DATA / "history.db")
CITE_RE = re.compile(r"【([^】]{3,60})】")


class History:
    def __init__(self, db: str = DB):
        os.makedirs(os.path.dirname(db), exist_ok=True)
        self.db = db
        self.con = sqlite3.connect(db, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS turns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT,
                mode TEXT,
                steps INTEGER,
                tools TEXT,
                citations TEXT,
                tokens INTEGER,
                cost_cny REAL,
                secs REAL,
                note TEXT,
                model TEXT
            )""")
        # 老库补列：同一浏览器标签页 = 同一个会话，用来区分"本次会话"和"更早的历史"
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(turns)")}
        if "session" not in cols:
            self.con.execute("ALTER TABLE turns ADD COLUMN session TEXT")
        # 留痕：这一条是哪个模型答的（配置名可能与实际服务名不同）
        if "model" not in cols:
            self.con.execute("ALTER TABLE turns ADD COLUMN model TEXT")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON turns(ts)")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_session ON turns(session)")
        self.con.commit()

    # ---------- 写 ----------
    def record(self, payload: dict, secs: float = 0.0, session: str = "") -> int:
        answer = payload.get("answer") or ""
        cites = payload.get("citations") or []
        if not cites and answer:
            # agent 模式下出处写在正文里，抽出来单独存一份，方便以后检索
            seen, out = set(), []
            for m in CITE_RE.findall(answer):
                if m not in seen:
                    seen.add(m)
                    out.append({"label": m})
            cites = out[:30]
        cost = payload.get("cost") or {}
        tokens = (cost.get("usage") or {}).get("total") or 0
        cur = self.con.execute(
            """INSERT INTO turns(ts, question, answer, mode, steps, tools, citations,
                                 tokens, cost_cny, secs, note, session, model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                payload.get("question") or "",
                answer,
                payload.get("mode") or "",
                int(payload.get("steps") or 0),
                json.dumps([t.get("tool") for t in (payload.get("tool_calls") or [])],
                           ensure_ascii=False),
                json.dumps(cites, ensure_ascii=False)[:20000],
                tokens,
                float(cost.get("cost") or 0.0),
                round(secs, 1),
                (payload.get("note") or "")[:400],
                session or "",
                payload.get("model") or "",
            ))
        self.con.commit()
        return cur.lastrowid

    # ---------- 读 ----------
    def recent(self, limit: int = 20, offset: int = 0, session: str = "") -> list[dict]:
        if session:
            rows = self.con.execute(
                "SELECT * FROM turns WHERE session=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (session, limit, offset)).fetchall()
        else:
            rows = self.con.execute(
                "SELECT * FROM turns ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "ts": r["ts"], "question": r["question"],
                "answer": r["answer"], "mode": r["mode"], "steps": r["steps"],
                "tools": json.loads(r["tools"] or "[]"),
                "citations": json.loads(r["citations"] or "[]"),
                "tokens": r["tokens"], "cost_cny": r["cost_cny"],
                "secs": r["secs"], "note": r["note"],
                "session": r["session"] or "",
            })
        return out

    def stats(self) -> dict:
        row = self.con.execute(
            "SELECT count(*) n, coalesce(sum(tokens),0) t, coalesce(sum(cost_cny),0) c, "
            "       sum(CASE WHEN tokens=0 THEN 1 ELSE 0 END) no_tok "
            "FROM turns").fetchone()
        return {"turns": row["n"], "tokens": row["t"], "cost_cny": round(row["c"], 6),
                "turns_without_tokens": row["no_tok"] or 0}

    def export_markdown(self, path: str | None = None) -> str:
        rows = self.con.execute("SELECT * FROM turns ORDER BY id ASC").fetchall()
        lines = ["# 战双剧情问答 · 历史记录", "",
                 f"共 {len(rows)} 条，导出时间 {datetime.now().astimezone():%Y-%m-%d %H:%M}", ""]
        for r in rows:
            lines.append(f"## {r['ts']}　{r['question']}")
            lines.append("")
            lines.append(r["answer"] or "（无回答）")
            tools = json.loads(r["tools"] or "[]")
            meta = [f"模式 {r['mode']}", f"{r['steps']} 步"]
            if tools:
                meta.append("工具：" + " → ".join(tools))
            meta.append(f"{r['tokens']} tok")
            meta.append(f"¥{r['cost_cny']:.4f}")
            lines.append("")
            lines.append("> " + "　|　".join(meta))
            lines.append("")
        text = "\n".join(lines)
        if path:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        return text


if __name__ == "__main__":
    h = History()
    print("stats:", h.stats())
