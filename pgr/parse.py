"""Parse wiki stage wikitext into structured dialogue JSONL  (v2).

Design
------
1. Inline text is rendered innermost-template-first: `{{...}}` with no nested
   braces is replaced by its evaluated text, repeat until none left. This
   handles wrappers such as {{灰色|{{剧情/对话|..}}}} without leaking markup.
2. Block level recurses: wrapper templates whose payload is block markup are
   re-parsed as blocks, so colour/spoiler wrappers keep their structure.
3. Every template name we do not knowingly handle is recorded in the audit so
   nothing is dropped silently.

Input : data/raw/<title>.txt   Output: data/dialogue.jsonl, data/audit.json
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter

from .paths import DATA as _DATA

DATA = str(_DATA)
RAW = os.path.join(DATA, "raw")

# ---------------------------------------------------------------- helpers
def split_params(inner: str) -> list[str]:
    """Split a template body on top-level '|' (respecting {{ }} and [[ ]])."""
    out, buf, dc, db = [], [], 0, 0
    i, n = 0, len(inner)
    while i < n:
        two = inner[i : i + 2]
        if two == "{{":
            dc += 1
            buf.append(two)
            i += 2
            continue
        if two == "}}":
            dc = max(0, dc - 1)
            buf.append(two)
            i += 2
            continue
        if two == "[[":
            db += 1
            buf.append(two)
            i += 2
            continue
        if two == "]]":
            db = max(0, db - 1)
            buf.append(two)
            i += 2
            continue
        c = inner[i]
        if c == "|" and dc == 0 and db == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


def name_of(inner: str) -> str:
    return re.split(r"[|}]", inner, maxsplit=1)[0].strip()


def find_templates(text: str):
    """Yield (start, end, name, inner) for top-level {{...}} calls."""
    i, n = 0, len(text)
    while i < n:
        j = text.find("{{", i)
        if j < 0:
            return
        depth, k = 0, j
        while k < n:
            if text.startswith("{{", k):
                depth += 1
                k += 2
                continue
            if text.startswith("}}", k):
                depth -= 1
                k += 2
                if depth == 0:
                    break
                continue
            k += 1
        else:
            return
        inner = text[j + 2 : k - 2]
        yield j, k, name_of(inner), inner
        i = k


LINK_RE = re.compile(r"\[\[([^\[\]|]*)\|([^\[\]]*)\]\]|\[\[([^\[\]]*)\]\]")
# [[file:xxx.png|class=ing-kk|50px]] / [[文件:...]] —— 整条丢弃
FILE_LINK_RE = re.compile(r"\[\[\s*(?:file|image|文件|图像)\s*:.*?\]\]", re.I | re.S)
EXTLINK_RE = re.compile(r"\[(https?://[^\s\]]+)(?:\s+([^\]]*))?\]")
TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*/?>")


def _flatten_tables(s: str) -> str:
    """把 {| ... |} 维基表格拍成可读文本。

    单元格形如 `|rowspan=2 style="..."|文本`：属性前缀截到第一个 | 为止。
    同一 `|-` 行内的多个单元格拼成一行，用 ｜ 连接；表格结构与样式全部丢弃。
    """
    out: list[str] = []
    row: list[str] = []

    def flush():
        if row:
            out.append(" ｜ ".join(row))
            row.clear()

    in_table = False
    for line in s.split("\n"):
        st = line.strip()
        if st.startswith("{|"):
            in_table = True
            row.clear()
            continue
        if in_table and st.startswith("|}"):
            flush()
            in_table = False
            continue
        if in_table:
            if st.startswith("|-"):
                flush()
                continue
            if not st:
                continue
            if st[0] in "!|":
                for cell in st[1:].split("||"):
                    if "|" in cell:
                        head, _, tail = cell.partition("|")
                        if "=" in head:           # 属性前缀，丢掉
                            cell = tail
                    cell = cell.strip().lstrip("!|").strip()
                    if cell:
                        row.append(cell)
                continue
            continue
        out.append(line)
    flush()
    return "\n".join(out)


def strip_markup(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = _flatten_tables(s)
    # 图片/文件链接必须整条删掉：它的"显示文字"是 class=/50px 这类参数，
    # 用普通链接的规则替换会把参数漏进正文。
    s = FILE_LINK_RE.sub("", s)
    s = s.replace("[[" + "文件:", "").replace("[[" + "File:", "").replace("[[" + "file:", "")
    s = LINK_RE.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(3), s)
    # 维基横线 ---- 不是内容
    s = re.sub(r"^\s*-{4,}\s*$", "", s, flags=re.M)
    s = EXTLINK_RE.sub(lambda m: m.group(2) or m.group(1), s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<hr\s*/?>", "\n", s, flags=re.I)
    s = TAG_RE.sub("", s)
    s = s.replace("'''", "").replace("''", "")
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&quot;", '"')):
        s = s.replace(a, b)
    s = s.replace("\t", " ")
    s = re.sub(r"[ \u00a0]+", " ", s)
    # 残留模板（源数据括号错位时会出现）与孤立花括号
    s = re.sub(r"\{\{[^{}]{0,200}\}\}", "", s)
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()


# ---------------------------------------------------------------- inline evaluators
COLOR_WRAPPERS = {"颜色引用", "灰色", "红色", "蓝色", "绿色", "黄色", "橙色", "紫色", "粉色",
                  "白色", "黑色", "青色", "灰色文字", "辅助蓝", "金色"}
INLINE_DROP = {"图片查看按钮", "视频引用", "B站视频", "插入视频", "剧情/一览", "切换显示按钮",
               "选项卡", "面包屑", "Swiper", "选项卡结束"}


def eval_inline(name: str, params: list[str], audit: Counter) -> str:
    """Evaluate one template occurrence to plain text (params[0] is the name)."""
    p = params[1:]

    def longest():
        vals = [x for x in p if x and "=" not in x]
        return max(vals, key=len) if vals else ""

    if name in COLOR_WRAPPERS:
        return p[-1] if p else ""
    if name in ("黑幕", "特效字", "修正", "剧情/居右", "居右"):
        return p[0] if p else ""
    if name == "标注":
        if len(p) >= 2 and p[1].strip():
            return f"{p[0]}（{p[1]}）"
        return p[0] if p else ""
    if name == "引用":
        audit["note:引用"] += 1          # editorial footnote, not story text
        return ""
    if name == "设定":
        return p[-1] if p else ""
    if name in INLINE_DROP:
        audit[f"drop:{name}"] += 1
        return ""
    if name.startswith("#"):
        audit[f"parserfn:{name}"] += 1
        return ""
    if name in ("剧情/对话", "剧情/选择", "剧情/战斗提示", "剧情/导语", "关卡引言",
                "节后信息", "免疫之城", "人物对话", "角色邮件"):
        audit[f"nested:{name}"] += 1
        return ""
    audit[f"unknown:{name}"] += 1
    # leave possible nested markup intact: render_inline() will rescan it
    return longest()


def render_inline(s: str, audit: Counter) -> str:
    for _ in range(20):
        m = re.search(r"\{\{([^{}]*)\}\}", s)
        if not m:
            break
        params = split_params(m.group(1))
        rep = eval_inline(name_of(m.group(1)), params, audit)
        s = s[: m.start()] + rep + s[m.end() :]
    return strip_markup(s)


# ---------------------------------------------------------------- block parser
BLOCK_WRAPPERS = {"灰色", "黑幕", "引用", "设定", "特效字", "剧情/居右", "居右",
                  "颜色引用"} | COLOR_WRAPPERS
BLOCK_SKIP = {"剧情/一览", "图片查看按钮", "视频引用", "B站视频", "插入视频", "Swiper",
              "选项卡", "切换显示", "面包屑"}
BATTLE_KEYS = ("大标题", "提示", "小标题")


def parse_blocks(text: str, audit: Counter, depth: int = 0) -> list[dict]:
    if depth > 6:
        return []
    blocks: list[dict] = []
    plain: list[str] = []

    def flush():
        raw = "".join(plain)
        plain.clear()
        txt = render_inline(raw, audit) if "{{" in raw else strip_markup(raw)
        if txt and not re.fullmatch(r"[\s\-—–·。,.、:：|]*", txt):
            blocks.append({"t": "say", "who": "", "text": txt})

    i = 0
    for start, end, name, inner in find_templates(text):
        if start < i:
            continue
        plain.append(text[i:start])
        i = end
        params = split_params(inner)

        if name == "剧情/对话":
            flush()
            who = render_inline(params[1], audit) if len(params) > 1 else ""
            body = render_inline("|".join(params[2:]), audit) if len(params) > 2 else ""
            if body:
                blocks.append({"t": "say", "who": who, "text": body})
        elif name == "剧情/选择":
            flush()
            opts = []
            rest = params[1:]
            named = [x for x in rest if re.match(r"\s*折叠\s*=", x)]
            rest = [x for x in rest if x not in named]
            for idx in range(0, len(rest) - 1, 2):
                label = render_inline(rest[idx], audit)
                body = parse_blocks(rest[idx + 1], audit, depth + 1)
                # wiki 的写法是「选项文字 + 指挥官把这句话说出来」，原文里 78% 的选项都这样。
                # 保留选项标记（分支信息），去掉重复的那句台词。
                if (body and body[0]["t"] == "say"
                        and (body[0]["text"] or "").strip() == (label or "").strip()):
                    body = body[1:]
                if body or label:
                    opts.append({"label": label, "blocks": body})
            if opts:
                blocks.append({"t": "choice", "options": opts})
        elif name == "剧情/战斗提示":
            flush()
            # 这个模板三个参数的语义完全不同，必须分开记：
            #   大标题 = 战斗开始/结束之类的节点
            #   提示   = 地点卡（"巴德尔号／大西洋海域"）或行动目标
            #   小标题 = 屏幕滚动的旁白（新闻播报、内心声音）
            key, text = "", ""
            for part in params[1:]:
                if "=" in part:
                    k, _, v = part.partition("=")
                    k = k.strip()
                    v = render_inline(v, audit)
                    if k in BATTLE_KEYS and v and not text:
                        key, text = k, v
                elif part.strip() and not text:
                    key, text = "提示", render_inline(part, audit)
            if text:
                blocks.append({"t": "battle", "key": key, "text": text})
        elif name in ("剧情/导语", "关卡引言"):
            flush()
            if name == "剧情/导语":
                body = render_inline("|".join(params[1:]), audit)
                if body:
                    blocks.append({"t": "intro", "text": body})
            else:
                kv = {}
                for part in params[1:]:
                    if "=" in part:
                        k, _, v = part.partition("=")
                        kv[k.strip()] = render_inline(v, audit)
                for key in ("导语", "梗概"):
                    if kv.get(key):
                        blocks.append({"t": "intro", "key": key, "text": kv[key]})
                extras = []
                for key in ("通关目标", "出战成员", "成就", "条件"):
                    if kv.get(key):
                        extras.append(f"{key}：{kv[key]}")
                if extras:
                    blocks.append({"t": "meta", "text": "\n".join(extras)})
                for key, val in kv.items():
                    if key not in ("导语", "梗概", "通关目标", "出战成员", "成就", "条件", "名字", "描述"):
                        pass
                if kv.get("描述"):
                    blocks.append({"t": "meta", "key": "成就描述", "text": kv["描述"]})
        elif name == "提示":
            flush()
            p = params[1:]
            if p and p[0].strip() in COLOR_WRAPPERS | {"蓝", "红"}:
                p = p[1:]
            raw = "|".join(p)
            if "{{" in raw:
                blocks.extend(parse_blocks(raw, audit, depth + 1))
            else:
                body = strip_markup(raw)
                if body:
                    blocks.append({"t": "note", "text": body})
        elif name == "引言框":
            flush()
            raw = "|".join(params[1:])
            if "通关目标" in raw or "全员存活" in raw:
                audit["drop:引言框(通关目标)"] += 1
            elif "{{" in raw:
                blocks.extend(parse_blocks(raw, audit, depth + 1))
            else:
                body = strip_markup(raw)
                if body:
                    blocks.append({"t": "quote", "text": body})
        elif name == "剧情":
            flush()
        elif name == "节后信息":
            flush()
            kv = {}
            for part in params[1:]:
                if "=" in part:
                    k, _, v = part.partition("=")
                    kv[k.strip()] = render_inline(v, audit)
            for n in range(1, 8):
                title, body = kv.get(f"标题{n}"), kv.get(f"内容{n}")
                if body:
                    blocks.append({"t": "extra", "key": title or "节后信息", "text": body})
        elif name == "免疫之城":
            flush()
            kv = {}
            for part in params[1:]:
                if "=" in part:
                    k, _, v = part.partition("=")
                    kv[k.strip()] = render_inline(v, audit)
            for key in ("标题", "描述", "探索后"):
                if kv.get(key):
                    blocks.append({"t": "extra", "key": key, "text": kv[key]})
        elif name == "人物对话":
            flush()
            if len(params) > 3 and params[1] in ("左", "右", "中"):
                who = render_inline(params[2], audit)
                body = render_inline(params[3], audit)
                if body:
                    blocks.append({"t": "say", "who": who, "text": body})
            elif len(params) > 2 and params[1] == "可用选项":
                sub = parse_blocks(params[2], audit, depth + 1)
                for b in sub:
                    blocks.append(b)
            else:
                audit[f"person_dialog:{params[1] if len(params) > 1 else '?'}"] += 1
        elif name == "角色邮件":
            flush()
            kv = {}
            for part in params[1:]:
                if "=" in part:
                    k, _, v = part.partition("=")
                    kv[k.strip()] = render_inline(v, audit)
            head = "｜".join(x for x in (kv.get("角色名"), kv.get("邮件标题")) if x)
            if kv.get("邮件内容"):
                blocks.append({"t": "extra", "key": "邮件" + ("（" + head + "）" if head else ""),
                               "text": kv["邮件内容"] + (f"\n附件：{kv['附件']}" if kv.get("附件") else "")})
        elif name in BLOCK_WRAPPERS:
            flush()
            payload = "|".join(params[-1:]) if name in COLOR_WRAPPERS else "|".join(params[1:])
            if "{{" in payload:
                blocks.extend(parse_blocks(payload, audit, depth + 1))
            else:
                audit[f"wrap_text:{name}"] += 1
                txt = strip_markup(payload)
                if txt:
                    blocks.append({"t": "say", "who": "", "text": txt})
        elif name in BLOCK_SKIP:
            flush()
            audit[f"drop:{name}"] += 1
        elif name.startswith("#"):
            flush()
            audit[f"parserfn:{name}"] += 1
        else:
            # unknown block template: try to recover its prose, then audit
            flush()
            audit[f"unknown:{name}"] += 1
            rep = eval_inline(name, params, audit)
            if rep:
                blocks.append({"t": "say", "who": "", "text": rep})
    plain.append(text[i:])
    flush()
    return blocks


# ---------------------------------------------------------------- salvage
SALVAGE_NEEDLES = ("{{剧情/对话", "{{剧情/选择", "{{剧情/战斗提示", "{{颜色引用",
                   "{{黑幕", "{{提示", "{{人物对话", "{{特效字", "{{蓝色", "{{红色")


def salvage(blocks: list[dict], audit: Counter, depth: int = 0) -> list[dict]:
    """Re-parse text that still holds markup.

    Unbalanced braces in a handful of source pages make an open template swallow
    the following dialogue; the swallowee ends up as plain text carrying `{{`.
    Re-parsing those strings recovers the content instead of losing it.
    """
    if depth > 3:
        return blocks
    out: list[dict] = []
    for b in blocks:
        if b["t"] == "choice":
            b = dict(b)
            opts = []
            for o in b["options"]:
                label = o["label"]
                if any(x in label for x in SALVAGE_NEEDLES):
                    sub_label, _ = flatten(salvage(parse_blocks(label, audit, 2), audit, 1))
                    label = sub_label or label
                opts.append({"label": label, "blocks": salvage(o["blocks"], audit, depth + 1)})
            b["options"] = opts
            out.append(b)
        elif "text" in b and any(x in b["text"] for x in SALVAGE_NEEDLES):
            audit["salvage"] += 1
            sub = parse_blocks(b["text"], audit, 2)
            if sub:
                out.extend(salvage(sub, audit, depth + 1))
            else:
                out.append(b)
        else:
            out.append(b)
    return out


# ---------------------------------------------------------------- fallback
LINE_DIALOG_RE = re.compile(r"\{\{剧情/对话\|([^|{}\n]*)\|(.*?)\}\}(?=\s*(?:\{\{|\n|$))", re.S)
LINE_BATTLE_RE = re.compile(r"\{\{剧情/战斗提示\|((?:[^{}]|\{\{[^{}]*\}\})*)\}\}")


def fallback_scan(text: str, audit: Counter) -> list[dict]:
    """Last-resort line scanner for pages whose template nesting defeats the parser.

    Merges both dialogue and battle markers by source position so order is kept.
    """
    audit["fallback_scan"] += 1
    hits = []
    for m in LINE_DIALOG_RE.finditer(text):
        hits.append((m.start(), "say", m.group(1).strip(), m.group(2)))
    for m in LINE_BATTLE_RE.finditer(text):
        hits.append((m.start(), "battle", "", m.group(1)))
    hits.sort(key=lambda x: x[0])
    blocks = []
    for _, kind, who, body in hits:
        if kind == "say":
            body = render_inline(body, audit)
            who = render_inline(who, audit) if who else ""
            if body:
                blocks.append({"t": "say", "who": who, "text": body})
        else:
            parts = []
            for part in split_params("x|" + body)[1:]:
                if "=" in part:
                    k, _, v = part.partition("=")
                    if k.strip() in BATTLE_KEYS:
                        v = render_inline(v, audit)
                        if v:
                            parts.append(v)
            if parts:
                blocks.append({"t": "battle", "text": "｜".join(parts)})
    return blocks


# ---------------------------------------------------------------- flatten
def _one_line(text: str) -> str:
    """标记类内容（地点卡、导语、旁白）内部换行压成分隔符。

    不压的话，第 2 行起就没有标记前缀，读者会以为是普通旁白——
    这正是"标记后面的文字和剧情粘在一起"的来源。
    """
    return " ｜ ".join(x.strip() for x in (text or "").split("\n") if x.strip())


def flatten(blocks: list[dict], depth: int = 0) -> tuple[str, int]:
    """把块序列拍成文本。

    选项是"分叉"，不是"接着往下演"：它和它分管的那几行必须成组、
    并和前后旁白隔开，否则读起来就是一团。所以：
      · 选项块前后各留一个空行；
      · 组内用 [选项] 标头 + 缩进的分支行（深度越深缩进越多）。
    """
    parts: list[str] = []
    lines = 0
    indent = "    " * (depth + 1)
    for b in blocks:
        t = b["t"]
        if t == "say":
            parts.append(f"{b['who']}：{b['text']}" if b["who"] else b["text"])
            lines += 1
        elif t == "battle":
            label = {"大标题": "战斗", "提示": "提示", "小标题": "旁白"}.get(b.get("key", ""), "提示")
            parts.append(f"[{label}]" + _one_line(b["text"]))
        elif t == "intro":
            parts.append("[导语]" + _one_line(b["text"]))
        elif t in ("meta", "quote", "extra", "note"):
            parts.append(f"[{b.get('key') or t}]" + _one_line(b["text"]))
        elif t == "choice":
            if parts and parts[-1] != "":
                parts.append("")            # 选项前留白
            for o in b["options"]:
                head = f"[选项]{o['label']}" if o["label"] else "[选项]"
                parts.append(head)
                sub, sub_lines = flatten(o["blocks"], depth + 1)
                if sub:
                    for ln in sub.split("\n"):
                        parts.append((indent + ln) if ln.strip() else ln)
                lines += sub_lines
            parts.append("")                # 选项后留白
    while parts and parts[-1] == "":
        parts.pop()
    return "\n".join(p for p in parts if p is not None), lines


def parse_meta(text: str) -> dict:
    for _, _, name, inner in find_templates(text):
        if name == "剧情":
            out = {}
            for part in split_params(inner)[1:]:
                if "=" in part:
                    k, _, v = part.partition("=")
                    out[k.strip()] = strip_markup(v)
            return out
    return {}


def main() -> int:
    with open(os.path.join(DATA, "stages.json"), encoding="utf-8") as f:
        stages = json.load(f)
    cat_of = {s["title"]: s for s in stages}

    audit: Counter = Counter()
    out_path = os.path.join(DATA, "dialogue.jsonl")
    n = empty = 0
    with open(out_path, "w", encoding="utf-8", newline="\n") as out:
        for fn in sorted(os.listdir(RAW)):
            if not fn.endswith(".txt"):
                continue
            title = fn[:-4]
            text = open(os.path.join(RAW, fn), encoding="utf-8").read()
            meta = parse_meta(text)
            blocks = salvage(parse_blocks(text, audit), audit)
            probe_flat, _ = flatten(blocks)
            if "{{剧情/" in probe_flat or "{{颜色引用" in probe_flat:
                kept = [b for b in blocks if b["t"] in ("intro", "meta")]
                blocks = kept + fallback_scan(text, audit)
            flat, nlines = flatten(blocks)
            row = {
                "title": title,
                "chapter": meta.get("章节", ""),
                "category": meta.get("分类", ""),
                "seq": meta.get("序号", ""),
                "stage_no": meta.get("标题", ""),
                "prev": meta.get("上一关", ""),
                "next": meta.get("下一关", ""),
                "blocks": blocks,
                "text": flat,
                "chars": len(flat),
                "lines": nlines,
            }
            if not row["seq"]:
                row["seq"] = str(cat_of.get(title, {}).get("seq") or "")
            if not flat:
                empty += 1
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1

    with open(os.path.join(DATA, "audit.json"), "w", encoding="utf-8") as f:
        json.dump(audit.most_common(), f, ensure_ascii=False, indent=1)
    print(f"parsed={n} empty={empty}")
    for k, v in audit.most_common(30):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
