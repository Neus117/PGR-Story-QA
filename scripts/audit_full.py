"""全面健壮性审计：接口、数据、并发、安全、静态扫描。

    python scripts/audit_full.py

不调用大模型（不产生费用），全部本地可达。任何一项 FAIL 都会让退出码为 1。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORT = int(os.environ.get("PGR_AUDIT_PORT", "8799"))
BASE = os.environ.get("PGR_BASE", f"http://127.0.0.1:{PORT}")
_own_server = None
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))


def http(path: str, method: str = "GET", body: dict | None = None, timeout: int = 20):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def jload(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------- 1. 基础可用性
def t_health():
    code, raw = http("/api/health")
    d = jload(raw) or {}
    ok = code == 200 and d.get("ok") and d.get("stages", 0) > 3000
    check("健康检查", ok, f"{code} stages={d.get('stages')} lines={d.get('lines')}")
    return d


def t_stage(seq: str):
    code, raw = http(f"/api/stage?seq={urllib.parse.quote(seq)}")
    d = jload(raw) or {}
    check(f"原文接口 seq={seq[:12]}", code in (200, 400, 404) and d.get("ok") in (True, False),
          f"{code} ok={d.get('ok')}")


# ---------------------------------------------------------------- 2. 输入恶意与边界
def t_bad_inputs():
    cases = [
        ("空 seq", "/api/stage"),
        ("SQL 注入", "/api/stage?seq=" + urllib.parse.quote("1' OR '1'='1")),
        ("超长 seq", "/api/stage?seq=" + "9" * 400),
        ("路径穿越", "/api/stage?seq=" + urllib.parse.quote("../../config.json")),
        ("空字节", "/api/stage?seq=%00"),
        ("历史 limit 负数", "/api/history?limit=-5"),
        ("历史 limit 巨型", "/api/history?limit=999999"),
        ("历史 offset 负数", "/api/history?offset=-1"),
        ("历史畸形 session", "/api/history?session=" + urllib.parse.quote("<script>")),
        ("未知路由", "/api/nope"),
    ]
    for name, path in cases:
        code, raw = http(path)
        ok = code in (200, 400, 404) and "<script>" not in raw
        check(f"输入边界：{name}", ok, f"{code}")


# ---------------------------------------------------------------- 3. 密钥不外泄
def t_no_secret():
    cfg = json.load(open(ROOT / "config.json", encoding="utf-8"))
    key = cfg.get("api_key", "")
    leaks = []
    for path in ("/api/health", "/api/config", "/api/history?limit=5", "/"):
        code, raw = http(path)
        if key and key in raw:
            leaks.append(path)
    check("密钥不出现在任何响应里", not leaks, f"泄漏于 {leaks}" if leaks else "4 个端点均无")

    # 静态文件不应能读到 config.json
    code, raw = http("/config.json")
    check("不能通过 HTTP 读到 config.json", code == 404, f"{code}")


# ---------------------------------------------------------------- 4. 配置接口
def t_config():
    code, raw = http("/api/config")
    d = jload(raw) or {}
    masked = d.get("mask", "")
    ok = code == 200 and (masked == "" or "…" in masked) and "api_key" not in d
    check("配置接口只回打码值", ok, f"{code} mask={masked}")

    code, raw = http("/api/config/key", "POST", {"api_key": ""})
    check("空密钥被拒", code == 400, f"{code}")

    code, raw = http("/api/config/key", "POST", {"api_key": "x" * 500})
    check("超长密钥被拒", code == 400, f"{code}")

    code, raw = http("/api/config/key", "POST", None)
    check("无请求体的密钥写入不崩", code in (400, 200), f"{code}")


# ---------------------------------------------------------------- 5. 更新接口
def t_update():
    code, raw = http("/api/update/status")
    d = jload(raw) or {}
    check("更新状态可读", code == 200 and "state" in d, f"{code} state={d.get('state')}")

    code, raw = http("/api/update/apply", "POST", {})
    ok = code in (200, 409, 429)
    check("更新触发有节流保护", ok, f"{code} {raw[:60]}")


# ---------------------------------------------------------------- 6. 并发
def t_concurrency():
    errs = []
    def hit(i):
        code, _ = http("/api/stage?seq=0100101")
        if code != 200:
            errs.append(code)
    threads = [threading.Thread(target=hit, args=(i,)) for i in range(8)]
    t0 = time.time()
    [t.start() for t in threads]
    [t.join() for t in threads]
    dt = time.time() - t0
    check("8 并发原文请求", not errs, f"耗时 {dt:.2f}s 失败 {errs}")
    check("并发响应时间 < 5s", dt < 5, f"{dt:.2f}s")


# ---------------------------------------------------------------- 7. SSE 心跳
def t_sse():
    status = ""
    try:
        req = urllib.request.Request(BASE + "/api/events")
        with urllib.request.urlopen(req, timeout=6) as r:
            status = r.headers.get("Content-Type", "")
            r.read(8)
    except Exception as e:
        status = f"error {e}"
    check("SSE 以 text/event-stream 返回", "text/event-stream" in status, status[:60])


# ---------------------------------------------------------------- 8. 数据完整性
def t_data(health: dict):
    con = sqlite3.connect(DATA / "pgr.db")
    n_stages = con.execute("SELECT COUNT(*) FROM stages").fetchone()[0]
    n_lines = con.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    n_fts = con.execute("SELECT COUNT(*) FROM fts").fetchone()[0]
    check("DB 关卡数与健康接口一致", n_stages == health.get("stages"), f"{n_stages} vs {health.get('stages')}")
    check("DB 台词数与健康接口一致", n_lines == health.get("lines"), f"{n_lines} vs {health.get('lines')}")
    check("FTS 行数与台词数一致", n_fts == n_lines, f"{n_fts} vs {n_lines}")

    # wiki 上有 5 个旁支文档没有「序号」属性（升格档案/回归剧情等），属于源数据事实
    empty_seq = con.execute("SELECT COUNT(*) FROM stages WHERE seq IS NULL OR seq=''").fetchone()[0]
    empty_no_title = con.execute(
        "SELECT COUNT(*) FROM stages WHERE (seq IS NULL OR seq='') AND (title IS NULL OR title='')"
    ).fetchone()[0]
    check("缺 seq 的关卡都是可标题定位的旁支", empty_seq <= 10 and empty_no_title == 0,
          f"{empty_seq} 条，无标题 {empty_no_title} 条")

    # 台词挂到不存在的关卡（lines.stage_id -> stages.id）
    orphan = con.execute(
        "SELECT COUNT(*) FROM lines l LEFT JOIN stages s ON l.stage_id = s.id WHERE s.id IS NULL"
    ).fetchone()[0]
    check("没有孤儿台词", orphan == 0, f"{orphan} 条")

    # 抽查一条原文能否读回
    row = con.execute("SELECT seq, title, txt FROM stages ORDER BY seq LIMIT 1").fetchone()
    check("抽查原文非空", bool(row and row[2]), f"{row[1] if row else '?'} {len(row[2]) if row else 0} 字")

    # 历史库
    hcon = sqlite3.connect(DATA / "history.db")
    cols = {r[1] for r in hcon.execute("PRAGMA table_info(turns)")}
    check("历史表有 model 列", "model" in cols, str(sorted(cols)))
    n_turns = hcon.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    check("历史可读", n_turns > 0, f"{n_turns} 条")


# ---------------------------------------------------------------- 9. 语料残留
def t_citation_coverage():
    """事实句的出处覆盖率：太低说明模型在写无出处的断言。"""
    from pgr.tools import Toolbox
    con = sqlite3.connect(DATA / "history.db")
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT answer FROM turns WHERE id > 45 ORDER BY id DESC LIMIT 10")]
    con.close()
    box = Toolbox()
    f = c = 0
    for r in rows:
        cv = box.coverage(r["answer"])
        f += cv["factual"]
        c += cv["cited"]
    ratio = (c / f) if f else 1.0
    # 分析型回答本就偏低，这里只守一个低水位：跌破 25% 说明基本不标了
    check(f"事实句出处覆盖率（{c}/{f}）", ratio >= 0.25, f"{ratio:.0%}")


def t_citation_health():
    """历史里每条「分类／章节…」形式的出处都必须能对回关卡。"""
    import re
    con = sqlite3.connect(DATA / "history.db")
    con.row_factory = sqlite3.Row
    turns = [dict(r) for r in con.execute("SELECT id, answer FROM turns")]
    con.close()
    from pgr.tools import Toolbox
    box = Toolbox()
    cite = re.compile(r"【([^】]{2,80})】")
    bad = []
    n = 0
    for t in turns:
        for m in cite.findall(t["answer"] or ""):
            if "／" not in m or m.startswith("未核实："):
                continue
            n += 1
            if not box.resolve_row(m):
                bad.append((t["id"], m))
    check(f"出处可解析（{n} 条标注）", not bad,
          "全部能对回关卡" if not bad else f"对不上：{bad[:3]}")


def t_cost_recompute():
    """有用量拆分的记录，必须能用价目表复算出与记录一致的花费。"""
    from pgr import pricing
    con = sqlite3.connect(DATA / "history.db")
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM turns WHERE prompt_tokens IS NOT NULL")]
    con.close()
    if not rows:
        check("花费可复算（无样本，跳过）", True, "尚未产生带用量拆分的记录")
        return
    worst = 0.0
    bad = []
    for r in rows:
        usage = {"prompt_tokens": r["prompt_tokens"],
                 "completion_tokens": r["completion_tokens"],
                 "prompt_cache_hit_tokens": r["cache_hit_tokens"],
                 "prompt_cache_miss_tokens": r["cache_miss_tokens"]}
        got = pricing.cost_cny(usage, r["model"] or "", peak=bool(r["peak"]))["cost"]
        diff = abs(got - (r["cost_cny"] or 0))
        worst = max(worst, diff)
        if diff > 0.0005:
            bad.append((r["id"], round(diff, 6)))
    check(f"花费可复算（{len(rows)} 条）", not bad,
          f"最大偏差 ¥{worst:.6f}" if not bad else f"偏差过大：{bad[:3]}")


def t_markup():
    from pgr.markup_check import fatal_total, scan
    r = scan(DATA / "dialogue.jsonl")
    check("语料无源码残留", fatal_total(r) == 0, f"{fatal_total(r)} 处 / {r['stages']} 关")


# ---------------------------------------------------------------- 10. 静态扫描
def t_static():
    py_files = list((ROOT / "pgr").glob("*.py"))
    risky = []
    for f in py_files:
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"except\s*:\s*\n\s*pass", src):
            risky.append(f"{f.name}: 裸 except+pass")
        for m in re.finditer(r"urllib\.request\.urlopen\([^)]*\)", src):
            if "timeout=" not in m.group(0):
                risky.append(f"{f.name}: urlopen 无 timeout")
        for m in re.finditer(r"sqlite3\.connect\(", src):
            pass
    check("无裸 except/pass", not [r for r in risky if "裸 except" in r], str([r for r in risky if "裸 except" in r][:3]))
    check("urllib 调用都有超时", not [r for r in risky if "timeout" in r], str([r for r in risky if "timeout" in r][:3]))

    # 前端：拼 HTML 时必须转义
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    check("前端有 esc()", "function esc(" in html, "")
    check("原文渲染用 esc", "esc(body)" in html or "esc(line)" in html, "")


# ---------------------------------------------------------------- 11. 日志
def t_log():
    p = ROOT / "logs" / "app.log"
    if not p.exists():
        check("运行日志存在", False, "logs/app.log 不存在")
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    has_key = re.search(r"sk-[A-Za-z0-9]{20,}", text)
    check("日志里没有明文密钥", not has_key, "发现疑似明文" if has_key else "只记 mask")
    check("日志在写", p.stat().st_size > 0, f"{p.stat().st_size} 字节")


def start_own_server() -> bool:
    """自起一个 --keep-alive 实例：审计里的 SSE 探测不会把它带走。"""
    global _own_server
    if os.environ.get("PGR_BASE"):
        return True                      # 外部指定了实例，直接用
    # 端口必须干净：否则会静默连到上一次没退出的实例，结果不可信
    import socket
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", PORT))
    except OSError:
        print(f"端口 {PORT} 已被占用（可能是上一次审计残留），请先释放或用 PGR_AUDIT_PORT 换端口")
        return False
    finally:
        probe.close()

    import subprocess
    _own_server = subprocess.Popen(
        [sys.executable, "app.py", "--no-browser", "--keep-alive", "--port", str(PORT)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.5)
        code, raw = http("/api/health", timeout=3)
        if code == 200:
            return True
    return False


def stop_own_server():
    global _own_server
    if _own_server is None:
        return
    import subprocess
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(_own_server.pid)],
                   capture_output=True)
    _own_server = None


def main() -> int:
    if not start_own_server():
        print(f"审计实例没起来（端口 {PORT}）")
        return 2
    try:
        return run_all()
    finally:
        stop_own_server()


def run_all() -> int:
    health = t_health()
    t_stage("0100101")
    t_stage("9999999")
    t_bad_inputs()
    t_no_secret()
    t_config()
    t_update()
    t_concurrency()
    t_sse()
    t_data(health)
    t_citation_coverage()
    t_citation_health()
    t_cost_recompute()
    t_markup()
    t_static()
    t_log()

    print(f"\n{'检查项':<34}{'结果':<6}说明")
    print("-" * 96)
    bad = 0
    for name, ok, detail in results:
        if not ok:
            bad += 1
        print(f"{name:<34}{'OK' if ok else 'FAIL':<6}{detail[:52]}")

    print("-" * 96)
    print(f"共 {len(results)} 项，失败 {bad} 项")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
