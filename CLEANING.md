# 剧情文本清洗规则

这份文档固定 `pgr/parse.py` 的清洗口径。**新增剧情内容后，按这里的规则跑一遍即可得到一致结果**；改规则必须同时改这份文档。

## 数据流向

```text
data/raw/<关卡名>.txt        wiki 页面 wikitext 原件（一句话一份）
      ↓ pgr/parse.py         解析 + 清洗
data/dialogue.jsonl          每行一个关卡：{title, chapter, category, seq, blocks, text, chars}
      ↓ pgr/index.py         建 SQLite + FTS5
data/pgr.db                  检索库（stages / lines / speakers / bigrams / fts）
```

## 规则表

| # | 问题 | 处理 | 位置 |
|---|---|---|---|
| 1 | 模板嵌套（`{{灰色\|{{剧情/对话\|…}}}}`） | **内层先展开**，再处理外层 | `render_inline` |
| 2 | 包裹型模板（颜色、黑幕、引用、设定、特效字、剧情/居右） | 块级递归解析其载荷，保留结构与文字 | `parse_blocks` / `BLOCK_WRAPPERS` |
| 3 | 未知模板 | 提取可读文字 + **记进 `data/audit.json`**，不静默丢弃 | `eval_inline` |
| 4 | 台词 `{{剧情/对话\|角色\|台词}}` | 角色为空 = 旁白 | `parse_blocks` |
| 5 | 选项文字与紧接着的「指挥官说出这句」重复（占 78%） | **删掉重复的那句台词**，保留 `[选项]` 标记（分支信息） | `parse_blocks` choice 分支 |
| 6 | 选项与其分支、前后旁白平铺在一起 | 选项块**前后各留空行**；分支行缩进 `4 空格 × 深度` | `flatten` |
| 7 | `{{剧情/战斗提示}}` 三参数语义不同 | `大标题→[战斗]`、`提示→[提示]`、`小标题→[旁白]` | `parse_blocks` / `flatten` |
| 8 | 标记类内容多行（地点卡「巴德尔号／大西洋海域」） | 内部换行压成一行，用 ` ｜ ` 分隔 | `_one_line` |
| 9 | `{{引言框}}` 两种用途 | 含「通关目标」的丢弃，其余作为引用块保留 | `parse_blocks` |
| 10 | 媒体/导航类 | 丢弃：图片查看按钮、视频引用、B站视频、插入视频、剧情一览、Swiper | `INLINE_DROP` / `BLOCK_SKIP` |
| 11 | HTML 注释 | 清除 | `strip_markup` |
| 12 | 行内标记 | 颜色引用/黑幕/特效字→取正文；修正→取改后字；标注→`中文（原文）`；设定→取末参数；引用→丢弃（编辑注） | `eval_inline` |
| 13 | 源数据花括号错位导致整段被吞 | **打捞**：残留 `{{剧情/` 的块重新解析；仍不行则用行级正则重扫该页 | `salvage` / `fallback_scan` |
| 14 | `{{=}}` / `{{!}}` 转义 | 还原成 `=` / `\|`。不还原会让 `<div class{{=}}"x">` 变成不完整标签而漏进正文 | `eval_inline` |
| 15 | 标签被换行拆到两个块里 | 产出前对**拍平后的整段**再跑一次 `scrub()`：删标签碎片、残留模板/链接 | `scrub` |

## 残留门禁（新增内容后必须过）

```bash
python scripts/check_markup.py          # 有残留退出码 1
python scripts/check_markup.py --soft    # 只报告
```

判据是「读者会看到的东西里不该出现的字符」：`class=`、`style=`、`{{`/`}}`、`[[`/`]]`、
`{|`/`|}`、已知 HTML 标签、HTML 实体、`NNpx`。

**不判失败**的：`----` 横线（剧情里的伪造终端会画分隔线）、`<homo_sapiens>`/`<end_loop>`
这类伪造代码——它们是内容。

页面上的「更新语料」跑完会自动执行同一套检查，结果写进更新摘要；
残留不为 0 时 `logs/app.log` 记一条 ERROR。

## 新增内容后怎么复用

```bash
python scripts/bootstrap.py --skip-fetch   # 已有 raw 时：只重新解析 + 建索引（约 1 分钟）
python scripts/bootstrap.py                # 连抓取一起（约 6~8 分钟）
```

- **出现新模板**：在 `parse.py` 的 `eval_inline`（行内）或 `parse_blocks`（块级）里注册分支；
  没注册的会落到 `unknown:*` 并写进 `data/audit.json`，不会静默丢失。
- **验收**：`python scripts/verify_parse.py` 检查残留标记；`data/audit.json` 里不应出现大量 `unknown:`。

## 已知未处理（明确不做的）

- **44 章「流光抛诸寰宇」30 关里 28 关没有正文**——wiki 源头就没录，不是清洗问题。
- 说话人别名未归一（露西亚／阿尔法／α）。
- `|提示=` 参数里混着「地点卡」和「行动目标」，目前统一显示 `[提示]`，没有再细分。
- 同一选项在不同分支重复出现属于真实游戏结构，不删。
