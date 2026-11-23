"""Hugging Face Papers 采集：提供社区热度信号。

依赖未文档化的 /api/daily_papers 端点（`huggingface_hub` 并未封装该功能）。
本源失败只跳过热度信号，不中断流水线。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..common import (
    HttpClient,
    SourceError,
    iso,
    load_config,
    log,
    parse_dt,
    split_arxiv_id,
)


def collect(batch: date, *, lookback_days: int | None = None) -> dict[str, Any]:
    """逐日拉取 daily papers，返回按 arxiv_id 去重的热度信号列表。"""
    cfg = load_config("sources")["huggingface"]
    if not cfg.get("enabled", True):
        return {"ok": True, "items": [], "stats": {"skipped": True}}

    lookback = lookback_days if lookback_days is not None else cfg["lookback_days"]
    days = [batch - timedelta(days=offset) for offset in range(lookback + 1)]
    stats: dict[str, Any] = {"by_date": {}, "host": None, "requests": 0}
    items: dict[str, dict[str, Any]] = {}

    client = HttpClient(
        interval=cfg["request_interval_seconds"],
        timeout=cfg["timeout_seconds"],
        max_retries=cfg["max_retries"],
    )
    try:
        host = _pick_host(client, cfg, days[0], stats)
        for day in days:
            payload = _fetch_day(client, host, cfg, day, stats)
            if payload is None:
                continue
            stats["by_date"][day.isoformat()] = len(payload)
            for entry in payload:
                parsed = _parse_entry(entry, day)
                if parsed is None:
                    continue
                # 同一论文可能连续多日上榜，保留 upvotes 最高的一条
                prior = items.get(parsed["arxiv_id"])
                if prior is None or parsed["hf_upvotes"] > prior["hf_upvotes"]:
                    items[parsed["arxiv_id"]] = parsed
    except SourceError as exc:
        log.error("Hugging Face 采集失败: %s", exc)
        return {"ok": False, "items": list(items.values()),
                "stats": stats, "error": str(exc)}
    finally:
        client.close()

    log.info("Hugging Face: %d 篇（%s）", len(items), stats["host"])
    return {"ok": True, "items": list(items.values()), "stats": stats}


def _pick_host(client: HttpClient, cfg: dict, probe_day: date,
               stats: dict) -> str:
    """按序试探配置的 host，返回第一个可达的。"""
    errors = []
    for host in cfg["hosts"]:
        url = f"{host}{cfg['daily_papers_path']}"
        try:
            resp = client.get(url, params={"date": probe_day.isoformat()})
            stats["requests"] += 1
            if resp.status_code == 200:
                stats["host"] = host
                return host
            errors.append(f"{host} HTTP {resp.status_code}")
        except SourceError as exc:
            errors.append(f"{host} {exc}")
            log.warning("HF host 不可达，尝试下一个: %s", host)
    raise SourceError("所有 HF host 均不可达: " + "; ".join(errors))


def _fetch_day(client: HttpClient, host: str, cfg: dict, day: date,
               stats: dict) -> list[dict] | None:
    url = f"{host}{cfg['daily_papers_path']}"
    try:
        resp = client.get(url, params={"date": day.isoformat()})
        stats["requests"] += 1
    except SourceError as exc:
        log.warning("HF %s 拉取失败，跳过该日: %s", day, exc)
        return None
    if resp.status_code != 200:
        log.warning("HF %s 返回 %d，跳过该日", day, resp.status_code)
        return None
    try:
        payload = resp.json()
    except ValueError:
        log.warning("HF %s 返回非 JSON，跳过该日", day)
        return None
    # 周末常返回空数组，属正常情况
    return payload if isinstance(payload, list) else None


def _parse_entry(entry: dict, listed_on: date) -> dict[str, Any] | None:
    paper = entry.get("paper") or {}
    raw_id = paper.get("id") or ""
    arxiv_id, _ = split_arxiv_id(str(raw_id))
    if not arxiv_id:
        return None

    authors = [
        author.get("name")
        for author in paper.get("authors") or []
        if isinstance(author, dict) and author.get("name")
    ]
    repo = paper.get("githubRepo") or None
    return {
        "source": "huggingface",
        "arxiv_id": arxiv_id,
        "title": paper.get("title") or entry.get("title"),
        "abstract": paper.get("summary") or entry.get("summary"),
        "authors": authors,
        "published_at": iso(parse_dt(paper.get("publishedAt"))),
        "hf_listed_on": listed_on.isoformat(),
        "hf_upvotes": int(paper.get("upvotes") or 0),
        "hf_num_comments": int(entry.get("numComments") or 0),
        "hf_submitted_by": (entry.get("submittedBy") or {}).get("user"),
        "hf_organization": paper.get("organization") or entry.get("organization"),
        "links": {
            "code": repo,
            "project": paper.get("projectPage") or None,
        },
        "repo_stars_hf": paper.get("githubStars"),
        "hf_ai_keywords": paper.get("ai_keywords") or [],
    }

