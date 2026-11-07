# -*- coding: utf-8 -*-
"""健壮性检查：接口边界 + 前端渲染 + 异常输入。只读，不消耗模型调用。"""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("PGR_BASE", "http://127.0.0.1:8712")


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


CASES = [
    ("缺 seq", "/api/stage"),
    ("空 seq", "/api/stage?seq="),
    ("不存在的 seq", "/api/stage?seq=9999999"),
    ("中文 seq", "/api/stage?seq=" + urllib.parse.quote("序章01")),
    ("SQL 味儿的 seq", "/api/stage?seq=" + urllib.parse.quote("1' OR '1'='1")),
    ("超长 seq", "/api/stage?seq=" + "9" * 500),
    ("正常 seq", "/api/stage?seq=0124102"),
    ("历史 limit 非法", "/api/history?limit=abc"),
    ("历史 limit 超大", "/api/history?limit=99999"),
    ("历史 offset 负数", "/api/history?offset=-5"),
    ("历史指定不存在 session", "/api/history?session=NOPE"),
    ("余额", "/api/balance"),
    ("健康", "/api/health"),
    ("不存在的路由", "/api/nope"),
]

fails = 0
print(f"{'检查项':<24} {'状态':<6} 结果")
print("-" * 78)
for name, path in CASES:
    code, body = get(path)
    ok = code in (200, 400, 404)
    if not ok:
        fails += 1
    snippet = body[:70].replace("\n", " ")
    print(f"{name:<24} {code:<6} {'OK  ' if ok else 'FAIL'} {snippet}")

# 结构校验
code, body = get("/api/stage?seq=0124102")
d = json.loads(body)
need = {"ok", "seq", "title", "chapter", "category", "text", "chars"}
missing = need - set(d)
print("\n/api/stage 字段完整性:", "OK" if not missing else f"缺 {missing}")
if missing:
    fails += 1

code, body = get("/api/history?limit=3")
h = json.loads(body)
cites = h["turns"][0]["citations"] if h.get("turns") else []
enriched = [c for c in cites if c.get("seq")]
print(f"历史出处补全: {len(enriched)}/{len(cites)} 条带 seq")
if cites and not enriched:
    fails += 1

print(f"\n{'全部通过' if fails == 0 else str(fails) + ' 项异常'}")
sys.exit(0 if fails == 0 else 1)
