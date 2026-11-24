"""规范化与跨源合并：产出统一的 ResearchItem 列表。

本模块负责三件事：
1. 把 arXiv / HF / GitHub / S2 四源的字段映射到统一结构，并按 arXiv ID 合并；
2. 判定前沿三通道（primary / catchup / revision）——这是 v1.0 把 2018 年
   的论文当成"前沿"的根因所在；
3. 按 topics.yaml 规则打二级主题标签，规则未命中才留给 L1 模型兜底。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .common import (
    content_hash,
    days_since,
    load_config,
    log,
    parse_dt,
    utcnow,
)

PRIMARY_DAYS = 7
CATCHUP_DAYS = 30


def normalize(
    *,
    batch: date,
    arxiv_items: list[dict],
    hf_items: list[dict] | None = None,
    github_items: list[dict] | None = None,
    citation_signals: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """合并四源数据，返回 {"items", "stats"}。"""
    hf_items = hf_items or []
    github_items = github_items or []
    citation_signals = citation_signals or {}
    now = utcnow()

    items: dict[str, dict[str, Any]] = {}
    for raw in arxiv_items:
        item = _from_arxiv(raw, batch)
        items[item["arxiv_id"]] = item
    stats = {"arxiv": len(items), "hf_merged": 0, "hf_new": 0,
             "repo_merged": 0, "repo_standalone": 0, "citations_attached": 0}

    # HF：命中则合并热度信号，未命中则补建条目（HF 常收录窗口外的论文）
    for raw in hf_items:
        arxiv_id = raw.get("arxiv_id")
        if not arxiv_id:
            continue
        target = items.get(arxiv_id)
        if target is None:
            items[arxiv_id] = _from_huggingface(raw, batch)
            stats["hf_new"] += 1
        else:
            _merge_huggingface(target, raw)
            stats["hf_merged"] += 1

    # GitHub：有论文线索则合并为代码链接，否则作为独立项目条目
    for raw in github_items:
        hint = raw.get("arxiv_id_hint")
        target = items.get(hint) if hint else None
        if target is not None:
            _merge_repo(target, raw)
            stats["repo_merged"] += 1
        else:
            standalone = _from_repo(raw, batch)
            items[standalone["id"]] = standalone
            stats["repo_standalone"] += 1

    # 学术信号：OpenAlex 为主，S2 的 tldr/influential 已在上游并入
    for arxiv_id, signals in citation_signals.items():
        target = items.get(arxiv_id)
        if target is None:
            continue
        for field in ("citation_count", "influential_citation_count",
                      "reference_count", "venue", "work_type", "fwci",
                      "citation_percentile", "citations_by_year",
                      "institutions"):
            if signals.get(field) is not None:
                target["signals"][field] = signals[field]
        target["s2_tldr"] = signals.get("tldr")
        # OpenAlex 的 topics 当作主题规则的额外输入，不直接当结论
        for topic in signals.get("oa_topics") or []:
            if topic not in target["topics_hint"]:
                target["topics_hint"].append(topic)
        stats["citations_attached"] += 1

    # 通道判定与主题标签
    channels: dict[str, int] = {}
    topic_sources: dict[str, int] = {}
    for item in items.values():
        item["channel"] = _classify_channel(item, now)
        channels[item["channel"]] = channels.get(item["channel"], 0) + 1
        topics, source = _assign_topics(item)
        item["topics"] = topics
        item["topics_source"] = source
        topic_sources[source] = topic_sources.get(source, 0) + 1
        item["content_hash"] = content_hash(
            item.get("title"), item.get("abstract"), item.get("arxiv_version")
        )

    stats["channels"] = channels
    stats["topic_sources"] = topic_sources
    stats["total"] = len(items)
    log.info("规范化：%d 条（通道 %s，主题来源 %s）",
             len(items), channels, topic_sources)
    return {"items": list(items.values()), "stats": stats}


# ------------------------------------------------------------ 通道判定


def _classify_channel(item: dict, now: Any) -> str:
    """区分真正的新论文、被低估的补捞对象和老论文的新修订版。

    v1.0 只列了 published / updated 两个字段却没规定筛选用哪个。实测一个
    OAI 日期窗口内约 39% 的记录是老论文的更新（例见 1802.03498 的 v9），
    若按 updated 判定会把 2018 年的论文当成"前沿研究"。
    """
    if item.get("content_type") == "repo":
        # 仓库不按论文年龄判定：热门 AI 项目多是存量项目突然增长，
        # 用 created_at 会把它们全判成 stale。
        repo = item.get("repo") or {}
        if repo.get("is_trending"):
            return "primary"
        growth = (item.get("signals") or {}).get("repo_stars_7d")
        if growth is not None and growth > 0:
            return "catchup"
        return "stale"

    age = days_since(parse_dt(item.get("published_at")), now)
    if age is None:
        return "unknown"

    if age <= PRIMARY_DAYS:
        return "primary"

    if age <= CATCHUP_DAYS:
        # 补捞：首发时被低估，之后出现了强信号
        signals = item.get("signals") or {}
        has_signal = (
            (signals.get("hf_upvotes") or 0) > 0
            or (signals.get("citation_count") or 0) > 0
            or bool((item.get("links") or {}).get("code"))
        )
        return "catchup" if has_signal else "stale"

    updated_age = days_since(parse_dt(item.get("updated_at")), now)
    if (
        updated_age is not None
        and updated_age <= PRIMARY_DAYS
        and (item.get("version_count") or 1) >= 2
    ):
        # 重大修订：前端必须标注版本号，不得伪装成新论文
        return "revision"

    return "stale"


# ------------------------------------------------------------ 主题标签


def _assign_topics(item: dict) -> tuple[list[str], str]:
    """按关键词命中强度排序取前 N 个主题，全部未命中才交 L1 兜底。

    不返回所有命中项：`any` 规则很宽，一篇论文顺带提到 attention、benchmark、
    reinforcement learning 就能挂上四五个主题，阶段五的"单主题不超过 4 篇"
    会被这种泛标签立刻耗尽（实测 120 条里 31 条因此落选）。标题命中按两倍
    计权——出现在标题里的词才是这篇在做的事。
    """
    cfg = load_config("topics")
    title = (item.get("title") or "").lower()
    haystack = " ".join([
        title,
        (item.get("abstract") or "").lower(),
        " ".join(item.get("topics_hint") or []).lower(),
    ])
    scored: list[tuple[float, str]] = []
    for topic in cfg["topics"]:
        keywords = topic.get("any") or []
        in_body = sum(1 for kw in keywords if _kw_hit(kw, haystack))
        in_title = sum(1 for kw in keywords if _kw_hit(kw, title))
        # title_any 里放的是泛用词（benchmark、evaluation 这类），只在标题里
        # 出现才算该主题——落在摘要里几乎每篇都有，会把主题标成同一个
        title_only = sum(1 for kw in (topic.get("title_any") or [])
                         if _kw_hit(kw, title))
        if not in_body and not title_only:
            continue
        weight = float(topic.get("boost") or 1.0)
        scored.append(((in_body + 2.0 * (in_title + title_only)) * weight,
                       topic["name"]))
    if not scored:
        return [], "pending_llm"
    scored.sort(key=lambda row: (-row[0], row[1]))
    limit = max(int(cfg.get("max_topics_per_item") or 2), 1)
    return [name for _, name in scored[:limit]], "rule"


_kw_patterns: dict[str, Any] = {}


def _kw_hit(keyword: str, text: str) -> bool:
    """按词边界匹配，而不是子串包含。

    "moe" 会命中 moment、"vla" 会命中 vlad、"agent" 会命中 reagent，
    "llm" 这类缩写尤其危险。首尾按字符类型决定是否加 \\b：keyword 以字母
    数字开头/结尾才加，否则（如 "text-to-3d" 的尾部）会永远匹配不上。
    """
    pattern = _kw_patterns.get(keyword)
    if pattern is None:
        body = re.escape(keyword.lower())
        prefix = r"\b" if keyword[:1].isalnum() else ""
        suffix = r"\b" if keyword[-1:].isalnum() else ""
        pattern = re.compile(f"{prefix}{body}{suffix}")
        _kw_patterns[keyword] = pattern
    return bool(pattern.search(text))


# ------------------------------------------------------------ 源映射


def _blank_signals() -> dict[str, Any]:
    return {"hf_upvotes": 0, "hf_num_comments": 0, "hf_trending": False,
            "citation_count": None, "influential_citation_count": None,
            "reference_count": None, "venue": None, "work_type": None,
            "fwci": None, "citation_percentile": None, "citations_by_year": None,
            "repo_stars": None, "repo_stars_7d": None, "repo_stars_30d": None}


def _from_arxiv(raw: dict, batch: date) -> dict[str, Any]:
    return {
        "id": f"arxiv-{raw['arxiv_id']}",
        "content_type": "paper",
        "source": ["arxiv"],
        "arxiv_id": raw["arxiv_id"],
        "arxiv_version": raw.get("arxiv_version"),
        "version_count": raw.get("version_count"),
        "title": raw.get("title"),
        "title_zh": None,
        "authors": raw.get("authors") or [],
        "abstract": raw.get("abstract"),
        "published_at": raw.get("published_at"),
        "updated_at": raw.get("updated_at"),
        "arxiv_announce_date": batch.isoformat(),
        "primary_category": raw.get("primary_category"),
        "categories": raw.get("categories") or [],
        "category_tier": raw.get("category_tier"),
        "comment": raw.get("comment"),
        "doi": raw.get("doi"),
        "journal_ref": raw.get("journal_ref"),
        "license": raw.get("license"),
        "links": dict(raw.get("links") or {}),
        "signals": _blank_signals(),
        "topics_hint": [],
    }


def _from_huggingface(raw: dict, batch: date) -> dict[str, Any]:
    """HF 收录但不在本次 arXiv 窗口内的论文，以 HF 字段建条目。"""
    arxiv_id = raw["arxiv_id"]
    signals = _blank_signals()
    signals.update({
        "hf_upvotes": raw.get("hf_upvotes") or 0,
        "hf_num_comments": raw.get("hf_num_comments") or 0,
        "hf_trending": True,
        "repo_stars": raw.get("repo_stars_hf"),
    })
    return {
        "id": f"arxiv-{arxiv_id}",
        "content_type": "paper",
        "source": ["huggingface"],
        "arxiv_id": arxiv_id,
        "arxiv_version": None,
        "version_count": None,
        "title": raw.get("title"),
        "title_zh": None,
        "authors": raw.get("authors") or [],
        "abstract": raw.get("abstract"),
        "published_at": raw.get("published_at"),
        "updated_at": None,
        "arxiv_announce_date": batch.isoformat(),
        "primary_category": None,
        "categories": [],
        "category_tier": None,
        "comment": None,
        "doi": None,
        "journal_ref": None,
        "license": None,
        "links": {
            "paper": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
            "code": _repo_url(raw.get("links", {}).get("code")),
            "project": raw.get("links", {}).get("project"),
        },
        "signals": signals,
        "topics_hint": raw.get("hf_ai_keywords") or [],
    }


def _repo_url(repo: Any) -> str | None:
    """HF 的 githubRepo 可能是 'owner/name' 或完整 URL。"""
    if not repo:
        return None
    value = str(repo)
    if value.startswith("http"):
        return value
    return f"https://github.com/{value.strip('/')}"


def _merge_huggingface(target: dict, raw: dict) -> None:
    if "huggingface" not in target["source"]:
        target["source"].append("huggingface")
    target["signals"].update({
        "hf_upvotes": max(target["signals"].get("hf_upvotes") or 0,
                          raw.get("hf_upvotes") or 0),
        "hf_num_comments": max(target["signals"].get("hf_num_comments") or 0,
                               raw.get("hf_num_comments") or 0),
        "hf_trending": True,
    })
    if raw.get("repo_stars_hf") is not None:
        target["signals"]["repo_stars"] = raw["repo_stars_hf"]
    # 补齐 arXiv 侧缺失的链接，不覆盖已有值
    links = raw.get("links") or {}
    if not target["links"].get("code"):
        target["links"]["code"] = _repo_url(links.get("code"))
    if not target["links"].get("project"):
        target["links"]["project"] = links.get("project")
    for keyword in raw.get("hf_ai_keywords") or []:
        if keyword not in target["topics_hint"]:
            target["topics_hint"].append(keyword)


def _merge_repo(target: dict, raw: dict) -> None:
    """论文的官方代码仓库合并进论文条目，不独立占用推荐名额。"""
    if "github" not in target["source"]:
        target["source"].append("github")
    if not target["links"].get("code"):
        target["links"]["code"] = raw["links"].get("code")
    if not target["links"].get("project"):
        target["links"]["project"] = raw["links"].get("project")
    target["signals"].update({
        "repo_stars": raw.get("stars"),
        "repo_stars_7d": raw.get("stars_7d"),
        "repo_stars_30d": raw.get("stars_30d"),
    })
    target["repo"] = {
        "full_name": raw.get("full_name"),
        "license": raw.get("license"),
        "language": raw.get("language"),
        "pushed_at": raw.get("pushed_at"),
        "is_trending": raw.get("is_trending"),
    }


def _from_repo(raw: dict, batch: date) -> dict[str, Any]:
    """无关联论文的开源项目，作为独立 repo 条目。"""
    signals = _blank_signals()
    signals.update({
        "repo_stars": raw.get("stars"),
        "repo_stars_7d": raw.get("stars_7d"),
        "repo_stars_30d": raw.get("stars_30d"),
    })
    return {
        "id": f"repo-{raw['full_name'].replace('/', '--')}",
        "content_type": "repo",
        "source": ["github"],
        "arxiv_id": None,
        "title": raw.get("full_name"),
        "title_zh": None,
        "authors": [raw.get("owner")] if raw.get("owner") else [],
        "abstract": raw.get("description"),
        "published_at": raw.get("created_at"),
        "updated_at": raw.get("pushed_at"),
        "arxiv_announce_date": batch.isoformat(),
        "primary_category": None,
        "categories": [],
        "links": dict(raw.get("links") or {}),
        "signals": signals,
        "topics_hint": raw.get("topics") or [],
        "repo": {
            "full_name": raw.get("full_name"),
            "license": raw.get("license"),
            "language": raw.get("language"),
            "pushed_at": raw.get("pushed_at"),
            "stars": raw.get("stars"),
            "forks": raw.get("forks"),
            "is_trending": raw.get("is_trending"),
            "trending_windows": raw.get("trending_windows") or [],
            "snapshot_days": raw.get("snapshot_days"),
        },
    }

