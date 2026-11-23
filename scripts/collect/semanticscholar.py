"""Semantic Scholar 采集：客观学术影响力信号（引用数、TLDR）。

用途：为"经典"和"综述"两类提供零 LLM 成本的硬信号，并作为前沿补捞
通道的触发条件之一。本源失败只降级为缺少学术信号，不中断流水线。
"""

from __future__ import annotations

from typing import Any, Iterable

from ..common import HttpClient, SourceError, env, load_config, log

_BATCH_LIMIT = 500


def collect(arxiv_ids: Iterable[str]) -> dict[str, Any]:
    """按 arXiv ID 批量回填引用数据，返回 {arxiv_id: signals}。"""
    cfg = load_config("sources")["semanticscholar"]
    ids = [str(i) for i in dict.fromkeys(arxiv_ids) if i]
    if not cfg.get("enabled", True) or not ids:
        return {"ok": True, "items": {}, "stats": {"skipped": True}}

    api_key = env("S2_API_KEY")
    if cfg.get("require_api_key") and not api_key:
        log.info("未设置 S2_API_KEY，跳过 S2（引用信号以 OpenAlex 为准）")
        return {"ok": True, "items": {}, "stats": {"skipped": "no_api_key"}}
    headers = {"x-api-key": api_key} if api_key else {}
    if not api_key:
        log.warning("未设置 S2_API_KEY，限速约 100 次/5 分钟，可能部分失败")

    batch_size = min(int(cfg.get("batch_size") or 100), _BATCH_LIMIT)
    fields = ",".join(cfg["fields"])
    stats: dict[str, Any] = {"requested": len(ids), "resolved": 0,
                             "batches": 0, "failed_batches": 0,
                             "authenticated": bool(api_key)}
    signals: dict[str, dict[str, Any]] = {}

    client = HttpClient(
        interval=cfg["request_interval_seconds"],
        timeout=cfg["timeout_seconds"],
        max_retries=cfg["max_retries"],
        headers=headers,
    )
    try:
        for start in range(0, len(ids), batch_size):
            chunk = ids[start:start + batch_size]
            resolved = _fetch_batch(client, cfg, fields, chunk, stats)
            signals.update(resolved)
    finally:
        client.close()
        stats["dropped_ids"] = stats.get("dropped_ids") or []

    stats["resolved"] = len(signals)
    stats["unindexed"] = stats.get("unindexed", 0)
    # 分母扣掉 S2 尚未入库的论文：当天公告的 arXiv 论文必然查不到，
    # 把它们算进失败率只会掩盖真正的接口问题。
    reachable = len(ids) - stats["unindexed"]
    coverage = len(signals) / reachable if reachable > 0 else 1.0
    log.info("Semantic Scholar: %d/%d 命中（已入库口径 %.0f%%，未入库 %d），%d 批失败",
             len(signals), len(ids), coverage * 100,
             stats["unindexed"], stats["failed_batches"])
    stats["coverage_indexed"] = round(coverage, 3)
    return {"ok": stats["failed_batches"] < stats["batches"] or not ids,
            "items": signals, "stats": stats}


def _fetch_batch(client: HttpClient, cfg: dict, fields: str,
                 chunk: list[str], stats: dict) -> dict[str, dict[str, Any]]:
    """POST /paper/batch 一次最多 500 个 ID，比逐条 GET 省掉两个数量级请求。

    S2 对整批请求做校验，400 分两种情况：
    - `No valid paper ids given`：批内没有任何一个 ID 命中索引。S2 对当天
      公告的 arXiv 论文有数天入库延迟，而候选池按公告日连续排列，所以
      "整批都是新论文"很常见。这种 400 没有可救的部分，直接整批记为未命中；
      若折半重试，一个 100 条的新论文批次会放大成 200+ 次请求。
    - 其他 400：可能只是批内个别 ID 格式异常，折半把损失收敛到那一个。
    """
    stats["batches"] += 1
    url = f"{cfg['api_base']}/paper/batch"
    try:
        resp = client.post(
            url,
            params={"fields": fields},
            json={"ids": [f"arXiv:{i}" for i in chunk]},
        )
    except SourceError as exc:
        stats["failed_batches"] += 1
        log.warning("S2 批次失败（%d 个 ID），跳过: %s", len(chunk), exc)
        return {}
    if resp.status_code == 400:
        body = resp.text[:200]
        if "No valid paper ids" in body:
            stats["unindexed"] = stats.get("unindexed", 0) + len(chunk)
            log.debug("S2 批次 %d 个 ID 均未入库，跳过", len(chunk))
            return {}
        if len(chunk) > 1:
            half = len(chunk) // 2
            log.info("S2 批次 400（%d 个 ID），折半重试: %s", len(chunk), body)
            out = _fetch_batch(client, cfg, fields, chunk[:half], stats)
            out.update(_fetch_batch(client, cfg, fields, chunk[half:], stats))
            return out
    if resp.status_code != 200:
        stats["failed_batches"] += 1
        if len(chunk) == 1:
            stats.setdefault("dropped_ids", []).append(chunk[0])
        log.warning("S2 批次返回 %d，跳过 %d 个 ID: %s",
                    resp.status_code, len(chunk), resp.text[:200])
        return {}
    try:
        payload = resp.json()
    except ValueError:
        stats["failed_batches"] += 1
        return {}
    if not isinstance(payload, list):
        stats["failed_batches"] += 1
        return {}

    # 返回数组与请求顺序一致，未入库的位置为 null —— 与整批 400 同一回事，
    # 计入 unindexed 才能让命中率的分母只剩"S2 确实有的论文"
    out: dict[str, dict[str, Any]] = {}
    for arxiv_id, record in zip(chunk, payload):
        if not isinstance(record, dict):
            stats["unindexed"] = stats.get("unindexed", 0) + 1
            continue
        tldr = record.get("tldr") or {}
        venue = record.get("publicationVenue") or {}
        out[arxiv_id] = {
            "citation_count": record.get("citationCount"),
            "influential_citation_count": record.get("influentialCitationCount"),
            "reference_count": record.get("referenceCount"),
            "fields_of_study": record.get("fieldsOfStudy") or [],
            "venue": venue.get("name") if isinstance(venue, dict) else None,
            "year": record.get("year"),
            "tldr": tldr.get("text") if isinstance(tldr, dict) else None,
        }
    return out

