"""Paced, cached HTTP client for wiki.biligame.com/zspms.

The site sits behind Tencent EdgeOne. Besides normal traffic shaping it returns
HTTP 567 with an HTML block page ("请求已被站点的安全策略拦截") when requests
are too dense. We therefore keep a global minimum interval between requests and
back off for a long time when a 567 shows up.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://wiki.biligame.com/zspms"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
from .paths import ROOT, DATA, CACHE
HERE = str(ROOT)
BLOCK_STATUS = 567


class Blocked(Exception):
    """Server refused us with the EdgeOne block page."""


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class Wiki:
    def __init__(self, min_interval: float = 3.0, use_cache: bool = True, quiet: bool = False):
        self.min_interval = min_interval
        self.use_cache = use_cache
        self.quiet = quiet
        self._last = 0.0
        self.n_requests = 0
        cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )
        os.makedirs(CACHE, exist_ok=True)

    # ---------- low level ----------
    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)

    def _throttle(self) -> None:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, cache_group: str, cache_name: str, retries: int = 4) -> str:
        path = os.path.join(CACHE, cache_group, _sha(cache_name) + ".txt")
        if self.use_cache and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        last_err: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8")
            req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
            req.add_header("Referer", BASE + "/%E5%89%A7%E6%83%85%E5%9B%9E%E9%A1%BE")
            try:
                self.n_requests += 1
                with self._opener.open(req, timeout=40) as resp:
                    body = resp.read().decode("utf-8", "replace")
                self._last = time.time()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(body)
                return body
            except urllib.error.HTTPError as e:
                self._last = time.time()
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if e.code == BLOCK_STATUS:
                    # long cooldown: this is an IP-level throttle, not a per-request issue
                    cooldown = [120, 240, 480, 900][min(attempt, 3)]
                    self._log(f"  [567 blocked] cooling down {cooldown}s (attempt {attempt + 1}/{retries})")
                    last_err = Blocked(f"567 on {url}")
                    time.sleep(cooldown)
                    continue
                last_err = RuntimeError(f"HTTP {e.code} on {url}: {body[:200]}")
                if e.code == 404:
                    raise FileNotFoundError(url)
                time.sleep(5)
            except Exception as e:  # network hiccup
                self._last = time.time()
                last_err = e
                self._log(f"  [net error] {e} (attempt {attempt + 1}/{retries})")
                time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"giving up on {url}: {last_err}")

    def post(self, url: str, form: dict, cache_group: str, cache_name: str, retries: int = 4) -> str:
        """POST application/x-www-form-urlencoded, same caching/backoff rules."""
        path = os.path.join(CACHE, cache_group, _sha(cache_name) + ".txt")
        if self.use_cache and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        data = urllib.parse.urlencode(form).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("User-Agent", UA)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
            try:
                self.n_requests += 1
                with self._opener.open(req, timeout=120) as resp:
                    body = resp.read().decode("utf-8", "replace")
                self._last = time.time()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(body)
                return body
            except urllib.error.HTTPError as e:
                self._last = time.time()
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if e.code == BLOCK_STATUS:
                    cooldown = [120, 240, 480, 900][min(attempt, 3)]
                    self._log(f"  [567 blocked] cooling down {cooldown}s (attempt {attempt + 1}/{retries})")
                    last_err = Blocked(f"567 on {url}")
                    time.sleep(cooldown)
                    continue
                last_err = RuntimeError(f"HTTP {e.code} on {url}: {body[:300]}")
                time.sleep(5)
            except Exception as e:
                self._last = time.time()
                last_err = e
                self._log(f"  [net error] {e} (attempt {attempt + 1}/{retries})")
                time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"giving up on POST {url}: {last_err}")

    def export(self, titles: list[str], cache_name: str) -> str:
        """Special:Export bulk wikitext dump."""
        body = self.post(
            f"{BASE}/Special:Export",
            {"pages": "\n".join(titles), "curonly": "1", "action": "submit", "templates": "0"},
            "export",
            "export:" + cache_name,
        )
        return body

    # ---------- endpoints ----------
    def raw(self, title: str) -> str:
        q = urllib.parse.urlencode({"title": title, "action": "raw"})
        return self.get(f"{BASE}/index.php?{q}", "raw", "raw:" + title)

    def api(self, params: dict) -> str:
        params = dict(params)
        params.setdefault("format", "json")
        params.setdefault("formatversion", "2")
        q = urllib.parse.urlencode(params)
        return self.get(f"{BASE}/api.php?{q}", "api", "api:" + q)

    def api_json(self, params: dict):
        return json.loads(self.api(params))

    def ask(self, query: str, limit: int = 5000, offset: int = 0) -> dict:
        """SMW #ask over the API. `query` is the raw ask string, e.g.
        '[[剧情分类::+]]|?章节|?序号'."""
        return self.api_json(
            {"action": "ask", "query": f"{query}|limit={limit}|offset={offset}"}
        )


if __name__ == "__main__":
    w = Wiki()
    print(w.raw("序章"))
