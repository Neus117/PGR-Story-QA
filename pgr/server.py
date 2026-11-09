"""Local web UI for the story QA bot.

  python server.py            -> http://127.0.0.1:8712
  python server.py 9000       -> custom port

Endpoints
  GET  /            chat page
  POST /api/ask     {"question": "..."} -> {answer, mode, citations, excerpts}
  GET  /api/health  readiness + corpus size + whether an LLM key is configured
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import agent, config_api, llm, pricing, update as update_mod, websearch
from .logfile import get as _log
from .history import History as _History
from .search import Searcher, SUMMARY_INTENT

STORY_INTENT = __import__('re').compile(r'讲讲|讲一下|说说|故事|历程|经历|生平|人物线|传记|一生')
BIO_INTENT = __import__('re').compile(r'介绍一下|介绍|是谁|什么来头|认识一下|人物介绍|简介')
from .paths import ROOT, WEB

WEB = str(WEB)


def _os_mtime(path: str) -> float:
    import os as _o
    return _o.path.getmtime(path)
DEFAULT_PORT = 8712

_lock = threading.Lock()
UPDATE_LOCK = threading.Lock()
UPDATE_STATE = {"state": "idle", "message": "", "progress": "", "result": None}

# 页面存活检测：浏览器用 SSE 维持一条连接，全部断开后自动退出
SSE_LOCK = threading.Lock()
SSE_CLIENTS = 0
STOPPING = threading.Event()
_CITE_BOX = None
_CITE_INDEX = None          # (by_title, by_bare_chapter) 一次性索引
_CITE_LOCK = threading.Lock()


def _cite_box():
    global _CITE_BOX
    if _CITE_BOX is None:
        from .tools import Toolbox
        _CITE_BOX = Toolbox()
    return _CITE_BOX


def _cite_index():
    """把 3692 个关卡读进内存做索引。

    原来每条出处都跑一次 `SELECT DISTINCT category, chapter FROM stages`，
    34 条历史 × 若干出处 = 上百次全表扫描，单个请求要 6 秒，浏览器会直接断开。
    """
    global _CITE_INDEX
    if _CITE_INDEX is None:
        with _CITE_LOCK:
            if _CITE_INDEX is None:
                import re as _re
                box = _cite_box()
                rows = box.con.execute(
                    "SELECT title, chapter, category, seq FROM stages").fetchall()
                by_title = {}
                by_norm = {}          # 去掉所有空格后的标题，用来容忍多打/少打空格
                by_chapter = {}
                for r in rows:
                    by_title.setdefault(r["title"], r)
                    by_norm.setdefault((r["title"] or "").replace(" ", ""), r)
                    bare = _re.sub(r"^[0-9A-Za-z]+[\s\-]*", "", r["chapter"] or "").strip()
                    by_chapter.setdefault(bare, r)
                _CITE_INDEX = (by_title, by_chapter, by_norm)
    return _CITE_INDEX


def _enrich_citations(cites: list) -> list:
    """{"label": "分类／章节／关卡"} -> 带 seq/url 的可点击结构。全程走内存索引。"""
    if not cites:
        return []
    import re as _re
    by_title, by_chapter, by_norm = _cite_index()
    out = []
    for c in cites:
        if not isinstance(c, dict):
            out.append({"label": str(c)})
            continue
        if c.get("seq") and c.get("url"):
            out.append(c)
            continue
        label = c.get("label") or ""
        try:
            parts = [x.strip() for x in _re.split(r"[／/]", label) if x.strip()]
            row = None
            if len(parts) >= 3:
                want = parts[-1]
                row = by_title.get(want) or by_norm.get(want.replace(" ", ""))
            if row is None and len(parts) >= 2:
                chap_raw = parts[-2] if len(parts) >= 3 else parts[-1]
                bare = _re.sub(r"^[0-9A-Za-z]+[\s\-]*", "", chap_raw).strip()
                row = by_chapter.get(bare)
        except Exception:
            row = None
        if row is None:
            out.append({"label": label, "unverified": True})
            continue
        out.append({
            "label": f'{row["category"]}／{row["chapter"]}／{row["title"]}',
            "category": row["category"], "chapter": row["chapter"],
            "title": row["title"], "seq": row["seq"],
            "url": "https://wiki.biligame.com/zspms/"
                   + __import__("urllib.parse", fromlist=["quote"]).quote(row["title"].replace(" ", "_")),
        })
    return out


_searcher = None
LAST_ERROR = ""
_MODEL_FILE = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "data" / "last_model.json")


def _load_served() -> str:
    try:
        with open(_MODEL_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("model", "")
    except Exception:
        return ""


def _save_served(name: str) -> None:
    try:
        os.makedirs(os.path.dirname(_MODEL_FILE), exist_ok=True)
        with open(_MODEL_FILE, "w", encoding="utf-8") as f:
            json.dump({"model": name}, f)
    except Exception:
        pass


SERVED_MODEL = _load_served()
# 进程内累计（重启清零），用于界面上显示"本次运行花了多少"
TOTALS = {"requests": 0, "tokens": 0, "cost": 0.0}
HIST = _History()


DB_LOCK = threading.RLock()

# 启动就绪状态：0~100，前端据此画进度条
READY = threading.Event()
BOOT = {"pct": 0, "text": "启动服务"}


def _boot(pct: int, text: str) -> None:
    BOOT["pct"] = pct
    BOOT["text"] = text


def _one_conn():
    """每次请求一条独立连接：sqlite 连接不是线程安全的，共享会串结果。"""
    import sqlite3
    from .paths import DATA
    con = sqlite3.connect(str(DATA / "pgr.db"))
    con.row_factory = sqlite3.Row
    return con


def _close_db_connections() -> None:
    """重建索引前必须放掉所有句柄，否则 Windows 上 os.replace 会失败。"""
    global _searcher, _CITE_BOX, _CITE_INDEX
    for obj in (_searcher, _CITE_BOX):
        try:
            if obj is not None:
                obj.con.close()
        except Exception:
            pass
    _searcher = None
    _CITE_BOX = None
    _CITE_INDEX = None


def get_searcher() -> Searcher:
    global _searcher
    if _searcher is None:
        _searcher = Searcher()
    return _searcher


def _account(usage: dict, model: str) -> dict:
    """记一笔账：本请求的 token/费用 + 进程内累计。"""
    global SERVED_MODEL
    if usage.get("_model") and usage["_model"] != SERVED_MODEL:
        SERVED_MODEL = usage["_model"]
        _save_served(SERVED_MODEL)
    u = pricing.normalize(usage)
    # 计价必须按「实际服务的模型」算：配置里可能写的是别名或被路由的名字
    r = pricing.cost_cny(usage, usage.get("_model") or model)
    TOTALS["requests"] += 1
    TOTALS["tokens"] += u["total"]
    TOTALS["cost"] += r["cost"]
    return {"cost": round(r["cost"], 6), "cn": pricing.fmt_cny(r["cost"]),
            "band": r["band"], "peak": r["peak"], "tier": r["tier"], "usage": u,
            "detail": {k: round(v, 6) for k, v in r["detail"].items()},
            "total_cn": pricing.fmt_cny(TOTALS["cost"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "PGRStory/1.0"

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}", flush=True)

    def _serve_events(self):
        """SSE 心跳：一条连接代表一个打开的页面。"""
        global SSE_CLIENTS
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with SSE_LOCK:
            SSE_CLIENTS += 1
            n = SSE_CLIENTS
        _log().info("页面连接建立，当前页面数=%d", n)
        try:
            while not STOPPING.is_set():
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(2)   # 心跳越密，越早知道页面关了
        except Exception:
            pass
        finally:
            with SSE_LOCK:
                SSE_CLIENTS -= 1
                n = SSE_CLIENTS
            _log().info("页面连接断开，当前页面数=%d", n)

    def _start_update(self):
        """后台跑更新，前端轮询 /api/update/status。"""
        if UPDATE_STATE["state"] == "running":
            return self._json({"ok": False, "error": "running",
                               "message": "已经在更新中，请等这次结束"}, 409)
        # 触发间隔：哪怕只是"检查"也会打两次请求，连续点会触发站点限流
        st = update_mod.load_state()
        wait = update_mod.ATTEMPT_COOLDOWN - (time.time() - float(st.get("last_attempt") or 0))
        if wait > 0:
            return self._json({"ok": False, "error": "cooldown",
                               "message": f"刚检查过，请等 {int(wait)} 秒再试（频繁触发会触发站点限流）"}, 429)
        st["last_attempt"] = time.time()
        update_mod.save_state(st)
        if not UPDATE_LOCK.acquire(blocking=False):
            return self._json({"ok": False, "error": "busy", "message": "更新任务正忙"}, 409)
        UPDATE_STATE.update({"state": "running", "message": "", "progress": "准备中", "result": None})

        def work():
            try:
                def progress(text: str):
                    UPDATE_STATE["progress"] = text

                plan = update_mod.check_updates(force=True)
                if not plan.get("ok"):
                    UPDATE_STATE.update({"state": "error",
                                         "message": plan.get("message", "检查失败"),
                                         "progress": ""})
                    return
                if not plan.get("has_update"):
                    UPDATE_STATE.update({
                        "state": "done", "progress": "",
                        "message": "wiki 源暂无新内容",
                        "result": {"no_change": True, "checked_at": plan.get("checked_at"),
                                   "pages": plan.get("remote_pages")}})
                    return
                UPDATE_STATE["progress"] = (
                    f"发现更新：新增 {len(plan['new'])} / 改动 {len(plan['changed'])} / 删除 {len(plan['deleted'])}")
                _close_db_connections()          # 先放掉句柄
                res = update_mod.apply_updates(plan, progress=progress)
                UPDATE_STATE.update({
                    "state": "done" if res.get("ok") else "error",
                    "message": res.get("message", ""),
                    "progress": "",
                    "result": res})
            except Exception as e:
                traceback.print_exc()
                UPDATE_STATE.update({"state": "error",
                                     "message": f"{type(e).__name__}: {e}", "progress": ""})
            finally:
                try:
                    UPDATE_LOCK.release()
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()
        return self._json({"ok": True, "started": True})

    def _finish(self, payload: dict, secs: float, code: int = 200, session: str = ""):
        """所有问答都从这里出去，保证一定落盘。"""
        try:
            # 本条回答实际用的模型：优先取本次回包里的 model，其次全局最近值
            payload["model"] = ((payload.get("usage") or {}).get("_model")
                                 or SERVED_MODEL or llm.load_config()["model"])
            # agent 模式不返回 citations（出处写在正文里），这里补一次提取，
            # 否则当次回答的出处全是死标签，要刷新后才变可点
            if not payload.get("citations"):
                answer = payload.get("answer") or ""
                if answer:
                    from .history import CITE_RE
                    seen = []
                    for m in CITE_RE.findall(answer):
                        if m.startswith('未核实：'):
                            continue
                        if m not in seen:
                            seen.append(m)
                    payload["citations"] = [{"label": m} for m in seen[:30]]
            payload["citations"] = _enrich_citations(payload.get("citations") or [])
            with DB_LOCK:
                payload["history_id"] = HIST.record(payload, secs, session)
        except Exception as e:          # 落盘失败不能影响回答
            print(f"  [history] 写入失败: {e}", flush=True)
        return self._json(payload, code)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 接口响应同样不能缓存：否则浏览器会拿旧的空历史反复渲染
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            return self._do_get()
        except Exception as e:
            # 任何未捕获异常都必须回一个响应：直接断连接会让浏览器只看到 "Failed to fetch"
            traceback.print_exc()
            _log().error("未处理异常 %s: %s", type(e).__name__, e)
            try:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def _do_get(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._file(os.path.join(WEB, "index.html"), "text/html; charset=utf-8")
        if path == "/api/stages":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q") or [""])[0].strip()
            chap = (qs.get("chapter") or [""])[0].strip()
            try:
                con = _one_conn()
                try:
                    if q:
                        like = f"%{q}%"
                        rows = con.execute(
                            "SELECT seq,title,chapter,category FROM stages "
                            "WHERE title LIKE ? OR chapter LIKE ? OR seq LIKE ? "
                            "ORDER BY seq LIMIT 60", (like, like, like)).fetchall()
                        return self._json({"ok": True, "mode": "search",
                                           "items": [dict(r) for r in rows]})
                    if chap:
                        rows = con.execute(
                            "SELECT seq,title,category FROM stages WHERE chapter=? "
                            "ORDER BY seq LIMIT 300", (chap,)).fetchall()
                        return self._json({"ok": True, "mode": "chapter", "chapter": chap,
                                           "items": [dict(r) for r in rows]})
                    rows = con.execute(
                        "SELECT chapter, COUNT(*) n, MIN(seq) s FROM stages "
                        "WHERE chapter<>'' GROUP BY chapter "
                        "ORDER BY CASE WHEN MIN(seq)='' THEN 1 ELSE 0 END, MIN(seq)").fetchall()
                    return self._json({"ok": True, "mode": "chapters",
                                       "chapters": [{"chapter": r["chapter"], "n": r["n"]}
                                                    for r in rows]})
                finally:
                    con.close()
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/stage":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            seq = (qs.get("seq") or [""])[0].strip()
            if not seq:
                return self._json({"ok": False, "error": "缺少 seq"}, 400)
            try:
                con = _one_conn()
                try:
                    row = con.execute(
                        "SELECT seq, title, chapter, category, stage_no, nchars, txt "
                        "FROM stages WHERE seq=?", (seq,)).fetchone()
                finally:
                    con.close()
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
            if not row:
                return self._json({"ok": False, "error": f"没有序号为 {seq} 的关卡"}, 404)
            return self._json({"ok": True, "seq": row["seq"], "title": row["title"],
                               "chapter": row["chapter"], "category": row["category"],
                               "stage_no": row["stage_no"], "chars": row["nchars"],
                               "text": row["txt"] or ""})
        if path == "/api/events":
            return self._serve_events()
        if path == "/api/config":
            d = config_api.current()
            # 配置里写的是请求名，实际服务的是回包里的 model（旧别名会被路由到别的型号）
            d["served_model"] = SERVED_MODEL
            return self._json(d)
        if path == "/api/config/test":
            return self._json(config_api.test())
        if path == "/api/update/status":
            return self._json({"state": UPDATE_STATE["state"],
                               "message": UPDATE_STATE["message"],
                               "progress": UPDATE_STATE["progress"],
                               "result": UPDATE_STATE["result"],
                               "wiki": "https://wiki.biligame.com/zspms/%E5%89%A7%E6%83%85%E5%9B%9E%E9%A1%BE"})
        if path == "/api/balance":
            cfg = llm.load_config()
            if not cfg["api_key"]:
                return self._json({"ok": False, "error": "未配置 API Key"})
            import json as _json
            import urllib.request as _ur
            req = _ur.Request(cfg["base"].rstrip("/") + "/user/balance")
            req.add_header("Authorization", "Bearer " + cfg["api_key"])
            try:
                with _ur.urlopen(req, timeout=20) as r:
                    data = _json.loads(r.read().decode("utf-8"))
                return self._json({"ok": True, "balance": data})
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        if path == "/api/history":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            def _int(name: str, default: int, lo: int, hi: int) -> int:
                """参数可能是 abс / 负数 / 超长，一律收敛到合法区间，不许抛异常。"""
                raw = (qs.get(name) or [""])[0]
                try:
                    return max(lo, min(hi, int(raw)))
                except (TypeError, ValueError):
                    return default

            limit = _int("limit", 20, 1, 200)
            offset = _int("offset", 0, 0, 10 ** 9)
            sess = (qs.get("session") or [""])[0][:64]
            with DB_LOCK:
                turns = HIST.recent(limit, offset, sess)
            for t in turns:
                t["citations"] = _enrich_citations(t.get("citations") or [])
            with DB_LOCK:
                stats = HIST.stats()
            return self._json({"stats": stats, "turns": turns})
        if path == "/api/history/export":
            with DB_LOCK:
                text = HIST.export_markdown()
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="pgr-history.md"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/health":
            try:
                s = get_searcher()
                with DB_LOCK:
                    n = s.con.execute("SELECT count(*) FROM stages").fetchone()[0]
                    lines = s.con.execute("SELECT count(*) FROM lines").fetchone()[0]
                diag = llm.diagnose()
                try:
                    import datetime as _dt
                    _idx = os.path.join(WEB, "index.html")
                    _build = _dt.datetime.fromtimestamp(_os_mtime(_idx)).strftime("%m-%d %H:%M:%S")
                except Exception:
                    _build = ""
                from . import __version__ as _ver
                from .paths import ROOT as _ROOT
                return self._json({"ok": True, "version": _ver, "app_dir": str(_ROOT),
                                   "ready": READY.is_set(),
                                   "pct": BOOT["pct"], "stage": BOOT["text"],
                                   "stages": n, "lines": lines,
                                   "llm": diag["ok"],
                                   "model": SERVED_MODEL or (llm.load_config()["model"] if diag["ok"] else ""),
                                   "requested_model": llm.load_config()["model"] if diag["ok"] else "",
                                   "mode": "llm" if diag["ok"] else "evidence",
                                   "diag": diag,
                                   "build": locals().get("_build", ""),
                                   "last_error": LAST_ERROR,
                                   "totals": {"requests": TOTALS["requests"],
                                              "tokens": TOTALS["tokens"],
                                              "cost": pricing.fmt_cny(TOTALS["cost"])}})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    def _file(self, path, ctype):
        if not os.path.exists(path):
            return self._json({"error": "missing " + os.path.basename(path)}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 页面改完必须立刻生效：不加这些头，浏览器会拿旧 HTML，
        # 表现就是"改了半天没变化"，误判成程序坏了。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path == "/api/config/key":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception as e:
                return self._json({"ok": False, "message": f"请求体解析失败：{e}"}, 400)
            res = config_api.save(data.get("api_key", ""), data.get("model", ""), data.get("base", ""))
            return self._json(res, 200 if res.get("ok") else 400)
        if urlparse(self.path).path == "/api/update/apply":
            return self._start_update()
        if urlparse(self.path).path != "/api/ask":
            return self._json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not READY.is_set():
                return self._json({"error": "正在加载语料索引，请稍候",
                                   "starting": True,
                                   "pct": BOOT["pct"],
                                   "stage": BOOT["text"]}, 503)
            if UPDATE_STATE["state"] == "running":
                return self._json({"error": "正在更新语料，请等这次完成再提问"}, 503)
            question = (data.get("question") or "").strip()
            if not question:
                return self._json({"error": "question 为空"}, 400)
            # 追问句自身没有内容词，拿上一轮的问题去补检索词
            _sid = (data.get("session") or "")[:64]
            history = data.get("history") or []
            if not isinstance(history, list):
                history = []
            hist_text = " ".join((t.get("question") or "") for t in history[-2:])

            # ---- 默认：agent 工具循环（无意图正则、无路由阈值）----
            if not data.get("legacy"):
                t0 = time.time()
                out = agent.run(question, history)
                t_agent = (time.time() - t0) * 1000
                cost = _account(out.get("usage", {}), llm.load_config()["model"])
                cost["cn"] = pricing.fmt_cny(cost["cost"])
                cost["total_cn"] = pricing.fmt_cny(TOTALS["cost"])
                return self._finish({
                    "question": question,
                    "answer": out.get("answer", ""),
                    "mode": out.get("mode"),
                    "note": out.get("note", ""),
                    "steps": out.get("steps", 0),
                    "tool_calls": out.get("tool_calls", []),
                    "search_ms": round(t_agent),
                    "entities": [], "chapter_hint": "", "intent": "agent",
                    "chars": 0, "prompt_chars": 0, "batches": 0, "stage_count": 0,
                    "citations": [], "excerpts": [], "external": [],
                    "cost": cost,
                }, (time.time() - t0), session=_sid)

            t0 = time.time()
            with _lock:
                searcher = get_searcher()
                ev = searcher.search(question, history=hist_text)
            t_search = (time.time() - t0) * 1000

            # 0) 「讲讲某角色的故事」：跨章节聚合，每章挑出场最密的一关
            # 「介绍一下X」只有在问题是"单纯问这个人是谁"时才走人物简介：
            # 把名字和口语词剥掉后如果还剩实体内容（如"第一次登场"），说明是个具体问题
            _residual = question
            for _e in ev.entities[:1]:
                _residual = _residual.replace(_e, '')
            _residual = __import__('re').sub(
                r'请|给我|帮我|介绍一下|介绍|讲讲|说说|一下|这个人|这个角色|是谁|什么来头|'
                r'人物介绍|简介|认识|吧|啊|呢|吗|的|？|\?|\s', '', _residual)
            bio_mode = (bool(ev.entities) and not ev.chapter_hint
                        and bool(BIO_INTENT.search(question))
                        and not bool(STORY_INTENT.search(question))
                        and len(_residual) <= 3)
            story_mode = (bool(ev.entities) and not ev.chapter_hint
                          and bool(STORY_INTENT.search(question)))
            if (story_mode or bio_mode) and searcher.is_character(ev.entities[0]):
                name = ev.entities[0]
                stages = searcher.character_stages(name)
                if len(stages) >= 3:
                    gen = llm.summarize_character(name, stages,
                                                  style="bio" if bio_mode else "story")
                    payload = {
                        "question": question,
                        "answer": gen.get("answer", ""),
                        "mode": gen.get("mode"),
                        "note": gen.get("note", ""),
                        "usage": gen.get("usage", {}),
                        "search_ms": round(t_search),
                        "entities": ev.entities,
                        "chapter_hint": "",
                        "intent": "story",
                        "subject": name,
                        "chars": gen.get("chars", 0),
                        "prompt_chars": 0,
                        "batches": gen.get("batches", 0),
                        "stage_count": gen.get("stage_count", len(stages)),
                        "citations": [{"title": st["title"], "chapter": st["chapter"],
                                       "category": st["category"], "seq": st["seq"],
                                       "url": "https://wiki.biligame.com/zspms/"
                                              + __import__("urllib.parse", fromlist=["quote"]).quote(st["title"].replace(" ", "_")),
                                       "score": 0} for st in stages],
                        "excerpts": [],
                        "external": [],
                        "cost": _account(gen.get("usage", {}), llm.load_config()["model"]),
                    }
                    return self._finish(payload, time.time() - t0, session=_sid)

            # 1) 「简述某一章」需要整章，不是四关 —— 走分段归纳再合并
            chapter_mode = bool(ev.chapter_hint) and bool(SUMMARY_INTENT.search(question))
            if chapter_mode:
                stages = searcher.chapter_stages(ev.chapter_hint)
                if len(stages) >= 3:
                    gen = llm.summarize_chapter(question, stages, history)
                    payload = {
                        "question": question,
                        "answer": gen.get("answer", ""),
                        "mode": gen.get("mode"),
                        "note": gen.get("note", ""),
                        "usage": gen.get("usage", {}),
                        "search_ms": round(t_search),
                        "entities": ev.entities,
                        "chapter_hint": ev.chapter_hint,
                        "intent": ev.intent,
                        "chars": gen.get("chars", 0),
                        "prompt_chars": 0,
                        "batches": gen.get("batches", 0),
                        "stage_count": gen.get("stage_count", len(stages)),
                        "citations": [{"title": st["title"], "chapter": st["chapter"],
                                       "category": st["category"], "seq": st["seq"],
                                       "url": "https://wiki.biligame.com/zspms/"
                                              + __import__("urllib.parse", fromlist=["quote"]).quote(st["title"].replace(" ", "_")),
                                       "score": 0} for st in stages],
                        "excerpts": [],
                        "external": [],
                        "cost": _account(gen.get("usage", {}), llm.load_config()["model"]),
                    }
                    return self._finish(payload, time.time() - t0, session=_sid)

            # 2) 本地证据薄弱时才联网，避免每次都打网络
            external = []
            want_web = bool(data.get("web", True))
            weak = (len(ev.excerpts) < 2) or (max((e["score"] for e in ev.excerpts), default=0) < 1.5)
            if want_web and llm.available() and (weak or ev.entities):
                # 用最有区分度的名字去搜；没有实体就退回整句
                term = " ".join(ev.entities[:2]).strip() or question
                try:
                    external = websearch.search(term, limit=3)
                except Exception as e:
                    print(f"  [websearch failed] {e}", flush=True)

            t_legacy = time.time()
            gen = llm.answer(question, ev, history, external)
            global LAST_ERROR
            LAST_ERROR = gen.get("note", "") if gen.get("mode") == "error" else ""
            return self._finish({
                "question": question,
                "answer": gen.get("answer", ""),
                "mode": gen.get("mode"),
                "note": gen.get("note", ""),
                "prompt_chars": gen.get("prompt_chars", 0),
                "usage": gen.get("usage", {}),
                "search_ms": round(t_search),
                "entities": ev.entities,
                "chapter_hint": ev.chapter_hint,
                "intent": ev.intent,
                "chars": ev.chars,
                "citations": [
                    {"title": e["title"], "chapter": e["chapter"], "category": e["category"],
                     "seq": e["seq"], "url": e["url"], "score": e["score"]}
                    for e in ev.excerpts
                ],
                "excerpts": [{"title": e["title"], "text": e["text"]} for e in ev.excerpts],
                "external": [{"title": r["title"], "url": r["url"],
                              "source": r["source"], "snippet": r["snippet"]}
                             for r in external],
                "cost": _account(gen.get("usage", {}), llm.load_config()["model"]),
            }, (time.time() - t_legacy), session=_sid)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


def main(port: int | None = None, open_browser: bool = True,
         keep_alive: bool = False) -> int:
    import webbrowser
    from .paths import DATA

    log = _log()
    port = int(port or DEFAULT_PORT)
    if not (DATA / "pgr.db").exists():
        msg = "缺少 data/pgr.db，请先运行:  python scripts/bootstrap.py"
        print(msg)
        log.error(msg)
        return 1

    url = f"http://127.0.0.1:{port}/"

    # 端口上已经有本工具在跑？直接打开它，别再起第二个（Windows 的 SO_REUSEADDR
    # 允许重复绑定，两个实例会抢同一个端口，请求随机落到其中一个）
    # 先用 socket 探端口（毫秒级）。以前直接发 HTTP 请求，端口没人监听时要等满
    # 2 秒超时才返回，这就是"双击后要等几秒才弹浏览器"的主因。
    import socket as _socket
    _probe = _socket.socket()
    _probe.settimeout(0.2)
    _occupied = (_probe.connect_ex(("127.0.0.1", port)) == 0)
    _probe.close()

    if _occupied:
        try:
            import urllib.request as _ur
            from .paths import ROOT as _ROOT
            with _ur.urlopen(url + "api/health", timeout=1.5) as _r:
                _j = json.loads(_r.read().decode("utf-8"))
            if _j.get("ok") and "stages" in _j:
                same = os.path.normcase(str(_j.get("app_dir", ""))) == os.path.normcase(str(_ROOT))
                if same:
                    print(f"已有实例在运行：{url}")
                    log.info("端口 %d 上已有同一个安装的实例，直接打开页面", port)
                    if open_browser:
                        webbrowser.open(url)
                    return 0
                # 是别的安装：绝不接管，否则用户会看到别人的密钥与历史
                log.warning("端口 %d 被另一个安装占用（%s），换端口启动", port, _j.get("app_dir"))
                print(f"端口 {port} 上是另一个安装的实例（{_j.get('app_dir')}），改用新端口")
                import socket as _s2
                for _alt in range(port + 1, port + 30):
                    _p = _s2.socket()
                    try:
                        if _p.connect_ex(("127.0.0.1", _alt)) != 0:
                            port = _alt
                            url = f"http://127.0.0.1:{port}/"
                            break
                    finally:
                        _p.close()
        except Exception:
            pass

    try:
        # 只做 bind+listen（毫秒级），重量级初始化放到后台
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        msg = f"端口 {port} 被占用且不是本工具（{e}）。换一个端口：python app.py --port 8712"
        print(msg)
        log.error(msg)
        return 1

    try:
        from .logfile import _elapsed_ms as _since_log
        _ms = int(_since_log())
    except Exception:
        _ms = -1
    log.info("开始监听 port=%d 自日志就绪 %d ms sock=%.3f", port, _ms, time.time())

    diag = llm.diagnose()
    print(f"战双剧情问答  ->  {url}")
    print(f"  config : {diag['config_path']}  (exists={diag['exists']})")
    if diag["ok"]:
        print(f"  llm    : key from {diag['source']}, len={diag['key_len']}, model={llm.load_config()['model']}")
        if diag.get("format_warning"):
            print(f"  !! 密钥格式可疑：{diag['format_warning']}")
    else:
        print(f"  llm    : 证据模式 —— {diag['hint']}")
    print(f"  exit   : {'常驻（--keep-alive）' if keep_alive else '页面全关后自动退出'}")
    log.info("服务启动 port=%d keep_alive=%s llm=%s", port, keep_alive, diag["ok"])

    # 页面全部关闭后退出：给刷新留 1 秒宽限
    def watchdog():
        had_page = False
        idle_since = None
        while not STOPPING.is_set():
            time.sleep(0.3)     # 轮询间隔要小于宽限，否则宽限会被量化成 1~2 秒
            with SSE_LOCK:
                n = SSE_CLIENTS
            if n > 0:
                had_page = True
                idle_since = None
            elif had_page:
                idle_since = idle_since or time.time()
                if time.time() - idle_since >= 1:
                    log.info("所有页面已关闭，服务退出")
                    STOPPING.set()
                    srv.shutdown()
                    return

    if not keep_alive:
        threading.Thread(target=watchdog, daemon=True).start()

    def init():
        try:
            _boot(20, "打开语料库")
            get_searcher()
            _boot(60, "加载检索索引")
            _cite_index()
            _boot(90, "准备就绪")
        except Exception as e:
            log.error("初始化失败：%s: %s", type(e).__name__, e)
            _boot(0, f"初始化失败：{e}")
            return
        _boot(100, "就绪")
        READY.set()
        log.info("初始化完成")

    # 浏览器先弹出来，页面自己显示进度条；不必等后端初始化完
    def _open_browser():
        t = time.perf_counter()
        try:
            webbrowser.open(url)
        except Exception as e:
            log.warning("打开浏览器失败：%s", e)
        log.info("已请求打开浏览器，耗时 %.0f ms", (time.perf_counter() - t) * 1000)

    if open_browser:
        threading.Timer(0.15, _open_browser).start()
    threading.Thread(target=init, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        STOPPING.set()
        log.info("服务停止")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", type=int, default=None)
    ap.add_argument("--keep-alive", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    raise SystemExit(main(port=a.port, open_browser=not a.no_browser, keep_alive=a.keep_alive))
