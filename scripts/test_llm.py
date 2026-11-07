"""Probe the configured LLM endpoint with a tiny request.

Answers three questions at once:
  1. is config.json found and parsed?
  2. which base URL / model is actually used?
  3. does the key authenticate?  (HTTP code + server message, key never printed)

Run: python test_llm.py
"""
from __future__ import annotations

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import json
import sys
import urllib.error
import urllib.request

from pgr import llm


def main() -> int:
    d = llm.diagnose()
    print("== 配置 ==")
    print(f"  文件      : {d['config_path']}")
    print(f"  存在      : {d['exists']}")
    print(f"  解析错误  : {d['parse_error'] or '无'}")
    print(f"  密钥来源  : {d['source']}")
    print(f"  密钥长度  : {d['key_len']}")
    if not d["ok"]:
        print(f"  → 证据模式：{d['hint']}")
        print("\n把密钥写进 config.json（不是 config.example.json）：")
        print('  { "api_key": "sk-...", "base": "https://api.deepseek.com", "model": "deepseek-flash" }')
        return 1

    warn = d.get("format_warning")
    if warn:
        print(f"\n!! 密钥格式不对：{warn}")
        print("   → 控制台左侧「API keys」→ 创建 API key → 复制 sk- 开头的那串（只显示一次）")

    cfg = llm.load_config()
    print("\n== 请求 ==")
    print(f"  base  : {cfg['base']}")
    print(f"  model : {cfg['model']}")
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 4,
        "temperature": 0,
    }
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print(f"  HTTP  : 200")
        print(f"  模型  : {data.get('model')}")
        print(f"  回复  : {data['choices'][0]['message']['content'][:40]!r}")
        print("\n结论：密钥可用。重启 server.py 即可用模型作答。")
        return 0
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(f"  HTTP  : {e.code}")
        print(f"  返回  : {detail}")
        if e.code == 401:
            print("\n结论：密钥无效。检查三点：")
            print("  · DeepSeek 官方密钥以 sk- 开头，共 35 位；若你的不是，说明来自中转站，")
            print("    需要把 config.json 的 base 改成那家的地址（多数形如 https://xxx/v1）。")
            print("  · 复制时是否少了一段 / 多了空格。")
            print("  · 密钥是否已被禁用或过期。")
        return 1
    except Exception as e:
        print(f"  异常  : {type(e).__name__}: {e}")
        print("\n结论：网络层没通（代理 / DNS / base 地址写错）。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
