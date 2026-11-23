# Daily AI Insight

[![daily](https://github.com/fdchen24/daily-ai-insight/actions/workflows/daily.yml/badge.svg)](https://github.com/fdchen24/daily-ai-insight/actions/workflows/daily.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](requirements.txt)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

每天自动从 arXiv、Hugging Face Papers、GitHub、OpenAlex 发现 AI 领域的优质研究，
**按质量筛选而不是按时间或热度排序**，生成带数值溯源的结构化中文解读，静态发布到
GitHub Pages。全程跑在 GitHub Actions 上，不需要服务器，也不需要数据库。

设计文档见 [DESIGN.md](DESIGN.md)。

## 和"每日 arXiv 推送"的区别

| | 常见做法 | 本项目 |
|---|---|---|
| 选什么 | 当天全部新论文，或按关键词过滤 | 两轮打分后只留当日前 10 篇，综述与经典单独配额 |
| 怎么排 | 时间倒序 / HF 点赞数 | 规则初筛 → L1 批内相对排序（便宜模型）→ 配额与主题多样性重排 |
| 读多少 | 摘要 | 分层取材：摘要 0.5K / 引言结论 5–8K / 全文 20–30K token，并记录 `evidence_level` |
| 可信度 | 直接采信模型输出 | 机械校验：数值必须能在模型实际看到的材料里反查到，链接探活，元数据与 arXiv 原始记录核对 |
| 出什么 | 一段摘要翻译 | 七字段解读（为何值得读 / 动机 / 创新 / 方法 / 结果 / 局限 / 启发）+ `claims` 溯源表 |

三类内容与配额：**前沿 10 篇/日、综述 5 篇/周、经典 5 篇/周**（周配额在成功投放后才记账，
被校验丢弃的条目不占名额）。

## 流水线

| 阶段 | 做什么 | 入口 |
|---|---|---|
| 一 采集 | 四源抓取、规范化、去重，落候选池 | [scripts/collect_all.py](scripts/collect_all.py) |
| 二 规则初筛 | 时效、通道、摘要长度、主题标签 | [scripts/filter.py](scripts/filter.py) |
| 三 L1 排序 | 每批 15 条同上下文排序并给 tier（`deepseek-flash`） | [scripts/rank.py](scripts/rank.py) |
| 四 配额重排 | 日/周配额、主题多样性、冷却期 | [scripts/select.py](scripts/select.py) |
| 五 L2 解读 | 一条目一次调用，同时拿回评分、解读与 claims（`deepseek-flash`, effort=high） | [scripts/summarize.py](scripts/summarize.py) |
| 六 机械校验 | 数值反查、链接探活、元数据核对、重复解读检测 | [scripts/validate.py](scripts/validate.py) |
| 七 落盘归档 | 当期 JSON、归档索引、已推荐台账、metrics | [scripts/archive.py](scripts/archive.py) |

阶段二至七由 [scripts/run_daily.py](scripts/run_daily.py) 串起来；采集单独一个入口，
因为它受网络与配额影响最大，重跑它不该连带重算 LLM。

CI 里这两个入口也是两个 job（`collect` → `daily`，候选池用 artifact 传递），各自独立
计时：首次实测采集 59 分钟、解读 20 分钟，放在同一个 job 里一旦超时，已付的 token
就全作废。

## 本地跑通

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                      # 填 DEEPSEEK_API_KEY，该文件已被 gitignore

# 阶段一：采集（无 GitHub token 时加 --no-baseline）
.venv/bin/python -m scripts.collect_all --no-baseline

# 阶段二至四：不花钱的试跑，只看选出哪些条目
.venv/bin/python -m scripts.run_daily --dry-run

# 全量：解读 + 校验 + 落盘（--limit 3 可先小样本验证）
.venv/bin/python -m scripts.run_daily
```

常用参数：

| 参数 | 入口 | 作用 |
|---|---|---|
| `--date 2026-11-23` | 两者 | 重跑指定 arXiv 公告批次日（按批次日幂等覆盖） |
| `--from-raw` | collect_all | 不联网，用 `data/raw/<批次>/` 已存响应重跑规范化与去重 |
| `--lookback 1` | collect_all | 覆盖各源的回溯天数 |
| `--no-github` / `--no-baseline` / `--no-citations` | collect_all | 跳过对应数据源 |
| `--dry-run` | run_daily | 只跑到配额重排，不调用 L2，不落盘 |
| `--limit 3` | run_daily | 最多解读几条，试运行省钱 |
| `--log-level DEBUG` | 两者 | 日志级别 |

改了主题词表、初筛阈值或去重参数之后，用 `--from-raw` 重建候选池：这些规则都在
阶段一的规范化里生效，不重建的话新标签进不了后面的阶段，而重新抓一遍一千篇论文
既慢又会撞上 arXiv 的接口超时。

产物默认写入 `./data`（已 gitignore）。设 `DAI_DATA_DIR` 可改写入位置，CI 用它指向
`data` 分支的工作区。

## 配置：Secrets 与 Variables

密钥存成仓库 **Secrets**（存进去就读不出来，日志里自动打码），行为开关存成仓库
**Variables**（明文可见、可随时改，不必改 YAML 也不进代码历史）。两者都在
`Settings → Secrets and variables → Actions`。

**网页步骤**

1. 打开仓库 `Settings` → 左栏 `Secrets and variables` → `Actions`。
2. `Secrets` 标签页 → `New repository secret` → Name 填 `DEEPSEEK_API_KEY`，
   Secret 填 key 本体 → `Add secret`。可选再加 `S2_API_KEY`。
3. `Variables` 标签页 → `New repository variable`，按下表按需添加；不填的保持缺省。
4. `Actions` → `daily` → `Run workflow` 手动触发一次，确认日志里没有
   "缺少 DEEPSEEK_API_KEY"。

**命令行步骤**（需要 [gh CLI](https://cli.github.com/)，`brew install gh && gh auth login`）

```bash
# Secrets：从文件读，避免 key 进入 shell 历史
gh secret set DEEPSEEK_API_KEY --repo fdchen24/daily-ai-insight < key.txt

# Variables
gh variable set DAI_SUMMARIZE_LIMIT --repo fdchen24/daily-ai-insight --body 12
gh variable set OPENALEX_MAILTO --repo fdchen24/daily-ai-insight --body you@example.com

gh secret list --repo fdchen24/daily-ai-insight    # 只列名字与更新时间
gh variable list --repo fdchen24/daily-ai-insight
```

**Secrets**

| 名称 | 必需 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 是 | L1 排序与 L2 解读 |
| `S2_API_KEY` | 否 | 只补 `tldr` 与 influential 引用数，引用数以 OpenAlex 为准 |
| `GITHUB_TOKEN` | — | 不用手动加，Actions 内置的那个已够（Search 30/min、Core 5000/hr） |

**Variables**

| 名称 | 缺省 | 作用 |
|---|---|---|
| `DAI_LOOKBACK` | 配置文件值 | 各源回溯天数 |
| `DAI_SUMMARIZE_LIMIT` | 不限（受配额约束） | 每期最多解读几条，临时压成本用 |
| `DAI_DRY_RUN` | `false` | 置 `true` 时只跑到配额重排，不调用 L2 |
| `DAI_SKIP_GITHUB` / `DAI_SKIP_BASELINE` / `DAI_SKIP_CITATIONS` | `false` | 跳过对应数据源 |
| `DAI_LOG_LEVEL` | `INFO` | 日志级别 |
| `OPENALEX_MAILTO` | 配置文件里的地址 | OpenAlex polite pool 身份，fork 后建议换成自己的 |

布尔值接受 `true/1/yes/on` 与 `false/0/no/off`，填别的词会按缺省处理并留一条日志。

取值优先级：**环境变量（含 Actions 的 Secrets/Variables）> 本地 `.env` > 配置文件默认值**，
命令行参数显式给出时优先于环境变量。本地开发把同名变量写进 `.env` 即可，
`.env` 与 `.env.example` 都在 [.gitignore](.gitignore) 里——模板文件一起忽略，
是因为本地常有人直接把真实 key 填进模板，而这个仓库要公开发布。

## 分支与目录

`main` 只放代码：成品、引用缓存与日志每天都变，混进代码历史会让 clone 体积逐日膨胀。
数据存在孤立分支 `data`，由 [daily.yml](.github/workflows/daily.yml) 首次运行自动创建，
CI 把它检出到 `.data-branch/` 并用 `DAI_DATA_DIR` 指过去。

```
config/           # 数据源、评分权重、主题词表、经典清单
  prompts/        # L1 排序与 L2 解读的四套模板（frontier/survey/classic/github）
scripts/
  collect/        # arxiv / huggingface / github / openalex / semanticscholar
  normalize.py    # 四源合并、主题标注、通道判定
  deduplicate.py  # DOI → 标题 → 项目三轮去重
  filter.py rank.py select.py summarize.py validate.py archive.py
  material.py     # 分层取材与 PDF 文本缓存
  llm.py          # DeepSeek 客户端、token 账本、预算硬闸门
  common.py       # 配置加载、HTTP 客户端、路径与时间工具
data/             # 运行产物（gitignore）：raw/ candidates/ daily/ archive/ state/ metrics/
```

## 成本与可观测

每次运行把真实用量写进 `data/metrics/YYYY-MM.json` 的当日行：按模型的
`calls / tokens_in / tokens_out / cache_hit_tokens`、各阶段统计、校验结果、
耗时与状态。采集入口与解读入口写同一个日期键的不同字段，互不覆盖。

成本折算要先在 [config/scoring.yaml](config/scoring.yaml) 的 `pricing` 段填入实测单价；
没填时 metrics 里的 `cost.amount` 如实写 `null` 并注明原因，不预填一个猜的数字。

两道成本闸门：`summarize.budget` 是单次运行的 token 硬上限，超出即停止并保留已完成
部分（减量投放，不降门槛）；解读缓存按 `content_hash + 模板 + 取材层级 + 模型` 命中，
重跑同一批次不重复付费。

## 实施阶段

- [x] 阶段一 采集与候选池
- [x] 阶段二 筛选、排序、解读、校验、落盘
- [ ] 阶段三 前端与 GitHub Pages（需先装 Node.js）
- [ ] 阶段四 PWA 与移动端
- [ ] 阶段五 稳定性与成本调优

验收清单见 [DESIGN.md](DESIGN.md) 第十一节。

## 致谢与许可

形式上参考了 [daily-arXiv-ai-enhanced](https://github.com/fdchen24/daily-arXiv-ai-enhanced)
的"GitHub Actions + 静态站点"思路，内容侧的筛选、分层取材与机械校验是本项目自己的设计。

代码以 MIT 许可发布。论文元数据与摘要版权属于原作者与 arXiv，站内只做引用与解读，
不再分发 PDF 全文。

