"""阶段三：规则初筛（零 LLM 成本）。

把 600–1200 条候选缩到 100–150 条，并分成 frontier / survey / classic 三池。
顺序照 DESIGN 4.3：通道 → 摘要长度 → 低质量特征 → 历史重复与冷却。

两个实现决定值得写明：

1. **无关联论文的 GitHub 项目进 frontier 池**，用 `content_type` 区分，
   后续按 `github.md` 模板解读。配额表里没有独立的 repo 名额，让项目与
   论文在同一池竞争比硬留名额更合理。
2. **名额按通道切分**。规则本身只能筛掉约三成，900 条到 120 条的那一刀
   终究要按信号排序；而当天公告的新论文引用与 upvotes 必然为 0，若与
   catchup 混在一起排，前沿频道会被几周前的热门论文整体占满。
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from .common import (
    days_since,
    load_config,
    log,
    parse_dt,
    read_json,
    recent_seen_months,
    utcnow,
)


def filter_candidates(items: list[dict], *, batch: date,
                      now: Any = None) -> dict[str, Any]:
    """返回 {"pools": {frontier/survey/classic: [...]}, "stats": {...}}。"""
    cfg = load_config("scoring")["filter"]
    now = now or utcnow()
    seen = _load_seen(batch, int(cfg["cooldown_days"]))

    kept: list[dict] = []
    drops: dict[str, int] = {}
    for item in items:
        reason = _drop_reason(item, cfg, seen, now)
        if reason:
            drops[reason] = drops.get(reason, 0) + 1
            continue
        item["prefilter"] = {
            "completeness": round(completeness_score(item), 1),
            "impact": round(impact_score(item), 1),
        }
        kept.append(item)
    return _build_pools(kept, cfg, drops, len(items))


# ------------------------------------------------------------ 分池


def _build_pools(kept: list[dict], cfg: dict, drops: dict[str, int],
                 total: int) -> dict[str, Any]:
    surveys: list[dict] = []
    frontier: list[dict] = []
    for item in kept:
        (surveys if _is_survey(item, cfg) else frontier).append(item)

    target = cfg["pool_target"]
    pools = {
        "frontier": _take_by_channel(frontier, cfg, int(target["frontier"])),
        "survey": _top(surveys, int(target["survey"])),
        # 经典池不来自当期候选，由 classics.yaml 装配（见 classic_pool）
        "classic": [],
    }
    stats = {
        "input": total,
        "passed_rules": len(kept),
        "dropped": drops,
        "survey_matched": len(surveys),
        "pool_sizes": {name: len(pool) for name, pool in pools.items()},
    }
    log.info("初筛：%d → %d 条（前沿 %d，综述 %d），丢弃 %s",
             total, sum(len(p) for p in pools.values()), len(pools["frontier"]),
             len(pools["survey"]), drops)
    return {"pools": pools, "stats": stats}


def _take_by_channel(items: list[dict], cfg: dict, target: int) -> list[dict]:
    """按通道配比取名额；某通道不足时把余额让给 primary。"""
    share = cfg["frontier_channel_share"]
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item.get("channel") or "unknown", []).append(item)

    picked: list[dict] = []
    leftover = 0
    for channel, ratio in share.items():
        quota = int(round(target * float(ratio)))
        pool = _top(buckets.get(channel) or [], quota)
        leftover += quota - len(pool)
        picked.extend(pool)
    if leftover > 0:
        chosen = {item["id"] for item in picked}
        rest = [item for item in buckets.get("primary") or []
                if item["id"] not in chosen]
        picked.extend(_top(rest, leftover))
    return sorted(picked, key=_prescore, reverse=True)


def _top(items: list[dict], count: int) -> list[dict]:
    return sorted(items, key=_prescore, reverse=True)[:max(count, 0)]


def _prescore(item: dict) -> float:
    """只用来决定"谁进 L1"，不是质量分：客观信号 + 完整度 + 分类优先级。"""
    pre = item.get("prefilter") or {}
    tier = {"high": 6.0, "medium": 0.0}.get(item.get("category_tier") or "", 0.0)
    topics = 4.0 if item.get("topics") else 0.0
    impact = pre.get("impact") or 0.0
    completeness = pre.get("completeness") or 0.0
    return impact * 0.6 + completeness * 0.4 + tier + topics


# ------------------------------------------------------------ 规则


def _drop_reason(item: dict, cfg: dict, seen: dict[str, str],
                 now: Any) -> str | None:
    """返回丢弃原因，None 表示通过。顺序按代价从低到高。

    DESIGN 4.3 还列了"PDF URL 可解析"。那要对每条发一次 HEAD 请求，900 条
    候选就是 900 次外部请求；链接可达性改到 validate.py 只查最终入选条目。
    """
    channel = item.get("channel")
    if channel not in cfg["channels"]:
        return f"channel_{channel}"

    text = f"{item.get('title') or ''} {item.get('abstract') or ''}".lower()
    for pattern in cfg["drop_patterns"]:
        if pattern in text:
            return "low_quality"

    abstract = (item.get("abstract") or "").strip()
    if item.get("content_type") == "repo":
        # 仓库只有一句 description，不能按论文摘要的长度卡
        if not abstract:
            return "no_description"
    elif len(abstract) < int(cfg["min_abstract_chars"]):
        return "short_abstract"

    if cfg.get("require_topic_hit") and not item.get("topics"):
        return "no_topic"

    last = seen.get(item.get("id") or "")
    if last:
        age = days_since(parse_dt(last), now)
        if age is not None and age <= int(cfg["cooldown_days"]):
            return "cooldown"
    return None


def _is_survey(item: dict, cfg: dict) -> bool:
    if item.get("content_type") == "repo":
        return False
    title = (item.get("title") or "").lower()
    if not any(p in title for p in cfg["survey_patterns"]):
        return False
    references = (item.get("signals") or {}).get("reference_count")
    if references is None:
        # OpenAlex 还没入库，光凭标题就判综述，交给 L1 再筛
        return True
    return int(references) >= int(cfg["survey_min_references"])


# ------------------------------------------------------------ 客观分


def completeness_score(item: dict) -> float:
    """内容完整度 0–100（DESIGN 4.4 的 10% 维度）：纯规则，无模型参与。"""
    abstract = item.get("abstract") or ""
    links = item.get("links") or {}
    # 摘要 1200 字符以上视为充分，再长不加分
    score = 50.0 * min(len(abstract) / 1200.0, 1.0)
    if links.get("code"):
        score += 20.0
    if links.get("project"):
        score += 10.0
    if item.get("doi") or item.get("journal_ref"):
        score += 10.0
    if item.get("authors"):
        score += 10.0
    return min(score, 100.0)


def impact_score(item: dict) -> float:
    """学术/社区影响力 0–100（DESIGN 4.4 的 15% 维度）。

    三项信号各自 log 归一化后取最大值：任一维度爆发都算有影响力，求和会让
    三项都平庸的条目反超单项突出的。
    """
    cfg = load_config("scoring")["impact_signals"]
    signals = item.get("signals") or {}
    pairs = (
        (signals.get("hf_upvotes"), cfg["hf_upvotes_full"]),
        (signals.get("citation_count"), cfg["citation_count_full"]),
        (signals.get("repo_stars_7d"), cfg["repo_stars_7d_full"]),
    )
    best = 0.0
    for raw, full in pairs:
        if raw is None or float(full) <= 0:
            continue
        ratio = math.log1p(max(float(raw), 0.0)) / math.log1p(float(full))
        best = max(best, min(ratio, 1.0) * 100.0)
    return best


# ------------------------------------------------------------ 历史与经典池


def _load_seen(batch: date, cooldown_days: int) -> dict[str, str]:
    """读最近几个月的已推荐记录，返回 {item_id: 末次推荐时间}。

    冷却期 90 天，读 4 个月分片足够覆盖；再早的记录对本期判断没有影响。
    """
    months = max(cooldown_days // 30 + 2, 2)
    seen: dict[str, str] = {}
    for path in recent_seen_months(batch, months):
        shard = read_json(path, default={}) or {}
        for item_id, record in shard.items():
            last = (record or {}).get("last_recommended")
            if last and last > seen.get(item_id, ""):
                seen[item_id] = last
    return seen


def classic_pool(*, batch: date, now: Any = None) -> list[dict]:
    """从 classics.yaml 装配经典池，剔除仍在冷却期的条目。

    经典不来自当期候选：它们是人工维护的长期池子，每周投放 5 篇，靠
    `cooldown_days` 控制复推间隔（DESIGN 3.5 用 180 天）。

    只向 L1 交 `pool_target.classic` 条：池子有 50–400 篇，全量送排序等于
    每天多烧几十次调用，而每周只投 5 篇。排序键是"上次推荐时间最早的优先"，
    从没推过的（空串）排最前，于是轮换不需要随机数也能覆盖全池。
    """
    try:
        cfg = load_config("classics")
    except FileNotFoundError:
        log.info("config/classics.yaml 不存在，经典池为空")
        return []
    now = now or utcnow()
    cooldown = int(cfg.get("cooldown_days") or 180)
    seen = _load_seen(batch, cooldown)
    ready: list[tuple[str, int, dict]] = []
    for index, entry in enumerate(cfg.get("papers") or []):
        item = _from_classic(entry, batch)
        last = seen.get(item["id"])
        age = days_since(parse_dt(last), now) if last else None
        if age is not None and age <= cooldown:
            continue
        item["previous_angles"] = (entry.get("angles") or [])
        ready.append((last or "", index, item))

    target = int(load_config("scoring")["filter"]["pool_target"]["classic"])
    ready.sort(key=lambda row: (row[0], row[1]))
    picked = [item for _, _, item in ready[:target]]
    log.info("经典池：%d 篇可用（共 %d 篇，冷却 %d 天），送排序 %d 篇",
             len(ready), len(cfg.get("papers") or []), cooldown, len(picked))
    return picked


def _from_classic(entry: dict, batch: date) -> dict[str, Any]:
    """classics.yaml 的条目转成与候选池同构的 ResearchItem。

    `completeness` 置 None 而不是按 `note` 长度算：那一项衡量的是"手上的材料
    够不够写解读"，经典篇的材料是全文 PDF，一句话定位不代表材料少。按长度算
    会让 BERT 的完整度只有 11 分，跨池比分时凭空垫底（composite_score 对
    None 的处理是剔除后重新归一化，不是按 0 计）。
    """
    arxiv_id = str(entry.get("arxiv_id") or "").strip()
    links = {"paper": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
             "pdf": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None,
             "code": entry.get("code"), "project": entry.get("project")}
    item = {
        "id": f"arxiv-{arxiv_id}" if arxiv_id else f"classic-{entry['title'][:40]}",
        "content_type": "classic",
        "source": ["classics"],
        "arxiv_id": arxiv_id or None,
        "title": entry.get("title"),
        "title_zh": None,
        "authors": entry.get("authors") or [],
        "abstract": entry.get("note"),
        "published_at": entry.get("published_at"),
        "updated_at": None,
        "arxiv_announce_date": batch.isoformat(),
        "channel": "classic",
        "topics": entry.get("topics") or [],
        "topics_source": "manual",
        "primary_category": None,
        "categories": [],
        "category_tier": "high",
        "doi": entry.get("doi"),
        "links": {k: v for k, v in links.items() if v},
        "signals": {"citation_count": entry.get("citation_count")},
        "topics_hint": [],
        "year": entry.get("year"),
    }
    item["prefilter"] = {"completeness": None,
                         "impact": round(impact_score(item), 1)}
    return item





