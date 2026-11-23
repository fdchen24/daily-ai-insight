# Daily AI Insight 设计方案

> Version 2.0 · 2026-11-23
> 项目类型：自动化 AI 研究内容推荐与阅读平台
> 架构：GitHub Actions + Python + DeepSeek API + React + GitHub Pages + PWA

---

## 0. v1.0 → v2.0 变更摘要

| # | 问题 | v1.0 | v2.0 |
|---|---|---|---|
| 1 | 配额与供给不匹配 | 每日 6/2/2 | 前沿 10 篇/日；综述 5 篇/周；经典 5 篇/周 |
| 2 | arXiv 时间语义未定义 | 只列字段 | `published` 定前沿窗口，`updated` 走重大修订通道，ID 剥版本号 |
| 3 | 调度与 arXiv 公告批次错位 | 06:00 北京时间 | 10:30 北京时间（`30 2 * * *` UTC），显式记录公告批次日 |
| 4 | arXiv 采集量偏低 | 200–400 | 600–1200（五个主分类工作日真实新增量级） |
| 5 | GitHub 增量冷启动悖论 | 自攒快照 | trending 页解析 + 自攒快照双通道 |
| 6 | LLM 成本低估 | 无金额 | L2 与解读合并单次调用 + prefix cache + 分级取材，token 落盘 |
| 7 | 事实校验空心 | "幻觉检查" | 数值溯源 `source_span` + 原文字符串反查，机械可检 |
| 8 | 缺客观质量信号 | 纯 LLM 打分 | 接入 OpenAlex 引用数据（无需 key，带持久缓存） |
| 9 | LLM 打分不可比 | 独立打 1–10 分 | 批内相对排序 + rubric 锚点 |
| 10 | PWA/Pages 工程坑 | 未提 | base path、SW 分策略缓存、404 回退、iOS 存储清除 |
| 11 | git 仓库膨胀 | 单文件全量重写 | 按月分片 + 滑窗裁剪 |
| 12 | 幂等与时区 | 未定义 | 以公告批次日为主键，JSON 全量 ISO8601 带时区 |
| 13 | 合规 | 未提 | 只存元数据 + 自产解读，不整段复制原文 |

### 已确认的三项决策

1. **独立 GitHub 仓库** `daily-ai-insight`，不混入 research 仓库。
2. **Star 增长**：抓 `github.com/trending` 解决冷启动 + 每日快照积累真实增量。
3. **配额**：前沿每日 10 篇，综述每周 5 篇，经典每周 5 篇。

---

## 一、项目定位与目标

Daily AI Insight 是基于 GitHub Actions 与 GitHub Pages 的自动化 AI 研究内容精选平台，参考 [daily-arXiv-ai-enhanced](https://fdchen24.github.io/daily-arXiv-ai-enhanced/) 的技术路线，扩展为集前沿论文、深度综述、经典研究与热门开源项目于一体的每日阅读站点。

### 1.1 核心目标

1. **自动发现**：每天从 arXiv、Hugging Face Papers、GitHub、OpenAlex 获取 AI 领域优质内容。
2. **质量优先**：综合研究价值、创新性、证据质量和影响力筛选，而非按时间或热度排序。
3. **深入解读**：生成结构化中文解读，突出动机、方法、证据与启发。
4. **知识积累**：前沿、综述、经典三轨并行，避免推荐被近期热点完全占据。
5. **低成本运行**：无常驻服务器、无数据库，可变成本只有 LLM 调用。
6. **移动端优先**：手机浏览、PWA 安装、收藏与历史阅读。

### 1.2 产品要回答的三个问题

- **为什么值得读？** 研究解决了什么重要问题？
- **它究竟讲了什么？** 核心发现、方法和证据是什么？
- **能获得什么？** 对研究方向或实际工作有什么启发？

---

## 二、内容组成与配额

### 2.1 为什么不用固定日比例

前沿类是**流量型**供给：五个主分类每工作日新增 500–900 篇，取之不尽。
综述与经典是**存量型**供给：高质量 AI Survey 一年约百余篇量级，真正的经典论文全领域数百篇量级。

v1.0 的每日 2+2 等于一年消耗 730 篇综述 + 730 篇经典，会在 2–5 个月内耗尽池子并被迫降质。因此存量型内容改为**周配额**。

### 2.2 配额表

| 推荐类型 | 配额 | 时间范围 | 内容重点 |
|---|---|---|---|
| 前沿研究与新文章 | **10 篇 / 天** | 主窗口 7 天，补捞窗口 30 天 | 重要新发现、前沿突破、新兴方向 |
| 深度分析与优质综述 | **5 篇 / 周** | 最近 1–12 个月 | 系统性研究、技术综述、趋势分析 |
| 经典重读 | **5 篇 / 周** | 不限 | 基础理论、开创性工作、长期价值 |

周配额的日分布：综述与经典各在周一至周五每天投放 1 篇，周末只出前沿。

| | 周一–周五 | 周六–周日 |
|---|---:|---:|
| 前沿 | 10 | 10 |
| 综述 | 1 | 0 |
| 经典 | 1 | 0 |
| **合计** | **12** | **10** |

周配额用**周预算账本**实现（`data/state/week-budget.json`），而不是硬编码星期：记录本周（ISO 周）已投放的综述/经典数量，每日可投放量 = 剩余配额 / 剩余天数（向上取整，上限 2）。某天没有合格候选时不补位，配额顺延到本周后续几天；到周日仍未用完则作废，不跨周累积。

### 2.3 推荐原则

- 三类内容各自维护候选池，采用各自的评价标准。
- 保持研究方向多样性，避免连续推荐高度相似内容。
- 不强制填充低质量内容：宁可当天少一篇。
- 同一研究成果的论文与官方代码仓库合并为一条推荐。
- 经典论文冷却 180 天后可复推，但必须更换解读角度（结合其后续工作），不复用旧解读。

---

## 三、数据来源设计

四个来源，分工明确：arXiv 供内容，Hugging Face 供社区热度信号，GitHub 供开源项目与代码，OpenAlex 供客观学术影响力信号。

### 3.1 arXiv：核心论文来源

| arXiv 分类 | 领域 | 优先级 |
|---|---|---|
| `cs.AI` | Artificial Intelligence | 高 |
| `cs.CV` | Computer Vision | 高 |
| `cs.CL` | Computation and Language | 高 |
| `cs.LG` | Machine Learning | 高 |
| `stat.ML` | Statistics / ML | 高 |
| `cs.RO` | Robotics | 中 |
| `cs.MA` | Multi-Agent Systems | 中 |
| `cs.IR` | Information Retrieval | 中 |
| `cs.NE` | Neural & Evolutionary Computing | 中 |

#### 采集方式与限速

- 用 **OAI-PMH**（`http://export.arxiv.org/oai2`）按 `from`/`until` 日期范围增量拉取，而不是 `search_query` + `sortBy=submittedDate`。后者在深分页时存在重复和缺页问题，不适合每天上千条的全量采集。
- Search API 仅用于补充查询（如按关键词补捞）。
- 请求间隔 ≥ 3 秒，分页 100–200 条，失败指数退避重试 3 次。
- 单日采集目标 **600–1200 篇**（去重后）。
- **跳过周六周日**：arXiv 这两天不公告，OAI datestamp 为 0 条（实测 2026-11-21 周六、11-22 周日均 0 条，而 11-20 周五 1078 条、11-23 周一 910 条）。回溯窗口内的周末日直接从请求列表里剔除，记入 `stats.skipped_weekends`。

#### 时间字段语义（v1.0 的主要缺陷）

`published` = v1 提交时间；`updated` = 最新版本时间。两者混用会导致老论文被当成新论文。

规则：

1. **前沿主通道**：`published` 落在最近 7 天内。
2. **补捞通道**：`published` 落在 8–30 天内，且有新的强信号（HF upvotes 增长、OpenAlex 引用启动、官方代码刚发布）。用于捞回首发时被低估的论文。
3. **重大修订通道**：`published` 超过 30 天，但 `updated` 在 7 天内且版本号跨越 ≥ 2（如 v1 → v3）。前端必须标注"修订版"及版本号，不能伪装成新论文。
4. 去重与 ID 存储**一律剥离版本后缀**：`2510.18927v2` → `2510.18927`。

#### 二级研究主题

arXiv 官方分类太粗（一篇视频生成论文可能同时属于 cs.CV / cs.AI / cs.LG），因此额外维护内部主题标签，一篇论文可带多个：

`LLM 与推理` · `强化学习与后训练` · `图像/视频/多模态生成` · `Agent 与工具使用` · `World Models 与具身智能` · `扩散模型与 Flow Matching` · `架构/训练/推理加速` · `AI 安全与评估`

主题判定分两步：先用 `config/topics.yaml` 的关键词规则命中（零成本、可解释），规则无法判定时才交 L1 模型兜底。数据里用 `topics_source` 字段记录来源，便于后续调规则。

#### 采集字段

`arxiv_id`（无版本）· `arxiv_version` · `title` · `authors` · `abstract` · `published`（ISO8601+TZ）· `updated` · `primary_category` · `categories` · `pdf_url` · `doi` · `comment`（常含会议信息与代码链接）· `journal_ref`

### 3.2 Hugging Face Papers：社区热度信号

端点：`https://huggingface.co/api/daily_papers?date=YYYY-MM-DD`

**这是未文档化的非官方端点**，`huggingface_hub` 库并未封装 Daily Papers。因此必须：

- 显式标注为非官方依赖，单独封装在 `scripts/collect/huggingface.py`。
- 失败时**只跳过 HF 热度信号，不让整条流水线失败**（故障隔离）。
- 同时解析 HTML 页面作为备用通道。

#### 采集字段

`paper_id` / `arxiv_id` · `title` · `summary` · `published_at` · `upvotes` · `num_comments` · `is_trending` · `project_page` · `github_url`

#### 定位

HF 提供的是**发现信号**，不是质量结论。平台分别维护两个独立分数：

- `quality.score`：内容质量（LLM + 规则，决定是否推荐）
- `signals.community`：社区热度（upvotes 等，仅参与候选召回与排序加权）

HF 论文必须按 arXiv ID 与 arXiv 条目合并，不得重复采集与重复解读。

### 3.3 GitHub：热门 AI 开源项目

#### 冷启动悖论与解法

v1.0 计划"保存每日 Star 快照 → 计算增量"，但存在鸡生蛋问题：**只能给已跟踪的仓库算增量，而刚爆火的新仓库第一次出现时恰好没有历史快照**。同时 `created:>` 过滤会严重偏向新建刷 star 的营销型仓库——真正的热门 AI 项目大多是存量项目突然增长。

双通道解法：

| 通道 | 作用 | 实现 |
|---|---|---|
| A. trending 页解析 | 解决冷启动，直接拿到"正在爆火"信号 | 解析 `github.com/trending?since=daily/weekly&spoken_language_code=`，按语言与关键词过滤 AI 项目 |
| B. 自攒每日快照 | 提供真实 7d/30d 增量数值 | 对基线池每日记录 `stargazers_count`，积累后算增量 |

基线池构建（绕过 Search API 的 1000 条返回上限）：按 star 区间分段查询（`stars:100..500`、`500..2000`、`2000..10000`、`>10000`）× AI 关键词 × `pushed:>`，合并去重，目标维持 2000–3000 个仓库。

#### API 限制（必须在实现中遵守）

- Search API：认证后 **30 请求/分钟**，且**单次搜索最多返回 1000 条**（10 页 × 100）。
- Core API：认证后 5000 请求/小时。
- 所有请求带 `Authorization: Bearer $GITHUB_TOKEN`（Actions 内置 token 即可）。

#### 评价指标

| 指标 | 作用 |
|---|---|
| Total Stars | 累计关注度 |
| Stars Growth 7d / 30d | 短期与中期增长（需快照，不能由总数推断） |
| Trending 命中 | 爆发信号（通道 A） |
| Recent Commits / 活跃度 | 是否在真实维护 |
| README 质量 | 文档完整性 |
| Examples / Weights | 可复现与可用程度 |
| Related Paper | 研究依据 |
| License | 开源使用条件 |

#### 过滤策略

过滤掉：资源清单与链接合集（`awesome-*` 类）、无实质创新的重复封装、营销型仓库、缺文档或无有效实现的项目、与 AI 研究无关的热门仓库。

论文对应的官方代码仓库**合并到论文条目**，不独立占用推荐名额。

### 3.4 OpenAlex：客观学术影响力（v2.0 主源，无需 key）

端点：`https://api.openalex.org/works`。User-Agent 里带 `mailto:` 即进入 polite pool，额度 10 万次/天、10 次/秒，**不需要 API key**。

arXiv 自 2022 年起给每篇论文分配 DataCite DOI（`10.48550/arxiv.<id>`），因此可用 `filter=doi:a|b|c` 一次查 50 篇，2000 篇候选只需 41 次请求、约 10 秒。

取用字段：`cited_by_count` · `referenced_works_count` · `publication_year` · `type`（preprint / conference-paper / article，用于区分"仍是预印本"与"已发表"）· `primary_location.source`（期刊/会议名）· `counts_by_year`（引用速度）· `fwci`（领域归一化影响因子）· `citation_normalized_percentile` · `topics`

三个用途：

1. **经典与综述的客观锚点**：引用数、fwci、百分位是零 LLM 成本的硬信号，比 LLM 主观打分可靠得多。
2. **经典池半自动扩充**：按领域取高被引论文作为候选，提交人工确认后入池，避免纯手工维护数百篇。
3. **补捞信号**：新论文引用数启动快，是前沿补捞通道的触发条件之一。

**持久缓存**：结果写入 `data/state/citations/<论文年月>.json`，近期论文 TTL 7 天、老论文 30 天。相邻两天的回溯窗口约 75% ID 重复，缓存后稳定态每天只剩十几次请求。

**未入库不算失败**：任何引用源对当天公告的 arXiv 论文都有数天索引延迟（实测 0–2 天龄命中 0%，3–30 天龄 100%）。因此 `hit_rate` 以"已入库口径"计，未入库的条目只是缺该项信号。

#### Semantic Scholar：可选增强

S2 仅在配置了 `S2_API_KEY` 时启用，只补 OpenAlex 没有的两项：`tldr` 与 `influentialCitationCount`。引用数一律以 OpenAlex 为准，所以**没有 key 不影响任何验收项**。

无 key 时 S2 限速约 100 请求/5 分钟，同一批候选实测耗时 30 分钟以上（OpenAlex 为 10 秒），故不作为必要依赖。

注意：Papers with Code 已关停，设计中不依赖该来源。

### 3.5 深度综述与经典研究来源

**综述**来自 arXiv 中标题/摘要命中 survey/review/overview 模式且满足篇幅与引用数门槛的论文，以及高质量技术报告、研究机构博客、重要开源项目的技术文档。

**经典**来自 `config/classics.yaml`，初期人工维护 150 篇起步，通过 OpenAlex 半自动扩充到 400+（按 5 篇/周 = 260 篇/年消耗，400+ 池子配 180 天冷却可长期运转）。


每篇经典记录：研究领域 · 核心贡献 · 重要性 · 技术难度 · 推荐历史（日期列表）· 关联后续研究。

---

## 四、每日自动化流程

```text
arXiv (OAI-PMH)   HF daily_papers   GitHub trending+API      OpenAlex      classics.yaml
       │                 │                   │                    │                │
       └─────────────────┴───────────────────┴────────────────────┴────────────────┘
                                     ▼
                      1. 采集（故障隔离，单源失败不中断）
                                     ▼
                      2. 规范化与跨源去重合并
                                     ▼
                      3. 规则初筛（零 LLM 成本）→ 100–150 条
                                     ▼
                      4. L1 批内相对排序（deepseek-flash）→ 30–40 条
                                     ▼
                      5. 配额与多样性重排（周预算账本）
                                     ▼
                      6. L2 深度分析 + 中文解读（deepseek-flash，单次合并调用）
                                     ▼
                      7. 机械校验（链接/元数据/数值溯源）
                                     ▼
              data/daily/YYYY/MM/DD.json + latest.json + metrics
                                     ▼
                         GitHub Pages 静态发布
```

### 4.1 调度时间（v1.0 的时区缺陷）

arXiv 新论文在**美东时间 20:00** 公告。v1.0 定的北京时间 06:00 运行，拿到的是前一天的批次，且该偏移随美东夏令时每年漂移 1 小时，会在批次边界造成系统性重复或遗漏。

修正：

- cron 一律写 UTC：`30 2 * * *`（北京时间 10:30），稳定落在公告之后。
- 数据中显式记录 `arxiv_announce_date`（本期对应的 arXiv 公告批次日），**不用运行日期推断批次**。
- `arxiv_announce_date` 作为当期主键：同一天手动重跑**覆盖**该批次，不追加。

| UTC | 北京时间 | 任务 |
|---|---|---|
| 02:30 | 10:30 | Actions 触发，多源采集 |
| 02:45 | 10:45 | 规范化、去重、规则初筛 |
| 02:55 | 10:55 | L1 相对排序 |
| 03:05 | 11:05 | 配额与多样性重排 |
| 03:10 | 11:10 | L2 深度分析 + 中文解读 |
| 03:40 | 11:40 | 机械校验与归档 |
| 03:50 | 11:50 | 构建并部署 Pages |

以上为目标时长，非保证。Actions 的 cron 常有 10–30 分钟延迟。

### 4.2 阶段二：规范化与跨源去重

统一内容实体：

```text
ResearchItem
├── paper            arXiv 元数据 / DOI
├── signals
│   ├── community    HF upvotes、GitHub stars
│   └── academic     OpenAlex 引用 / fwci / 百分位
├── repo             GitHub 仓库信息
└── insight          quality.score / topics / 中文解读
```

去重键按优先级：`arxiv_id`（剥版本）→ `doi` → 标题归一化相似度 → `project_url` → `repo_full_name`。

**标题相似度的具体算法**（v1.0 只写"相似度"，无法实现）：

1. 归一化：转小写、去标点、折叠空白、去停用词（a/an/the/of/for/with/on/in）、去 LaTeX 标记。
2. 计算归一化后的 token set Jaccard 相似度。
3. 阈值 **≥ 0.90** 判为同一论文；0.75–0.90 进入人工复核日志（不自动合并）。

命中去重时**合并信息**（取并集：多个 source、补齐缺失字段、取最新信号值），而不是丢弃或重复生成解读。

### 4.3 阶段三：规则初筛（零 LLM 成本）

按序执行，把 600–1200 条缩减到 100–150 条：

1. 主题是否命中目标 AI 领域（`topics.yaml` 规则）。
2. 是否符合所属类别的时间通道（见 3.1）。
3. 摘要长度是否足够（< 300 字符的直接丢弃）。
4. 是否与历史推荐重复（查 `seen-items`，含冷却期判断）。
5. 低质量特征：纯 workshop abstract、无方法描述、撤稿标记、明显的占位稿。
6. 原始来源是否可访问（PDF URL 可解析）。

### 4.4 阶段四：L1 批内相对排序

**不用独立打 1–10 分。** LLM 独立评分存在严重分数压缩（多数落在 7–8）且跨批次不可比。

改为：每批 15 条一组，让模型在同一上下文内做**相对排序**，输出 `rank` + `tier`（A/B/C）+ 一句具体理由。同时在 prompt 内给出 rubric 锚点样例（各 tier 一个示例）。这样既解决可比性，也比逐条独立调用省 token。

- 模型：`deepseek-flash`，`reasoning_effort="low"`
- 输入：标题 + 摘要 + 分类 + 来源 + 热度/引用信号
- 输出：JSON（`response_format={"type":"json_object"}`）
- 规模：100–150 条 → 约 7–10 次调用 → 保留前 30–40 条

三类内容**分池独立排序**，各自带不同 rubric：前沿看新颖性与问题重要性，综述看覆盖面与脉络梳理质量，经典看长期影响与可读性。

#### 综合得分权重

L1 排序结果转为分数后，与客观信号加权：

| 维度 | 权重 | 来源 |
|---|---:|---|
| 研究价值与重要性 | 30% | L1/L2 判定 |
| 创新性 | 25% | L1/L2 判定 |
| 技术深度与证据质量 | 20% | L2 判定（仅深度分析过的条目） |
| 学术/社区影响力 | 15% | OpenAlex 引用 + HF upvotes + GitHub 增量（客观） |
| 内容完整度与可读性 | 10% | 规则（摘要长度、是否有代码、是否有项目页） |

### 4.5 阶段五：配额与多样性重排

从各池按配额选出最终条目（前沿 10；综述/经典按周预算账本，通常各 1）。

多样性约束：

1. 单一研究子方向（二级主题）默认不超过 4 篇。
2. 同一通讯机构不超过 3 篇。
3. 高度相似研究（标题相似度 0.75–0.90 区间）只保留得分最高的一篇。
4. 最近 90 天推荐过的论文进入冷却期，不再出现。
5. 保留不同技术路线与研究视角。

某类别无足够合格候选时**减少当日数量，不降低质量门槛**。减量原因写入当期 JSON 的 `notes` 字段，前端显示。

### 4.6 阶段六：L2 深度分析与解读（合并调用）

v1.0 把"深度评分"和"生成解读"设计为两步，各自读一遍全文，等于把最贵的 token 付两次。v2.0 **合并为单次调用**：一次读材料，同时输出结构化评分和中文解读。

#### 取材分级（控制 token）

| 层级 | 取材 | 约 token | 用于 |
|---|---|---:|---|
| `abstract` | 标题 + 摘要 | 0.5K | 未入选的候选 |
| `intro_conclusion` | 摘要 + Intro + Conclusion + 表格 caption + Related Work | 5–8K | L2 初筛（约 30 条） |
| `fulltext` | PDF 全文抽取 | 20–30K | 最终入选条目（10–12 条） |

`evidence_level` 字段记录实际取材层级，**只有摘要时不得声称完成全文分析**，前端显式标注。

#### 模型配置

- 模型：`deepseek-flash`，`extra_body={"reasoning_effort": "high"}`，`temperature=0`，`response_format={"type": "json_object"}`
- 上下文 1M / 最大输出 393K，全文输入无压力
- 解读与排序同用 flash：flash 的上下文与最大输出和 pro 相同，全文取材放得下，单价低一个档位
- 依赖 DeepSeek 的自动 prefix cache：把固定的 system prompt + 模板放在消息最前，可变的论文正文放后面，使 `prompt_cache_hit_tokens` 最大化
- 每条目记录 `model_used`、`tokens_in`、`tokens_out`、`cache_hit_tokens`

---

## 五、AI 解读设计

解读是产品核心价值。要求模型不只概括摘要，而要解释：为什么重要、现有工作的问题、核心创新、方法如何对应动机、证据支持什么结论、有哪些局限、对后续研究的启发。

输出为 JSON（供前端渲染），字段按内容类型分四套模板，定义在 `config/prompts/`。

### 5.1 前沿论文（`frontier.md`）

`why_read` 为什么推荐 · `motivation` 研究背景与动机 · `innovations[]` 核心创新 1–3 条 · `method` 方法思路 · `results` 实验结果（含基线、指标、结论）· `limitations` 局限性 · `insights` 研究启发

### 5.2 深度综述（`survey.md`）

`why_read` · `scope` 主题与背景 · `evolution` 领域发展脉络 · `approaches[]` 代表性技术路线 · `key_differences` 关键技术差异 · `bottlenecks` 当前瓶颈 · `future_directions` 未来方向

### 5.3 经典研究（`classic.md`）

`why_today` 今天为何仍值得读 · `historical_context` 历史背景 · `problem_then` 当时的核心问题 · `contributions` 开创性贡献 · `core_idea` 核心技术思想 · `influence` 对后续研究的影响 · `modern_reading` 今天应如何理解

复推时额外传入 `previous_angles[]`（历次解读角度）与该论文近 180 天的新引用工作，要求换角度。

### 5.4 GitHub 项目（`github.md`）

`why_read` · `problem` 解决什么问题 · `features` 核心功能 · `architecture` 技术架构与关键实现 · `vs_alternatives` 相比现有方案的特点 · `usage` 安装与使用 · `scope_limits` 适用场景与局限

项目性能、功能与限制必须依据 README、源码或关联论文，**不得把宣传措辞当事实**。

### 5.5 解读质量硬性要求

1. 不虚构实验、数值和结论。
2. 区分"作者声称"与"已验证"，前者用明确的归属措辞。
3. 优先基于正文分析；仅有摘要时标记 `evidence_level: abstract`。
4. 核心技术术语保留英文（Transformer、Flow Matching、KV Cache 等）。
5. 简洁专业的中文表达，避免营销腔。
6. 客观指出局限。
7. 保留原文链接。
8. 推荐理由必须具体到机制或数字，不能只写"具有重要意义"。
9. **每个引用的数值必须附 `source_span`**（见第六节）。

---

## 六、机械化事实校验

v1.0 写了"关键数值校验""AI 幻觉检查"但没有可执行定义。用另一个 LLM 复查既贵又不可靠。v2.0 全部改为**机械可检**。

### 6.1 元数据核验（硬失败则丢弃条目）

| 检查 | 方法 |
|---|---|
| arXiv ID 存在 | 调 arXiv API 确认返回条目 |
| 标题一致 | 与官方元数据归一化后逐字比对 |
| 首位作者一致 | 字符串包含比对 |
| 日期一致 | `published` 与官方一致 |
| 链接可达 | paper / code / project 逐个 HEAD 请求，期望 2xx/3xx |
| 中文标题非空且与原标题不同 | 防止未翻译 |

### 6.2 数值溯源（核心反幻觉机制）

要求模型在输出每个数值时，同时给出结构化溯源：

```json
{
  "claim": "在 VBench 上总分达到 83.1，较 baseline 提升 2.4 分",
  "numbers": ["83.1", "2.4"],
  "source_span": "Table 2, row 'Ours'",
  "quote": "Ours 83.1 ... +2.4"
}
```

校验脚本在抽取的 PDF 正文中**字符串反查**每个 `numbers` 元素与 `quote` 片段：

- 全部命中 → 保留。
- 数值未在原文出现 → **删除该 claim 所在句子**，并在 `validation.dropped_claims` 记录。
- 同一条目丢弃 claim 超过 30% → 整条降级为 `evidence_level: abstract` 并重新生成不含数值的解读。

`evidence_level: abstract` 的条目**禁止输出任何实验数值**，prompt 内硬性约束 + 校验层正则拦截（匹配 `\d+\.\d+%?` 等模式）。

### 6.3 其他机械检查

- 字段完整性：按内容类型校验必填字段非空。
- 重复检查：当期内两条解读的 3-gram 重叠率 > 0.4 时告警。
- 中英混排：中英文之间空格规范化；检测未翻译残留（连续 ≥ 8 个 ASCII 单词）。
- 长度范围：各字段字数上下限（如 `why_read` 60–200 字）。

"中文可读性"不做 LLM 评估，自动化价值低。

---

## 七、前端与手机端

技术栈：React + Vite + Tailwind CSS + `vite-plugin-pwa`，托管 GitHub Pages，本地状态 IndexedDB。

### 7.1 页面结构

**首页 · 今日精选**：当期内容按前沿/综述/经典分组。卡片显示中文标题、英文原标题、研究领域、推荐类型、来源、简短介绍、推荐理由、发布时间、预计阅读时间、证据层级标记、收藏状态。

**发现页 · 研究方向**：按二级主题浏览，支持关键词搜索、分类/来源/时间筛选、质量或热度排序。

**历史归档**：按日期浏览、按领域筛选、搜索历史条目。

**收藏与阅读记录**：收藏、已读标记、最近阅读、**导出与导入**（见 7.3，P0 而非 P1）。

### 7.2 GitHub Pages 工程坑（v1.0 未提）

| 问题 | 表现 | 处理 |
|---|---|---|
| 子路径部署 | Pages 在 `/daily-ai-insight/`，PWA 的 `start_url`/`scope`/SW scope 不带前缀会注册失败 | `vite.config.ts` 设 `base: '/daily-ai-insight/'`，manifest 与 SW 同步 |
| SPA 路由 404 | Pages 无 history 回退 | 构建后把 `index.html` 复制为 `404.html` |
| SW 缓存吃掉每日更新 | `latest.json` 若走 CacheFirst，用户永远看不到新内容 | 静态资源 CacheFirst；**数据 JSON 用 NetworkFirst**；`registerType: 'autoUpdate'` |
| 新版本不生效 | 旧 SW 持续接管 | `skipWaiting` + `clientsClaim`，前端检测到新版本提示刷新 |

### 7.3 iOS 存储清除（必须处理）

Safari 对**未安装到主屏**的站点，7 天无访问会清除 script-writable storage（含 IndexedDB 与 localStorage），收藏会静默丢失。

对策：

1. 收藏导出/导入提到 **P0**（v1.0 放在阶段四）。
2. UI 明确提示："收藏保存在本机浏览器，建议添加到主屏并定期导出。"
3. 收藏数超过阈值且超过 14 天未导出时，提示导出。

### 7.4 手机端要求

Mobile First 响应式；底部导航；适合中文长文的字号行距（正文 16px / 行高 1.75）；深色模式；简版/详细版解读切换；PWA 添加到桌面；缓存近 7 期实现离线阅读；阅读进度记录；一键打开论文与代码链接。

第一版不做实时 AI 问答，避免引入需要密钥中转的在线服务。

---

## 八、仓库、存储与运行保障

### 8.1 技术组件

| 层级 | 技术 | 职责 |
|---|---|---|
| 调度 | GitHub Actions（cron UTC） | 每日任务 |
| 采集 | Python + httpx + feedparser | 多源数据 |
| 解析 | pymupdf / BeautifulSoup | PDF 与 HTML |
| 排序 | `deepseek-flash` + 规则 | L1 相对排序 |
| 解读 | `deepseek-flash` | L2 分析 + 中文解读 |
| 存储 | Git + JSON | 每期与历史归档 |
| 前端 | React + Vite + Tailwind | 静态站点 |
| 托管 | GitHub Pages | 部署 |
| 本地状态 | IndexedDB | 收藏与已读 |
| 安装缓存 | PWA | 手机与离线 |

### 8.2 Actions 权限与故障隔离

- 拆成两个 job：`pipeline`（`permissions: contents: write`，持有 `DEEPSEEK_API_KEY`）与 `deploy`（`permissions: pages: write, id-token: write`，不接触密钥）。
- 单数据源失败只记日志并跳过该源，不中断流水线。
- LLM 调用设单次任务 token 预算上限，超限中止并保留已完成部分。
- 失败时**不覆盖 `latest.json`**；前端始终显示数据自带的 `arxiv_announce_date`，而不是"今天"。
- **即使失败也 push 一份运行日志**：公共仓库 60 天无 push 会被 GitHub 自动禁用 scheduled workflow，若写回因 token 失效连续失败，任务会静默停掉。
- 支持 `workflow_dispatch` 手动触发，可传 `--date` 重跑指定批次。
- 写回落在 `data` 分支：工作流把它检出到 `.data-branch/`（分支不存在时首跑自动创建），采集步骤的 `DAI_DATA_DIR` 指向该目录，提交与 push 都在其中执行，`main` 的历史只有代码。

### 8.3 数据文件与防膨胀（v1.0 未处理）

v1.0 的 `seen-items.json` 和 `github-star-history.json` 每天全量重写，git 每次提交都存一份完整副本，一年后每次 commit 带几 MB 冗余。

**数据不进 `main`**：成品、缓存、日志每天都变，混进代码历史会让 clone 体积逐日膨胀。所有 `data/` 内容提交到孤立分支 **`data`**，CI 把它检出到 `.data-branch/` 并通过 `DAI_DATA_DIR` 让脚本写入该目录（`scripts/common.py` 的 `DATA_DIR` 读这个环境变量，默认回落到 `./data` 供本地开发）。阶段三的前端构建同样从 `data` 分支取数据。

| 文件 | 内容 | 防膨胀策略 |
|---|---|---|
| `data/latest.json` | 当期推荐（首页快速读取） | 单期，小 |
| `data/daily/YYYY/MM/DD.json` | 该期完整内容与解读 | 天然按天分片 |
| `data/archive/index-YYYY.json` | **按年分片**的轻量索引 | 每年一个文件，前端按需加载 |
| `data/state/seen-YYYY-MM.json` | **按月分片**的已推荐记录 | 冷却期判断只需读最近 4 个月 |
| `data/state/seen-digest.json` | 全历史 ID 哈希集（去重用） | 仅存短哈希，增长极慢 |
| `data/state/week-budget.json` | 本周综述/经典配额账本 | 单周，每周重置 |
| `data/state/stars/YYYY-MM.json` | **按月分片**的 star 快照 | 保留 13 个月，更早的聚合为月度均值后归档 |
| `data/state/citations/YYYY-MM.json` | **按论文年月分片**的 OpenAlex 信号缓存 | 只读写请求涉及的分片；条目去空值，`counts_by_year` 只留 2 年 |
| `data/classics.json` | 经典池（含推荐历史） | 随池子线性增长，可接受 |
| `data/metrics/YYYY-MM.json` | token、成本、成功率、各源状态 | 按月分片 |
| `data/raw/YYYY-MM-DD/<source>.json` | 各源原始响应（调试用） | **不进 git**（`data/.gitignore`），可由 `collect_all` 重算 |
| `data/candidates/YYYY-MM-DD.json` | 阶段一候选池（约 2000 条含全文摘要，单批次 ~8MB） | **不进 git**，同上 |

### 8.4 时区与幂等

- JSON 内所有时间字段统一 **ISO8601 带时区**（如 `2026-11-23T10:30:00+08:00`），不再出现裸日期。
- `arxiv_announce_date` 为纯日期（批次标识），单独注明语义。
- 以 `arxiv_announce_date` 为当期主键，重跑覆盖而非追加。

### 8.5 目录结构

```text
daily-ai-insight/
├── .github/workflows/daily.yml
├── config/
│   ├── sources.yaml          # 各源的分类、限速、开关
│   ├── topics.yaml           # 二级主题关键词规则
│   ├── scoring.yaml          # 权重、阈值、多样性约束
│   ├── classics.yaml         # 经典论文池
│   └── prompts/
│       ├── rank_frontier.md  ├── rank_survey.md  ├── rank_classic.md
│       ├── frontier.md       ├── survey.md
│       └── classic.md        └── github.md
├── scripts/
│   ├── collect/{arxiv,huggingface,github,openalex,semanticscholar}.py
│   ├── llm.py                # DeepSeek 客户端、重试、token 计量
│   ├── normalize.py          ├── deduplicate.py   ├── filter.py
│   ├── rank.py               ├── rerank.py        ├── summarize.py
│   ├── validate.py           ├── archive.py       └── run_daily.py
├── data/                     # 见 8.3；提交到 data 分支，不进 main
├── web/
│   ├── public/{manifest.webmanifest,icons/}
│   ├── src/{components,pages,hooks,utils,styles}/
│   ├── package.json          └── vite.config.ts
├── tests/
├── requirements.txt
├── DESIGN.md                 └── README.md
```

### 8.6 统一数据结构

```json
{
  "id": "arxiv-2510.18927",
  "content_type": "paper",
  "recommendation_type": "frontier",
  "channel": "primary",
  "title": "BAPO: Stabilizing Off-Policy Reinforcement Learning...",
  "title_zh": "BAPO：稳定离策略强化学习的平衡裁剪方法",
  "authors": ["..."],
  "source": ["arxiv", "huggingface"],
  "topics": ["强化学习与后训练", "LLM 与推理"],
  "topics_source": "rule",
  "arxiv_id": "2510.18927",
  "arxiv_version": "v2",
  "primary_category": "cs.LG",
  "published_at": "2026-11-18T01:30:00+00:00",
  "updated_at": "2026-11-21T06:12:00+00:00",
  "arxiv_announce_date": "2026-11-23",
  "recommended_at": "2026-11-23T10:30:00+08:00",
  "links": { "paper": "...", "pdf": "...", "code": null, "project": null },
  "signals": {
    "hf_upvotes": 42, "hf_trending": true,
    "citation_count": 3, "influential_citation_count": 0,
    "work_type": "preprint", "fwci": 1.8, "citation_percentile": 0.91,
    "repo_stars": null, "repo_stars_7d": null
  },
  "quality": { "score": 86.4, "tier": "A", "rank_in_pool": 2,
               "evidence_level": "fulltext" },
  "content": { "why_read": "...", "motivation": "...", "innovations": ["..."],
               "method": "...", "results": "...", "limitations": "...",
               "insights": "...", "reading_minutes": 12 },
  "claims": [ { "claim": "...", "numbers": ["83.1"],
                "source_span": "Table 2", "verified": true } ],
  "validation": { "links_ok": true, "metadata_ok": true,
                  "dropped_claims": 0 },
  "cost": { "model_used": "deepseek-flash", "tokens_in": 24130,
            "tokens_out": 2874, "cache_hit_tokens": 1180 }
}
```

说明：`channel` 取值 `primary` / `catchup` / `revision`，对应 3.1 的三个通道。`evidence_level` 取值 `abstract` / `intro_conclusion` / `fulltext`。所有值必须从真实数据生成并通过第六节校验。

---

## 九、成本控制与可观测

### 9.1 每日处理预算

| 阶段 | 规模 | 模型 | 取材层级 |
|---|---:|---|---|
| 原始采集 | 700–1400 | 无 | — |
| 规则初筛 | 100–150 | 无 | — |
| L1 相对排序 | 100–150（批内 15 条/次） | `deepseek-flash` eff=low | abstract |
| L2 深度分析 | 30–40 | `deepseek-flash` eff=high | intro_conclusion |
| 解读生成 | 10–12 | `deepseek-flash` eff=high | fulltext（与 L2 合并调用） |

### 9.2 降成本措施

1. **L2 与解读合并单次调用**，不重复读全文（v1.0 的最大浪费）。
2. **取材分级**：只有最终入选的 10–12 条读全文，L2 初筛阶段读 5–8K 摘要+Intro+Conclusion。
3. **利用 DeepSeek 自动 prefix cache**：固定 system prompt 与模板置于消息最前，可变正文置后。
4. 规则承担全部初筛，不用 LLM。
5. 已分析条目缓存评分与解读，按 `content_hash` 判断是否需要重算。
6. 经典论文解读缓存复用，仅复推换角度时重新生成。
7. GitHub 项目若 README 与版本号未变（`content_hash` 一致）则复用历史解读。
8. 单次任务设 token 预算上限与失败保护。

### 9.3 成本与运行指标落盘

写入 `data/metrics/YYYY-MM.json`，使"每日更新成功率"和"成本"成为可观测指标而非口号：

```json
{ "date": "2026-11-23",
  "sources": { "arxiv": {"ok": true, "count": 912},
               "huggingface": {"ok": true, "count": 47},
               "github": {"ok": false, "error": "rate_limit"},
               "openalex": {"ok": true, "count": 1317},
               "semanticscholar": {"ok": true, "stats": {"skipped": "no_api_key"}} },
  "llm": { "deepseek-flash": {"calls": 9, "tokens_in": 68420, "tokens_out": 7310},
           "deepseek-flash": {"calls": 12, "tokens_in": 281500,
                               "tokens_out": 34120, "cache_hit_tokens": 42300} },
  "output": { "frontier": 10, "survey": 1, "classic": 1 },
  "validation": { "items_dropped": 1, "claims_dropped": 4 },
  "duration_seconds": 3180, "status": "ok" }
}
```

单价不写入设计文档（避免引用未核实数字）：首周实测后把实际单价填入 `config/scoring.yaml` 的 `pricing` 段，由脚本按 token 自动折算每日成本写进 metrics。

---

## 十、版权与合规（v1.0 未提）

- arXiv 摘要多数并非 CC 授权，**不整段复制英文摘要**到站点；只存元数据加自产中文解读（衍生内容风险低）。
- GitHub README 内容受各仓库 license 约束，引用需短摘并标注来源。
- Hugging Face 页面抓取遵守其服务条款，设合理限速与 User-Agent 标识。
- 所有条目**始终给出原文链接**，站点定位为导读而非替代阅读。
- 密钥只存 Actions Secrets（`DEEPSEEK_API_KEY`）；`llm_api_key.txt` 已加入 `.gitignore`，仅用于本地开发。
- `pull_request` 事件下 secrets 不可用，流水线相关 workflow 不挂 PR 触发。

---

## 十一、实施阶段与验收

每阶段完成后执行验收，**不符合预期则暂停并请求确认**，不自行扩大改动范围。

### 阶段一：数据源与自动采集（P0）

交付：四个采集模块、统一数据结构、规范化与去重、分片存储、Actions 定时执行。

验收：

- [ ] arXiv OAI-PMH 单日采集去重后 ≥ 600 篇，字段完整
- [ ] `published`/`updated` 三通道判定正确，ID 已剥版本号
- [ ] HF 端点可用；人为令其失败时流水线继续，metrics 标 `ok: false`
- [ ] GitHub trending 解析出 AI 项目；基线池 ≥ 2000 仓库；首日快照落盘
- [ ] OpenAlex 引用回填：已入库口径命中率 ≥ 80%（0–2 天龄论文任何源都未入库，不计入分母）
- [ ] 引用缓存生效：同批次二次运行从缓存读取，请求数降到个位/十位数
- [ ] 跨源去重：HF 与 arXiv 同一论文合并为一条，`source` 为两个值
- [ ] 标题相似度算法在构造样例上准确区分 ≥ 0.90 与 0.75–0.90
- [ ] 重跑同一 `arxiv_announce_date` 为覆盖而非追加
- [ ] 所有时间字段带时区

### 阶段二：排序、解读与校验（P0）

交付：规则初筛、L1 批内相对排序、周预算账本与多样性重排、四套解读模板、合并式 L2 调用、机械校验、metrics。

验收：

- [ ] 初筛后 100–150 条，各淘汰原因可统计
- [ ] L1 分池排序，同批次结果稳定（同输入两次运行 top-10 重叠 ≥ 8）
- [ ] 周预算账本正确：一周内综述与经典各恰好 5 篇，周末不投放
- [ ] 多样性约束生效：单一主题 ≤ 4 篇
- [ ] 入选条目 `evidence_level: fulltext`，PDF 全文抽取成功
- [ ] `evidence_level: abstract` 的条目输出中不含任何实验数值
- [ ] 数值溯源校验跑通：人为注入伪造数值能被检出并删除
- [ ] 链接与元数据校验全部通过
- [ ] metrics 记录真实 token，L2 单次调用而非两次
- [ ] 人工抽读 3 篇解读，内容准确、无幻觉、推荐理由具体

### 阶段三：静态网站与手机端（P0）

交付：首页、详情页、发现页、历史归档、搜索筛选、收藏与已读、收藏导出导入、响应式、PWA、Pages 自动部署。

> 前置依赖：本机当前**未安装 Node.js**，进入本阶段前需确认安装方式。

验收：

- [ ] `base: '/daily-ai-insight/'` 配置正确，Pages 子路径下资源与路由正常
- [ ] `404.html` 回退生效，直接访问深链接不 404
- [ ] PWA 可安装；manifest 的 `start_url`/`scope` 带仓库前缀
- [ ] SW 分策略：静态资源 CacheFirst，`latest.json` NetworkFirst（更新后刷新即见新内容）
- [ ] 离线可读最近 7 期
- [ ] 收藏导出为 JSON 并能导入还原；UI 有存储风险提示
- [ ] 历史索引按年分片，首页不下载全部历史
- [ ] iPhone Safari 与桌面 Chrome 实测通过
- [ ] 前端显示数据自带的批次日期，而非"今天"

### 阶段四：长期优化（P1）

Star 增长趋势可视化 · 研究兴趣个性化 · 相关文章关联 · 主题多样性调优 · 推荐反馈机制 · 内容质量监控 · 成本趋势看板 · 经典池半自动扩充流程。

---

## 十二、核心原则与 MVP 验收

### 12.1 核心原则

1. **热度只是发现信号，不是质量标准。** HF upvotes、GitHub stars 帮助召回候选，最终推荐必须经过独立质量评估。质量分与热度分在数据结构中物理分离。
2. **三类内容提供不同阅读价值。** 前沿看最新进展，综述建立系统认知，经典理解原理与脉络。配额匹配各自的供给特性，而不是强求等比例。
3. **解读准确性优先于数量。** 数值必须可溯源，证据层级必须如实标注。
4. **控制复杂度与成本。** 第一版坚持静态架构，无后端服务。
5. **支持长期积累。** 持久化归档，避免重复生成，防止仓库膨胀。

### 12.2 MVP 验收标准

- [ ] arXiv 指定领域自动采集（OAI-PMH，≥600 篇/日）
- [ ] Hugging Face 热门论文采集（故障隔离）
- [ ] GitHub 热门 AI 项目采集（trending + 快照双通道）
- [ ] OpenAlex 客观质量信号接入（引用数 / fwci / 百分位）
- [ ] 跨来源去重与信息合并
- [ ] 前沿/综述/经典三类独立候选池与独立 rubric
- [ ] 前沿 10 篇/日，综述 5 篇/周，经典 5 篇/周
- [ ] L1 批内相对排序 + 客观信号加权
- [ ] 结构化中文深度解读（四套模板）
- [ ] 数值溯源校验与证据层级标注
- [ ] 原文、代码、项目链接可达性校验
- [ ] 历史数据按年/月分片归档
- [ ] 手机端阅读 + PWA 安装
- [ ] 收藏、已读、导出导入
- [ ] GitHub Actions 每日自动运行（UTC cron，故障隔离，失败不覆盖上期）
- [ ] GitHub Pages 自动部署（子路径与 SW 策略正确）
- [ ] token 与成本落盘可观测
- [ ] 无常驻服务器与数据库

---

## 十三、最终技术方案

| 项目 | 方案 |
|---|---|
| 项目名称 | Daily AI Insight |
| 仓库 | 独立仓库 `daily-ai-insight` |
| 研究领域 | AI / CV / NLP / ML / RL / Multimodal / Robotics |
| 数据源 | arXiv + Hugging Face + GitHub + OpenAlex（S2 可选） |
| 推荐配额 | 前沿 10 篇/日；综述 5 篇/周；经典 5 篇/周 |
| 排序方法 | 规则初筛 → L1 批内相对排序 → 客观信号加权 → 多样性重排 |
| 解读模型 | 全程 `deepseek-flash`：L2 解读 eff=high，L1 排序 eff=low |
| 反幻觉 | 数值 `source_span` 原文反查 + 证据层级标注 |
| 调度 | GitHub Actions，cron `30 2 * * *` UTC（北京 10:30） |
| 存储 | Git + JSON，按年/月分片 |
| 前端 | React + Vite + Tailwind CSS + vite-plugin-pwa |
| 托管 | GitHub Pages（子路径 `/daily-ai-insight/`） |
| 用户数据 | IndexedDB + 导出导入 |
| 常驻服务器 | 无 |
| 可变成本 | 仅 DeepSeek API 调用，token 落盘可观测 |

**最终目标：建设一个无需维护服务器、每天自动发现 AI 领域优质研究成果、提供可溯源的中文深度解读，并支持手机阅读与长期知识积累的个人研究内容推荐平台。**
















