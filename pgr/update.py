"""增量更新：检查 wiki 是否有新内容/改动，并按需抓取 → 清洗 → 重建索引。

设计要点
  · **不做全量重爬**。检查只用 2 次请求：一次 SMW 枚举（拿当前全部关卡），
    一次 recentchanges（拿最近的编辑）。新增/删除靠集合对比，改动靠 recentchanges。
  · **限流友好**：抓取沿用 `Wiki` 的 4 秒/请求 + HTTP 567 分钟级冷却；
    连续两次「检查」之间至少隔 `CHECK_COOLDOWN`，连续两次「更新」至少隔 `APPLY_COOLDOWN`。
  · **失败不破坏现有数据**：抓取先落 raw/；解析和建索引失败时旧库仍在（索引写临时文件后原子替换）。
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from .paths import DATA
from .wiki import Wiki, BASE

STATE = DATA / "sync.json"
RAW = DATA / "raw"
STAGES = DATA / "stages.json"
DB = DATA / "pgr.db"

CHECK_COOLDOWN = 30          # 秒：两次检查之间
ATTEMPT_COOLDOWN = 60        # 秒：两次「点更新」之间（哪怕只是检查）
APPLY_COOLDOWN = 600         # 秒：两次更新之间（防频繁触发站点限流）
EXPORT_BATCH = 50
NS = "{http://www.mediawiki.org/xml/export-0.11/}"


def _now() -> datetime:
    return datetime.now().astimezone()


def load_state() -> dict:
    if STATE.exists():
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 首次：以建库时间为基线，避免"上次同步"无从判断
    base = DB.stat().st_mtime if DB.exists() else (_now() - timedelta(days=7)).timestamp()
    return {
        "last_sync": datetime.fromtimestamp(base).astimezone().isoformat(timespec="seconds"),
        "checked_at": None,
        "last_check_request": 0.0,
        "last_apply_request": 0.0,
        "last_attempt": 0.0,
        "pages": 0,
        "last_result": "",
    }


def save_state(st: dict) -> None:
    tmp = str(STATE) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _local_titles() -> dict[str, dict]:
    if not STAGES.exists():
        return {}
    with open(STAGES, encoding="utf-8") as f:
        rows = json.load(f)
    return {r["title"]: r for r in rows}


def _fetch_stage_map(w: Wiki) -> dict[str, dict]:
    """SMW 枚举当前全部关卡（与 pgr/enumerate.py 同口径）。"""
    rows: dict[str, dict] = {}
    offset = 0
    while True:
        res = w.ask("[[剧情分类::+]]|?标题|?章节|?序号|?分类", limit=5000, offset=offset)
        if "error" in res:
            raise RuntimeError("SMW 查询失败: " + json.dumps(res["error"], ensure_ascii=False)[:300])
        q = res.get("query", {})
        results = q.get("results", {})
        meta = q.get("meta", {})
        for title, data in results.items():
            po = data.get("printouts", {})
            rows[title] = {
                "title": title,
                "title_prop": (po.get("标题") or [""])[0] if po.get("标题") else "",
                "chapter": (po.get("章节") or [""])[0] if po.get("章节") else "",
                "seq": (po.get("序号") or [None])[0] if po.get("序号") else None,
                "cats": [c.get("fulltext", "").replace("分类:", "") for c in po.get("分类", []) if isinstance(c, dict)],
            }
        total = meta.get("count")
        if not results or total is None or offset + len(results) >= total:
            break
        offset += len(results)
    return rows


def _recent_edits(w: Wiki, since: str, limit: int = 500) -> list[dict]:
    """自 since 起被编辑过的页面（主命名空间）。"""
    out: list[dict] = []
    cont = {}
    while True:
        params = {
            "action": "query", "list": "recentchanges",
            "rcprop": "title|timestamp", "rclimit": "500",
            "rcdir": "newer", "rcstart": since, "rcnamespace": "0",
            "format": "json", "formatversion": "2",
        }
        params.update(cont)
        q = "&".join(f"{k}={v}" for k, v in params.items())
        try:
            data = json.loads(w.get(f"{BASE}/api.php?{q}", "api", "rc:" + q))
        except Exception:
            break
        out.extend(data.get("query", {}).get("recentchanges", []))
        nxt = data.get("continue")
        if not nxt or len(out) >= limit:
            break
        cont = nxt
    return out


def check_updates(force: bool = False) -> dict:
    """只读：返回 {ok, new, changed, deleted, since, total}。不修改任何数据。"""
    st = load_state()
    now = time.time()
    if not force and now - float(st.get("last_check_request") or 0) < CHECK_COOLDOWN:
        return {"ok": False, "error": "cooldown",
                "message": f"检查太频繁，请等 {int(CHECK_COOLDOWN - (now - float(st.get('last_check_request') or 0)))} 秒再试"}

    st["last_check_request"] = now
    save_state(st)

    # 检查与抓取必须读实时数据：不能用本地 HTTP 缓存，否则永远看到第一次的结果
    w = Wiki(min_interval=4.0, use_cache=False)
    try:
        remote = _fetch_stage_map(w)
    except Exception as e:
        return {"ok": False, "error": "network", "message": f"连不上 wiki：{type(e).__name__}: {e}"}

    local = _local_titles()
    new = sorted(set(remote) - set(local))
    deleted = sorted(set(local) - set(remote))

    since = st.get("last_sync") or (_now() - timedelta(days=7)).isoformat(timespec="seconds")
    edits = []
    try:
        edits = [r["title"] for r in _recent_edits(w, since)]
    except Exception:
        pass
    changed = sorted({t for t in edits if t in local and t not in new})

    st["checked_at"] = _now().isoformat(timespec="seconds")
    st["pages"] = len(remote)
    save_state(st)

    return {
        "ok": True,
        "since": since,
        "checked_at": st["checked_at"],
        "local_pages": len(local),
        "remote_pages": len(remote),
        "new": new,
        "changed": changed,
        "deleted": deleted,
        "has_update": bool(new or changed or deleted),
        "recent_edits_scanned": len(edits),
    }


def _export_pages(w: Wiki, titles: list[str], progress=None) -> dict[str, str]:
    """批量导出 wikitext（每批 50 页）。"""
    got: dict[str, str] = {}
    for i in range(0, len(titles), EXPORT_BATCH):
        chunk = titles[i:i + EXPORT_BATCH]
        if progress:
            progress(f"下载 {i + 1}-{i + len(chunk)} / {len(titles)}")
        body = w.post(f"{BASE}/api.php", {
            "action": "query", "export": "1", "exportnowrap": "1",
            "format": "xml", "titles": "|".join(chunk),
        }, "api", "export50:" + "|".join(chunk))
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        for page in root.iter(NS + "page"):
            t = page.findtext(NS + "title") or ""
            rev = page.find(NS + "revision")
            text = (rev.findtext(NS + "text") if rev is not None else "") or ""
            if t:
                got[t] = text
    return got


def apply_updates(plan: dict | None = None, progress=None) -> dict:
    """抓变化 → 写 raw → 重新解析 → 重建索引。返回结果摘要。"""
    st = load_state()
    now = time.time()
    if now - float(st.get("last_apply_request") or 0) < APPLY_COOLDOWN:
        wait = int(APPLY_COOLDOWN - (now - float(st.get("last_apply_request") or 0)))
        return {"ok": False, "error": "cooldown",
                "message": f"更新太频繁会触发站点限流，请等 {wait} 秒（约 {wait // 60 + 1} 分钟）再试"}
    st["last_apply_request"] = now
    save_state(st)

    plan = plan or check_updates(force=True)
    if not plan.get("ok"):
        return plan
    if not plan.get("has_update"):
        return {"ok": True, "no_change": True,
                "message": "wiki 源暂无新内容",
                "checked_at": plan.get("checked_at")}

    to_fetch = list(plan["new"]) + list(plan["changed"])
    t0 = time.time()
    # 检查与抓取必须读实时数据：不能用本地 HTTP 缓存，否则永远看到第一次的结果
    w = Wiki(min_interval=4.0, use_cache=False)
    try:
        if progress:
            progress(f"开始下载 {len(to_fetch)} 个页面")
        got = _export_pages(w, to_fetch, progress)
    except Exception as e:
        return {"ok": False, "error": "network", "message": f"下载失败：{type(e).__name__}: {e}"}

    if progress:
        progress("写入原始文件")
    RAW.mkdir(parents=True, exist_ok=True)
    for title, text in got.items():
        with open(RAW / (title.replace("/", "_") + ".txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    for title in plan["deleted"]:
        p = RAW / (title.replace("/", "_") + ".txt")
        if p.exists():
            p.unlink()

    # 用本次枚举结果覆盖 stages.json（新增/删除都会反映出来）
    if progress:
        progress("更新关卡清单")
    # 复用 check 刚写进缓存的枚举结果：几秒前的数据，没必要再走一次网络
    remote = _fetch_stage_map(Wiki(min_interval=1.0))
    tmp = str(STAGES) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(sorted(remote.values(),
                         key=lambda r: (r["seq"] is None, r["seq"] or 0, r["title"])),
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, STAGES)

    if progress:
        progress("重新解析")
    from . import parse, index
    try:
        parse.main()
    except SystemExit as e:
        if e.code not in (0, None):
            return {"ok": False, "error": "parse", "message": "解析失败，已保留旧数据"}
    if progress:
        progress("重建索引")
    try:
        index.main()
    except SystemExit as e:
        if e.code not in (0, None):
            return {"ok": False, "error": "index", "message": "建索引失败，已保留旧库"}
    except Exception as e:
        return {"ok": False, "error": "index", "message": f"建索引失败：{type(e).__name__}: {e}"}

    st = load_state()
    st["last_sync"] = _now().isoformat(timespec="seconds")
    st["pages"] = len(remote)
    st["last_result"] = f"新增 {len(plan['new'])} / 更新 {len(plan['changed'])} / 删除 {len(plan['deleted'])}"
    save_state(st)

    return {
        "ok": True, "no_change": False,
        "new": len(plan["new"]), "changed": len(plan["changed"]), "deleted": len(plan["deleted"]),
        "downloaded": len(got), "pages": len(remote),
        "elapsed": round(time.time() - t0, 1),
        "message": st["last_result"],
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(check_updates(force="--force" in sys.argv), ensure_ascii=False, indent=1))
