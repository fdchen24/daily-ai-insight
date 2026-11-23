"""跨源去重：arXiv ID → DOI → 标题相似度 → 项目/仓库地址。

标题相似度用归一化后的 token set Jaccard：≥ 0.90 判为同一论文并合并，
0.75–0.90 只记入复核日志而不自动合并（v1.0 只写"相似度"，无法实现）。
"""

from __future__ import annotations

from typing import Any

from .common import jaccard, log, title_tokens

MERGE_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.75


def deduplicate(items: list[dict]) -> dict[str, Any]:
    """返回 {"items", "stats", "review"}；review 为需人工复核的相似对。"""
    stats = {"input": len(items), "by_doi": 0, "by_title": 0,
             "by_project": 0, "output": 0}
    review: list[dict[str, Any]] = []

    # 先按得分潜力排序，保留信息更完整的一条作为主条目
    ordered = sorted(items, key=_completeness, reverse=True)

    kept: list[dict] = []
    doi_index: dict[str, dict] = {}
    project_index: dict[str, dict] = {}
    token_index: list[tuple[frozenset[str], dict]] = []

    for item in ordered:
        doi = (item.get("doi") or "").strip().lower()
        if doi and doi in doi_index:
            _merge(doi_index[doi], item)
            stats["by_doi"] += 1
            continue

        project_key = _project_key(item)
        if project_key and project_key in project_index:
            _merge(project_index[project_key], item)
            stats["by_project"] += 1
            continue

        tokens = title_tokens(item.get("title"))
        duplicate = None
        near_misses: list[dict[str, Any]] = []
        for other_tokens, other in token_index:
            score = jaccard(tokens, other_tokens)
            if score >= MERGE_THRESHOLD:
                duplicate = other
                break
            if score >= REVIEW_THRESHOLD:
                near_misses.append({
                    "score": round(score, 3),
                    "kept": other.get("id"),
                    "candidate": item.get("id"),
                    "kept_title": other.get("title"),
                    "candidate_title": item.get("title"),
                })
        if duplicate is not None:
            _merge(duplicate, item)
            stats["by_title"] += 1
            continue
        # 只有在未被合并时，相近对才值得人工复核
        review.extend(near_misses)

        kept.append(item)
        if doi:
            doi_index[doi] = item
        if project_key:
            project_index[project_key] = item
        if tokens:
            token_index.append((tokens, item))

    stats["output"] = len(kept)
    stats["review_pairs"] = len(review)
    log.info("去重：%d → %d（DOI %d，标题 %d，项目 %d），待复核 %d 对",
             stats["input"], stats["output"], stats["by_doi"],
             stats["by_title"], stats["by_project"], len(review))
    return {"items": kept, "stats": stats, "review": review}


def _completeness(item: dict) -> tuple:
    """信息更全、信号更强的条目优先作为合并主体。"""
    signals = item.get("signals") or {}
    return (
        len(item.get("source") or []),
        1 if item.get("abstract") else 0,
        1 if item.get("arxiv_id") else 0,
        signals.get("hf_upvotes") or 0,
        signals.get("citation_count") or 0,
    )


def _project_key(item: dict) -> str | None:
    links = item.get("links") or {}
    for field in ("code", "project"):
        value = links.get(field)
        if value:
            return str(value).rstrip("/").lower()
    return None


def _merge(target: dict, other: dict) -> None:
    """并集合并：补齐缺失字段、合并来源、取更强的信号值。"""
    for source in other.get("source") or []:
        if source not in target["source"]:
            target["source"].append(source)

    for field in ("abstract", "doi", "journal_ref", "license", "comment",
                  "primary_category", "arxiv_id", "arxiv_version",
                  "published_at", "updated_at", "s2_tldr", "repo"):
        if not target.get(field) and other.get(field):
            target[field] = other[field]

    target_links = target.setdefault("links", {})
    for field, value in (other.get("links") or {}).items():
        if value and not target_links.get(field):
            target_links[field] = value

    target_signals = target.setdefault("signals", {})
    for field, value in (other.get("signals") or {}).items():
        if value is None:
            continue
        current = target_signals.get(field)
        if current is None:
            target_signals[field] = value
        elif isinstance(value, bool):
            target_signals[field] = current or value
        elif isinstance(value, (int, float)) and isinstance(current, (int, float)):
            target_signals[field] = max(current, value)

    for field in ("categories", "authors", "topics_hint"):
        merged = list(target.get(field) or [])
        for value in other.get(field) or []:
            if value not in merged:
                merged.append(value)
        target[field] = merged

    target.setdefault("merged_from", []).append(other.get("id"))

