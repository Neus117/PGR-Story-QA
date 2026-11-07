"""联网检索：只在指定社区范围内找，且结果一律标为「外部资料」。

用途：词法检索的硬边界是"某个昵称在语料里一次都没出现"。这类东西只能向外找。

范围（白名单，其余域名一律丢弃）：
  wiki.biligame.com  战双 BWIKI（官方整理，最可信）
  bilibili.com       哔哩哔哩（专栏/视频简介）
  nga.cn / tieba.baidu.com / kurobbs.com / taptap.cn   玩家社区

两个后端：
  1. BWIKI 站内检索（MediaWiki search API）—— 最快最准，优先
  2. Bing 网页检索 —— 兜底，结果按白名单过滤
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

ALLOWED = (
    "wiki.biligame.com",
    "bilibili.com",
    "nga.cn",
    "tieba.baidu.com",
    "kurobbs.com",
    "taptap.cn",
    "taptap.com",
)
BWIKI_API = "https://wiki.biligame.com/zspms/api.php"


def _get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def allowed(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED)


def wiki_search(term: str, limit: int = 4) -> list[dict]:
    """战双 BWIKI 站内检索。返回命中的页面名与摘要。"""
    q = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": term,
        "srlimit": limit, "format": "json",
    })
    try:
        data = _get(f"{BWIKI_API}?{q}")
        import json
        d = json.loads(data)
    except Exception:
        return []
    out = []
    for item in d.get("query", {}).get("search", []):
        title = item.get("title", "")
        snippet = re.sub(r"<[^>]+>", "", item.get("snippet", ""))
        out.append({
            "title": title,
            "url": "https://wiki.biligame.com/zspms/" + urllib.parse.quote(title.replace(" ", "_")),
            "snippet": html.unescape(snippet)[:200],
            "source": "战双 BWIKI",
        })
    return out


BING_ITEM = re.compile(r'<li class="b_algo".*?</li>', re.S)
BING_TITLE = re.compile(r'<h2>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
BING_SNIP = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def bing_search(term: str, limit: int = 8) -> list[dict]:
    q = urllib.parse.urlencode({"q": term, "setlang": "zh-CN", "cc": "CN"})
    try:
        body = _get(f"https://www.bing.com/search?{q}")
    except Exception:
        return []
    out = []
    for block in BING_ITEM.findall(body):
        m = BING_TITLE.search(block)
        if not m:
            continue
        url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        if not allowed(url):
            continue
        sm = BING_SNIP.search(block)
        snip = re.sub(r"<[^>]+>", "", sm.group(1)) if sm else ""
        out.append({
            "title": html.unescape(title).strip(),
            "url": url,
            "snippet": html.unescape(snip).strip()[:220],
            "source": urllib.parse.urlparse(url).netloc,
        })
        if len(out) >= limit:
            break
    return out


def search(term: str, limit: int = 5, use_bing: bool = True) -> list[dict]:
    """先问 BWIKI，再补网页。去重，保持白名单。"""
    term = term.strip()
    if not term:
        return []
    seen, out = set(), []

    def push(rows):
        for r in rows:
            key = r["url"].split("#")[0]
            if key in seen or not allowed(r["url"]):
                continue
            seen.add(key)
            out.append(r)

    push(wiki_search(term, limit=min(4, limit)))
    if use_bing and len(out) < limit:
        push(bing_search(f"{term} 战双帕弥什", limit=limit * 3))
    return out[:limit]


if __name__ == "__main__":
    import sys
    t = " ".join(sys.argv[1:]) or "小梅 伊什梅尔"
    for r in search(t):
        print(f"[{r['source']}] {r['title']}")
        print(f"   {r['url']}")
        print(f"   {r['snippet'][:120]}")
