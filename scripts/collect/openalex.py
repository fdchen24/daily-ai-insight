"""OpenAlex 采集：客观学术信号（引用数、参考文献数、发表状态、引用速度）。

替代 Semantic Scholar 作为引用信号主源，原因是它不需要 API key：UA 里带
`mailto:` 即进入 polite pool，额度 10 万次/天、10 次/秒，一次请求可查 50 篇。
实测同一批论文的命中率（3–30 天龄）为 100%，而 S2 无 key 时会被限速打断。

arXiv 自 2022 年起给每篇论文分配 DataCite DOI（`10.48550/arxiv.<id>`），
因此可直接用 `filter=doi:a|b|c` 批量查询，不必逐条解析。

查询结果写入 `data/state/citations/<论文年月>.json` 缓存：相邻两天的
lookback 窗口有约 75% 的 ID 重复，缓存后稳定态每天只剩十几次请求。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from ..common import (
    HttpClient,
    SourceError,
    citations_path,
    env,
    iso,
    load_config,
    log,
    parse_dt,
    read_json,
    utcnow,
    write_json,
)

_DOI_PREFIX = "10.48550/arxiv."
# 缓存条目的结构版本。加字段（如 institutions）后旧条目缺的值靠 TTL 补要等
# 到 30 天后，老论文尤其慢；版本号一改即全部按过期处理，重查一次补齐。
# 空值在落盘时会被剔掉，所以不能靠"某个键在不在"判断，只能显式记版本。
_CACHE_VERSION = 2
_META_KEYS = ("fetched_at", "v")


def collect(arxiv_ids: Iterable[str], *, now: datetime | None = None) -> dict[str, Any]:
    """按 arXiv ID 批量取学术信号，返回 {arxiv_id: signals}。

    命中缓存且未过期的不发请求；失败只降级为缺少该条信号，不抛出。
    """
    cfg = load_config("sources")["openalex"]
    ids = [str(i) for i in dict.fromkeys(arxiv_ids) if i]
    if not cfg.get("enabled", True) or not ids:
        return {"ok": True, "items": {}, "stats": {"skipped": True}}

    now = now or utcnow()
    cache, shards = _load_cache(ids)
    signals: dict[str, dict[str, Any]] = {}
    stale: list[str] = []
    for arxiv_id in ids:
        entry = cache.get(arxiv_id)
        if entry and not _expired(entry, arxiv_id, now, cfg):
            signals[arxiv_id] = {k: v for k, v in entry.items()
                                 if k not in _META_KEYS}
        else:
            stale.append(arxiv_id)

    stats: dict[str, Any] = {
        "requested": len(ids), "from_cache": len(signals),
        "queried": len(stale), "requests": 0, "resolved": 0,
        "unindexed": 0, "failed_batches": 0, "batches": 0,
    }
    if not stale:
        log.info("OpenAlex: %d 条全部命中缓存，未发请求", len(signals))
        return {"ok": True, "items": signals, "stats": stats}

    batch_size = max(1, min(int(cfg.get("batch_size") or 50), 50))
    fetched: dict[str, dict[str, Any]] = {}
    # mailto 决定 polite pool 身份，fork 本仓库的人应当换成自己的地址：
    # 配成仓库 Variable `OPENALEX_MAILTO` 即可，不必改配置文件
    mailto = env("OPENALEX_MAILTO") or cfg["mailto"]
    client = HttpClient(
        interval=cfg["request_interval_seconds"],
        timeout=cfg["timeout_seconds"],
        max_retries=cfg["max_retries"],
        headers={"User-Agent": f"DailyAIInsight/2.0 (mailto:{mailto})"},
    )
    try:
        for start in range(0, len(stale), batch_size):
            chunk = stale[start:start + batch_size]
            fetched.update(_fetch_batch(client, cfg, chunk, stats))
    finally:
        client.close()

    stats["resolved"] = len(fetched)
    stats["unindexed"] = len(stale) - len(fetched)
    signals.update(fetched)
    if fetched:
        _save_cache(fetched, shards, now)

    log.info("OpenAlex: %d 条（缓存 %d，新查 %d → 命中 %d，未命中 %d），%d 次请求",
             len(signals), stats["from_cache"], len(stale), len(fetched),
             stats["unindexed"], stats["requests"])
    # 响应里缺 DOI 的条目既可能是 OpenAlex 没收录，也可能是刚公告还没索引，
    # 两者无法区分，所以分母用本次实际发问的条数。当天公告的新论文任何引用源
    # 都查不到，因此这个比率天然偏低，不作为失败判据（ok 只看失败批次）。
    stats["hit_rate"] = round(len(fetched) / len(stale), 3) if stale else 1.0
    return {"ok": stats["failed_batches"] < max(stats["batches"], 1),
            "items": signals, "stats": stats}


# ------------------------------------------------------------ 批量查询


def _fetch_batch(client: HttpClient, cfg: dict, chunk: list[str],
                 stats: dict) -> dict[str, dict[str, Any]]:
    """一次请求查 50 篇。返回顺序与请求无关，按响应里的 DOI 回映射。"""
    stats["batches"] += 1
    params = {
        "filter": "doi:" + "|".join(f"{_DOI_PREFIX}{i}" for i in chunk),
        "select": ",".join(cfg["select"]),
        "per-page": len(chunk),
    }
    try:
        resp = client.get(f"{cfg['api_base']}/works", params=params)
        stats["requests"] += 1
    except SourceError as exc:
        stats["failed_batches"] += 1
        log.warning("OpenAlex 批次失败（%d 个 ID），跳过: %s", len(chunk), exc)
        return {}
    if resp.status_code != 200:
        stats["failed_batches"] += 1
        log.warning("OpenAlex 批次返回 %d，跳过 %d 个 ID: %s",
                    resp.status_code, len(chunk), resp.text[:200])
        return {}
    try:
        results = resp.json().get("results") or []
    except ValueError:
        stats["failed_batches"] += 1
        return {}

    out: dict[str, dict[str, Any]] = {}
    for work in results:
        arxiv_id = _arxiv_id_from_doi(work.get("doi"))
        if arxiv_id:
            out[arxiv_id] = _parse_work(work)
    return out


def _arxiv_id_from_doi(doi: str | None) -> str | None:
    """`https://doi.org/10.48550/arxiv.2611.26444` → `2611.26444`。"""
    if not doi:
        return None
    lowered = doi.lower()
    index = lowered.find(_DOI_PREFIX)
    if index < 0:
        return None
    return lowered[index + len(_DOI_PREFIX):].strip("/") or None


def _parse_work(work: dict) -> dict[str, Any]:
    source = (work.get("primary_location") or {}).get("source") or {}
    percentile = work.get("citation_normalized_percentile") or {}
    fwci = work.get("fwci")
    # counts_by_year 只留最近两年：判"经典"要看现在还在不在被引，而不是
    # 历史总量；全量年份表会让缓存分片无谓变大。
    counts = sorted((work.get("counts_by_year") or []),
                    key=lambda row: row.get("year") or 0, reverse=True)[:2]
    parsed = {
        "citation_count": work.get("cited_by_count"),
        "reference_count": work.get("referenced_works_count"),
        "venue": source.get("display_name"),
        "year": work.get("publication_year"),
        # preprint / conference-paper / article：能区分"还是预印本"和"已发表"
        "work_type": work.get("type"),
        "fwci": round(fwci, 2) if isinstance(fwci, (int, float)) else None,
        "citation_percentile": (round(percentile["value"], 3)
                                if isinstance(percentile.get("value"), (int, float))
                                else None),
        "citations_by_year": {str(row["year"]): row.get("cited_by_count")
                              for row in counts if row.get("year")},
        "oa_topics": [t.get("display_name") for t in (work.get("topics") or [])[:2]
                      if t.get("display_name")],
        "institutions": _institutions(work.get("authorships") or []),
    }
    # 缓存按条目逐行落盘，空值占的体积不小，一律去掉
    return {k: v for k, v in parsed.items() if v not in (None, {}, [])}


def _institutions(authorships: list[dict]) -> list[str]:
    """按作者顺序取去重后的单位名，最多 5 个。

    优先用 OpenAlex 消歧后的 `institutions[].display_name`；预印本常常没消歧
    成功，这时退回 `raw_affiliation_strings`——但那是 PDF 首页抽出来的原文，
    会带进 "Duke University Project Page : https://..." 这类噪声，所以只收
    短且不含链接邮箱的片段，宁可留空也不往页面上写脏数据。
    """
    names: list[str] = []
    for authorship in authorships:
        picked = [inst.get("display_name")
                  for inst in (authorship.get("institutions") or [])]
        if not any(picked):
            picked = [_clean_affiliation(raw)
                      for raw in (authorship.get("raw_affiliation_strings") or [])[:1]]
        for name in picked:
            if name and name not in names:
                names.append(name)
        if len(names) >= 5:
            break
    return names[:5]


def _clean_affiliation(raw: str | None) -> str | None:
    text = " ".join((raw or "").split()).strip(" ,.;·")
    if not text or len(text) > 50 or "http" in text or "@" in text:
        return None
    return text


# ------------------------------------------------------------ 缓存


def _paper_month(arxiv_id: str) -> str:
    """从 arXiv ID 前缀推论文年月：`2611.26444` → `2026-11`。

    2007 年 4 月以前的旧式 ID（如 `cs.LG/0701001`）没有这个结构，
    统一归到 legacy 分片。
    """
    head = arxiv_id.split(".")[0]
    if len(head) == 4 and head.isdigit():
        year = 2000 + int(head[:2])
        month = int(head[2:])
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    return "legacy"


def _load_cache(ids: list[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    """只读请求涉及的分片，返回 (合并视图, 分片原文)。"""
    shards: dict[str, dict] = {}
    merged: dict[str, dict] = {}
    for month in {_paper_month(i) for i in ids}:
        shard = read_json(citations_path(month), default={}) or {}
        shards[month] = shard
        merged.update(shard)
    return merged, shards


def _save_cache(fetched: dict[str, dict], shards: dict[str, dict],
                now: datetime) -> None:
    stamp = iso(now)
    touched: set[str] = set()
    for arxiv_id, signals in fetched.items():
        month = _paper_month(arxiv_id)
        shards.setdefault(month, {})[arxiv_id] = {**signals, "fetched_at": stamp,
                                                  "v": _CACHE_VERSION}
        touched.add(month)
    for month in touched:
        write_json(citations_path(month), shards[month], indent=0)


def _expired(entry: dict, arxiv_id: str, now: datetime, cfg: dict) -> bool:
    if entry.get("v") != _CACHE_VERSION:
        return True
    fetched_at = parse_dt(entry.get("fetched_at"))
    if fetched_at is None:
        return True
    month = _paper_month(arxiv_id)
    cutoff = now.date() - timedelta(days=31 * int(cfg["recent_months"]))
    # legacy 是 2007 年以前的旧式 ID，一律按老论文处理
    is_recent = month != "legacy" and month >= f"{cutoff:%Y-%m}"
    ttl = cfg["cache_ttl_recent_days"] if is_recent else cfg["cache_ttl_old_days"]
    return (now - fetched_at) > timedelta(days=int(ttl))
