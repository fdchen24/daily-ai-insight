"""阶段一入口：多源采集 → 规范化 → 去重 → 候选池落盘。

用法：
    python -m scripts.collect_all                  # 当期批次
    python -m scripts.collect_all --date 2026-11-20 --lookback 1
    python -m scripts.collect_all --no-github      # 无 token 时跳过 GitHub
    python -m scripts.collect_all --from-raw       # 用已存的 raw 重建候选池

设计要点：单源失败只记入 metrics 并跳过，不中断流水线；按
arxiv_announce_date 幂等覆盖，重跑不追加。

`--from-raw` 是改规则后的重算通道：主题词表、补捞阈值、去重参数都在
normalize/deduplicate 里，调完它们必须让新标签进到候选池，但不该为此再抓
一遍上千篇论文（arXiv OAI 一次全量要几分钟且会超时）。raw 分目录按批次日
保存了三源原始响应，这里直接读回来重跑阶段二。学术信号仍走 openalex 的
月度缓存，命中就不发请求。
"""

from __future__ import annotations

import argparse
import time
from datetime import date
from typing import Any

from . import deduplicate as dedup_mod
from . import normalize as norm_mod
from .collect import arxiv, github, huggingface, openalex, semanticscholar
from .common import (
    DATA_DIR,
    announce_date,
    env,
    env_flag,
    env_int,
    iso,
    log,
    metrics_path,
    raw_path,
    read_json,
    setup_logging,
    utcnow,
    write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily AI Insight 阶段一采集")
    parser.add_argument("--date", help="arXiv 公告批次日 YYYY-MM-DD")
    parser.add_argument("--lookback", type=int, default=env_int("DAI_LOOKBACK"),
                        help="覆盖各源的回溯天数")
    parser.add_argument("--no-github", action="store_true",
                        default=env_flag("DAI_SKIP_GITHUB"))
    parser.add_argument("--no-citations", action="store_true",
                        default=env_flag("DAI_SKIP_CITATIONS"),
                        help="跳过 OpenAlex/S2 学术信号")
    parser.add_argument("--no-baseline", action="store_true",
                        default=env_flag("DAI_SKIP_BASELINE"),
                        help="跳过 GitHub 基线池，只走 trending 通道")
    parser.add_argument("--from-raw", action="store_true",
                        help="不联网，用 data/raw/<批次>/ 已存响应重跑规范化与去重")
    parser.add_argument("--log-level", default=env("DAI_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    setup_logging(args.log_level)
    batch = announce_date(args.date)
    started = time.monotonic()
    log.info("=== 批次 %s 开始采集 ===", batch)
    return run(batch, args, started)


def run(batch: date, args: argparse.Namespace, started: float) -> int:
    sources: dict[str, dict[str, Any]] = {}

    if args.from_raw:
        arxiv_items, hf_items, github_items = _load_raw(batch, sources)
        if not arxiv_items and not hf_items and not github_items:
            log.error("raw 目录为空：%s（先跑一次联网采集）",
                      raw_path(batch, "arxiv").parent)
            return 1
    else:
        # --- arXiv（主内容源，失败则整期无意义，但仍不抛出）
        result = arxiv.collect(batch, lookback_days=args.lookback)
        sources["arxiv"] = _source_meta(result)
        arxiv_items = result["items"]
        write_json(raw_path(batch, "arxiv"), arxiv_items, indent=0)

        # --- Hugging Face（热度信号，可缺）
        result = huggingface.collect(batch, lookback_days=args.lookback)
        sources["huggingface"] = _source_meta(result)
        hf_items = result["items"]
        write_json(raw_path(batch, "huggingface"), hf_items, indent=0)

        # --- GitHub（项目源，可缺）
        github_items = []
        if args.no_github:
            sources["github"] = {"ok": True, "count": 0, "skipped": True}
            log.info("按参数跳过 GitHub")
        else:
            result = github.collect(batch, build_baseline=not args.no_baseline)
            sources["github"] = _source_meta(result)
            github_items = result["items"]
            write_json(raw_path(batch, "github"), github_items, indent=0)

    # --- 学术信号（OpenAlex 为主源，S2 仅在配了 key 时补 tldr）
    # 必须在 normalize 之前取：补捞通道的判定依赖 citation_count
    citation_signals: dict[str, dict] = {}
    if args.no_citations:
        sources["openalex"] = {"ok": True, "count": 0, "skipped": True}
        log.info("按参数跳过学术信号采集")
    else:
        ids = {item["arxiv_id"] for item in arxiv_items if item.get("arxiv_id")}
        ids |= {item["arxiv_id"] for item in hf_items if item.get("arxiv_id")}
        result = openalex.collect(sorted(ids))
        sources["openalex"] = _source_meta(result)
        citation_signals = result["items"]

        s2 = semanticscholar.collect(sorted(ids))
        sources["semanticscholar"] = _source_meta(s2)
        # OpenAlex 没有 tldr 与 influential 引用，有 S2 时按条目补上
        for arxiv_id, extra in (s2["items"] or {}).items():
            merged = citation_signals.setdefault(arxiv_id, {})
            for field in ("tldr", "influential_citation_count"):
                if extra.get(field) is not None:
                    merged[field] = extra[field]

    if not arxiv_items and not hf_items and not github_items:
        log.error("所有数据源均无结果，保留上一期内容，不写入候选池")
        _write_metrics(batch, sources, None, None, started, status="failed")
        return 1

    normalized = norm_mod.normalize(
        batch=batch,
        arxiv_items=arxiv_items,
        hf_items=hf_items,
        github_items=github_items,
        citation_signals=citation_signals,
    )
    deduped = dedup_mod.deduplicate(normalized["items"])

    payload = {
        "arxiv_announce_date": batch.isoformat(),
        "generated_at": iso(utcnow()),
        "stage": "candidates",
        "counts": _channel_counts(deduped["items"]),
        # 各源的 ok / count / stats 一起存进候选池，不只写 metrics：解读入口是
        # 另一个 job、另一个进程，它要据此判断"批次日的 arXiv 公告是否已入场"
        # （见 run_daily 的公告闸门），而候选池是两个入口唯一共享的文件。
        "sources": sources,
        "normalize_stats": normalized["stats"],
        "dedup_stats": deduped["stats"],
        "items": deduped["items"],
    }
    out = DATA_DIR / "candidates" / f"{batch:%Y-%m-%d}.json"
    write_json(out, payload)
    if deduped["review"]:
        write_json(DATA_DIR / "candidates" / f"{batch:%Y-%m-%d}-review.json",
                   deduped["review"])

    _write_metrics(batch, sources, normalized["stats"], deduped["stats"],
                   started, status="ok")
    log.info("=== 批次 %s 完成：候选 %d 条 → %s ===",
             batch, len(deduped["items"]), out.relative_to(DATA_DIR.parent))
    return 0


def _load_raw(batch: date, sources: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """读回三源原始响应。缺某一源按空处理，与联网时该源失败等价。"""
    loaded: list[list[dict]] = []
    for name in ("arxiv", "huggingface", "github"):
        items = read_json(raw_path(batch, name), default=None)
        items = items if isinstance(items, list) else []
        sources[name] = {"ok": bool(items), "count": len(items),
                         "from_raw": True}
        loaded.append(items)
    log.info("从 raw 重建：arxiv %d / hf %d / github %d",
             *(len(x) for x in loaded))
    return loaded[0], loaded[1], loaded[2]


def _source_meta(result: dict) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "ok": result.get("ok", False),
        "count": len(result.get("items") or []),
        "stats": result.get("stats") or {},
    }
    if result.get("error"):
        meta["error"] = result["error"]
    return meta


def _channel_counts(items: list[dict]) -> dict[str, Any]:
    channels: dict[str, int] = {}
    types: dict[str, int] = {}
    for item in items:
        channel = item.get("channel") or "unknown"
        channels[channel] = channels.get(channel, 0) + 1
        kind = item.get("content_type") or "unknown"
        types[kind] = types.get(kind, 0) + 1
    return {"total": len(items), "by_channel": channels, "by_type": types}


def _write_metrics(
    batch: date,
    sources: dict,
    normalize_stats: dict | None,
    dedup_stats: dict | None,
    started: float,
    *,
    status: str,
) -> None:
    """按月分片写 metrics，同一批次日幂等覆盖采集相关字段。

    只更新本阶段产出的键，不整行替换：解读阶段（`archive.write_metrics`）往
    同一个日期键写 `llm`/`output`/`validation`，重跑采集不该把它们抹掉。
    """
    path = metrics_path(batch)
    shard = read_json(path, default={}) or {}
    row = dict(shard.get(batch.isoformat()) or {})
    row.update({
        "arxiv_announce_date": batch.isoformat(),
        "generated_at": iso(utcnow()),
        "stage": "collect",
        "collect_status": status,
        "sources": sources,
        "normalize": normalize_stats,
        "dedup": dedup_stats,
        "duration_seconds": round(time.monotonic() - started, 1),
    })
    shard[batch.isoformat()] = row
    write_json(path, shard)


if __name__ == "__main__":
    raise SystemExit(main())


