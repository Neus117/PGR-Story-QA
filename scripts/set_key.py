"""写 DeepSeek 密钥到 config.json，并在写盘前校验格式。

    python set_key.py

交互输入（不回显），校验通过才覆盖 config.json；
保留原有的 base / model / timeout。不把密钥写进命令行参数，免得留在历史里。
"""
from __future__ import annotations

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import getpass
import io
import json
import os
import sys

CONFIG = str(_pl.Path(__file__).resolve().parent.parent / "config.json")
DEFAULT = {"api_key": "", "base": "https://api.deepseek.com",
           "model": "deepseek-flash", "timeout": 90}


from pgr.llm import key_shape_warning  # noqa: E402  同一个判据，不复制一份


def load() -> dict:
    cfg = dict(DEFAULT)
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8-sig") as f:
                cfg.update(json.load(f) or {})
        except Exception as e:
            print(f"现有 config.json 读不了（{e}），将重建。")
    return cfg


def main() -> int:
    cfg = load()
    cur = (cfg.get("api_key") or "").strip()
    print(f"配置文件：{CONFIG}")
    print(f"当前密钥：{'已填（' + str(len(cur)) + ' 位）' if cur else '空'}")
    warn = key_shape_warning(cur)
    if warn:
        print(f"  但有格式问题：{warn}")
    print("\nDeepSeek 控制台 → 左侧「API keys」→ 创建 API key → 复制 sk- 开头那串（只显示一次）")
    print("直接回车 = 放弃修改\n")

    try:
        key = getpass.getpass("粘贴密钥（输入不回显）：").strip()
    except Exception:
        key = input("粘贴密钥：").strip()

    if not key:
        print("未输入，不做修改。")
        return 1

    while True:
        warn = key_shape_warning(key)
        if not warn:
            break
        print(f"\n格式不对：{warn}")
        again = input("重新粘贴（回车放弃）：").strip()
        if not again:
            print("已放弃，未写入。")
            return 1
        key = again

    cfg["api_key"] = key
    tmp = CONFIG + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG)

    print(f"\n已写入 {CONFIG}")
    print(f"  密钥长度 {len(key)}，前缀 {key[:3]}…")
    print("\n下一步验证：python test_llm.py")
    print("（服务端每次请求都会重读配置，不用重启；页面刷新即可）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
