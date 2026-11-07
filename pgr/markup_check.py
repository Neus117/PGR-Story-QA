"""语料残留检查（被 scripts/check_markup.py 与更新流程共用）。

判据是「读者会看到的东西里不该出现的字符」：标签、属性、模板括号、链接括号、实体。
尖括号伪代码（<homo_sapiens>、<end_loop>）与横线属内容，只提示不判失败。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FATAL = {
    "class=": re.compile(r"class\s*="),
    "style=": re.compile(r"style\s*="),
    "模板括号": re.compile(r"\{\{|\}\}"),
    "链接括号": re.compile(r"\[\[|\]\]"),
    "表格标记": re.compile(r"^\s*\{\||\|\}\s*$", re.M),
    "HTML 标签": re.compile(
        r"</?(?:div|span|font|table|tr|td|th|p|a|b|i|u|s|em|strong|br|hr|img|center|"
        r"small|big|sup|sub|code|pre|blockquote|ul|ol|li|style|script)\b[^>]{0,80}>",
        re.I),
    "HTML 实体": re.compile(r"&(?:nbsp|lt|gt|amp|quot);"),
    "px 尺寸": re.compile(r"\b\d+px\b"),
}

INFO = {
    "横线 ----": re.compile(r"^\s*-{4,}\s*$", re.M),
    "尖括号伪代码": re.compile(r"<[a-zA-Z_][a-zA-Z0-9_]{1,20}>"),
}


def scan(path: Path | str) -> dict:
    stat = {k: [0, 0, []] for k in FATAL}
    info = {k: [0, 0] for k in INFO}
    total = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            total += 1
            text = d.get("text") or ""
            title = d.get("title") or "?"
            for name, pat in FATAL.items():
                hits = pat.findall(text)
                if hits:
                    stat[name][0] += len(hits)
                    stat[name][1] += 1
                    if len(stat[name][2]) < 5:
                        m = pat.search(text)
                        stat[name][2].append(
                            (title, text[max(0, m.start() - 40): m.end() + 40].replace("\n", "⏎")))
            for name, pat in INFO.items():
                hits = pat.findall(text)
                if hits:
                    info[name][0] += len(hits)
                    info[name][1] += 1
    return {"stages": total, "fatal": stat, "info": info}


def fatal_total(result: dict) -> int:
    return sum(v[0] for v in result["fatal"].values())
