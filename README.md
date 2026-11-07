# 战双剧情问答 · PGR Story QA

![问答界面](web/preview.png)

把《战双帕弥什》全部剧情语料收进本地一个数据库，用自然语言提问，回答带可点击的出处和原文。语料 3,692 个关卡、1,113 万字，来自 [战双 BWIKI](https://wiki.biligame.com/zspms/剧情回顾)。

**当前版本：v1.0.0**

## 功能

- **会自己查资料的问答**：不是固定检索管线。模型通过工具自己决定查什么——查角色档案、翻章节目录、读整章原文、检索台词，多轮之后再作答。
- **出处可点击**：每条结论标注【分类／章节／关卡】，点开直接显示该关原文（本地读取，不联网）。
- **有据可依，无据可推**：原文没直接写但有线索时，明确标「推断」并列出依据；连线索都没有才说没有。
- **出处强制校验**：答案里的每个出处都会拿去数据库核对，编号写错自动改正，关卡不存在标为【未核实】。
- **追问接得上**：刷新页面后仍保留本次会话，追问沿用上文。
- **历史自动保存**：每条问答落盘（时间、工具链、出处、token、人民币费用），可导出 Markdown。
- **费用可见**：单次花费按 DeepSeek 官方价目计算，区分缓存命中与高峰/空闲时段。
- 纯 Python 标准库，无第三方依赖；只需一个 DeepSeek API Key。

## 快速开始

### 从源码运行

```bat
git clone https://github.com/Neus117/PGR-Story-QA.git
cd PGR-Story-QA
pip install -r requirements.txt   :: 无需第三方包，此步可跳过
python scripts\bootstrap.py       :: 首次：抓取并建立本地语料库（约 6~8 分钟）
python app.py                     :: 启动，自动打开浏览器
```

浏览器会自动打开 `http://127.0.0.1:8712/`。

### 配置密钥

没有密钥也能用（证据模式：只给原文与出处，不生成成句回答）。要生成回答，把 `config.example.json` 复制成 `config.json` 并填入：

```json
{ "api_key": "sk-...", "base": "https://api.deepseek.com", "model": "deepseek-flash", "timeout": 90 }
```

或使用交互式工具（会校验格式，不把密钥写进命令历史）：

```bat
python scripts\set_key.py
```

`config.json` 已被 `.gitignore` 排除，不会误提交。

## 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--port` | `8712` | 监听端口 |
| `--no-browser` | 关闭 | 启动后不自动打开浏览器 |

## 语料更新

```bat
python scripts\bootstrap.py              :: 全流程：枚举 → 抓取 → 解析 → 建索引
python scripts\bootstrap.py --skip-fetch :: 已有 data\raw 时只重解析并重建索引
```

抓取对站点限速 4 秒/请求，遇到 `HTTP 567` 会按 120/240/480/900 秒冷却重试，响应落盘可断点续跑。
清洗口径见 [CLEANING.md](CLEANING.md)——**新增剧情内容按那份规则跑一遍即可**。

## 自检

| 命令 | 作用 |
|---|---|
| `python scripts\check_robust.py` | 接口健壮性（14 项：非法参数、超长、注入、边界） |
| `python scripts\eval_agent.py` | 27 题固定回归题集，报通过率与花费 |
| `python scripts\verify_parse.py` | 检查解析残留的模板标记 |
| `python scripts\audit.py` | 语料体量与分布审计 |

## 已知限制

- **44 章「流光抛诸寰宇」30 关里 28 关没有正文**，是 wiki 源头未录入，问到最新章会答不出。
- 说话人别名未归一（露西亚／阿尔法／α），用别称提问可能召不回。
- 每次提问的模型调用次数不固定（1~10 次），耗时 4~25 秒、花费约 ¥0.002~0.11。
- 同一问题两次生成内容不同（temperature 0.3），人物小传不是幂等的。
- 费用是按 token 数×官方价目**估算**，不是账单；高峰/空闲判定未内置法定节假日日历（节假日会被高估）。
- 单机单人使用；未做多用户并发压测。

## 实现

- `app.py`：入口，解析参数、打开浏览器、调用 `pgr.server`。
- `pgr/server.py`：本地 `ThreadingHTTPServer`，路由 `/api/ask`、`/api/history`、`/api/stage`、`/api/balance`、`/api/health`。
- `pgr/agent.py`：工具调用循环（`MAX_STEPS=10`、`MAX_READS=6`），出稿前跑 `verify_and_fix` 校验出处。
- `pgr/tools.py`：五个工具（列章节、查角色、读章节、检索台词、站外检索）与本地 SQLite 查询。
- `pgr/parse.py`：wikitext → 结构化对话，规则见 `CLEANING.md`。
- `pgr/index.py`：建 `pgr.db`，含 FTS5 trigram 索引与两字词频表（中文两字词无法走 trigram，退化为 LIKE）。
- `pgr/history.py`：问答历史落盘（独立于 `pgr.db`，因为重建索引会删库）。
- `pgr/pricing.py`：token → 人民币，含缓存命中与高峰/空闲。
- `web/index.html`：内联 CSS/JS，无外部资源；原文浮层、历史折叠、费用展示。

## 目录

```text
PGR-Story-QA/
  app.py                    启动入口
  web/index.html            页面（内联 CSS/JS）
  web/preview.png           界面预览
  pgr/                      语料管道 + 检索 + 服务
  scripts/                  bootstrap / 自检 / 评测
  启动剧情问答.bat            无控制台启动
  config.example.json       配置样例
  CLEANING.md               清洗规则（新增内容按此复用）
  README.md
  data/                     运行数据（已 gitignore，用 bootstrap 重建）
```

## 故障排查

- **页面提示「历史载入失败」**：先看服务端控制台有没有 traceback；`Failed to fetch` 通常意味着后端抛异常断开了连接。
- **提示缺少 `data/pgr.db`**：先跑 `python scripts\bootstrap.py`。
- **模型调用失败**：`python scripts\test_llm.py` 会打印配置路径、密钥来源、实际 base 与 HTTP 返回。
- **双击 `.bat` 没反应**：确认 `pythonw` 在 `PATH` 中，或改用 `python app.py` 看报错。
