# Daily AI Insight

[![daily](https://github.com/fdchen24/daily-ai-insight/actions/workflows/daily.yml/badge.svg)](https://github.com/fdchen24/daily-ai-insight/actions/workflows/daily.yml)
[![pages](https://github.com/fdchen24/daily-ai-insight/actions/workflows/pages.yml/badge.svg)](https://github.com/fdchen24/daily-ai-insight/actions/workflows/pages.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](requirements.txt)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

每天自动从 arXiv、Hugging Face Papers、GitHub、OpenAlex 发现 AI 领域的优质研究，
**按质量筛选而不是按时间或热度排序**，生成带数值溯源的结构化中文解读，静态发布到
GitHub Pages。全程跑在 GitHub Actions 上，不需要服务器，也不需要数据库。

**站点**：<https://fdchen24.github.io/daily-ai-insight/> · 设计文档见 [DESIGN.md](DESIGN.md)。

## 和"每日 arXiv 推送"的区别

| | 常见做法 | 本项目 |
|---|---|---|
| 选什么 | 当天全部新论文，或按关键词过滤 | 两轮打分后只留当日前 10 篇，综述与经典单独配额 |
| 怎么排 | 时间倒序 / HF 点赞数 | 规则初筛 → L1 批内相对排序（便宜模型）→ 配额与主题多样性重排 |
| 读多少 | 摘要 | 分层取材：摘要 0.5K / 引言结论 5–8K / 全文 20–30K token，并记录 `evidence_level` |
| 可信度 | 直接采信模型输出 | 机械校验：数值必须能在模型实际看到的材料里反查到，链接探活，元数据与 arXiv 原始记录核对 |
| 出什么 | 一段摘要翻译 | 六段解读（TL;DR / 任务与问题 / 问题根因 / 方法设计 / 实验结果 / 局限），作者单位照论文首页抄录，数值另登记 `claims` 溯源表 |

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

## 前端与发布

React 19 + Vite + Tailwind 4 + vite-plugin-pwa，没有后端：页面直接 fetch
`data/daily/latest.json`、`data/daily/<年>/<月>/<日>.json` 与 `data/archive/index-<年>.json`，
归档索引按年分片正是为了让首页不下载全部历史。

| 页面 | 内容 |
|---|---|
| 今日 `/` | 当期条目卡片，按综合得分排序 |
| 发现 `/discover` | 按推荐类型与主题筛选最近若干期 |
| 归档 `/archive` | 按年索引翻历史批次 |
| 收藏 `/saved` | 收藏与已读台账，可导出/导入 JSON 备份 |
| 详情 `/p/<批次日>/<条目 id>` | TL;DR、作者与单位、引用数、六段解读、链接与校验结论 |

四个前端侧的实现决定：

- **公式用 KaTeX 渲染**，prompt 要求解读里的符号一律写成 `$...$`；SW 把 19 个
  KaTeX woff2（共 256 KiB）一起预缓存，离线时公式不会退化成纯文本。
- **已读分自动与手动两条路**。详情页停留满 8 秒或滚过半页自动记一次，标题下的
  按钮可以手动补记或撤回——进页面瞬间就记会让"未读"失去意义，只给按钮则没人点。
- **收藏连标题一起存快照**。只存 id 的话，当期 JSON 被 SW 淘汰后收藏页就只剩一串
  id。收藏、已读与阅读进度都在本机 IndexedDB，换设备靠导出备份：iOS 对未安装到
  主屏的站点，7 天无访问就会清掉 script-writable storage。
- **静态资源 CacheFirst，数据 JSON NetworkFirst**（保留近 40 个文件，够覆盖最近
  7 期离线阅读）。预缓存的 glob 刻意不含 json，否则当期内容被 SW 接管，用户刷新
  也看不到新一期。

```bash
cd web && npm install
npm run dev        # http://localhost:5173/daily-ai-insight/，data/ 由中间件直读仓库目录
npm run build      # tsc --noEmit + vite build + 拷 daily/ 与 archive/ 进 dist/data/
npm run typecheck
```

发布由 [pages.yml](.github/workflows/pages.yml) 做：daily 跑完、或 `web/` 有代码改动
推到 main 时各触发一次，检出 `data` 分支取成品 JSON，构建后部署到 Pages。

**首次使用要先在网页上开站**：仓库 `Settings` → `Pages` → `Build and deployment` →
Source 选 `GitHub Actions`，然后 `Actions` → `pages` → `Run workflow` 补发一次。
内置 `GITHUB_TOKEN` 没有建站权限（创建 Pages 站点要 admin），没开站时工作流会在
`configure-pages` 一步失败。

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
web/
  src/
    pages/        # 今日 / 发现 / 归档 / 收藏 / 详情
    components/   # 条目卡片、解读分段、KaTeX 渲染、公用小件
    lib/          # 数据读取、各模板字段表、IndexedDB 本机状态、主题
  scripts/        # postbuild：404 回退 + 把 daily/ 与 archive/ 拷进 dist
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

定时任务挂在 `30 16 * * *` UTC（北京次日 00:30），避开 DeepSeek 工作日
01:00–04:00 与 06:00–10:00 UTC 的 peak 时段——同一批活在 peak 下单价翻倍，
实测 2.76 CNY/天。当期因此在北京时间凌晨上线，早上读。

## 实施阶段

- [x] 阶段一 采集与候选池
- [x] 阶段二 筛选、排序、解读、校验、落盘
- [x] 阶段三 前端与 GitHub Pages
- [x] 阶段四 PWA 与移动端（Android Chrome 实测通过；iPhone Safari 待测）
- [ ] 阶段五 稳定性与成本调优（进行中）

验收清单见 [DESIGN.md](DESIGN.md) 第十一节。

## 致谢与许可

形式上参考了 [daily-arXiv-ai-enhanced](https://github.com/fdchen24/daily-arXiv-ai-enhanced)
的"GitHub Actions + 静态站点"思路，内容侧的筛选、分层取材与机械校验是本项目自己的设计。

代码以 MIT 许可发布。论文元数据与摘要版权属于原作者与 arXiv，站内只做引用与解读，
不再分发 PDF 全文。

