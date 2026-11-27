"""重解读：用当前 prompt 把已发布批次的解读重新生成一遍。

prompt 一改，解读缓存键里的指纹就跟着变，新一期自然用新模板，但已经发布的
批次不会自己更新——归档里会同时存在两种文风的解读。这个入口补上那一步，
只换解读，不换条目：

1. **不重跑选取**。候选池不在 data 分支（`raw/` 与 `candidates/` 被 data 分支
   自己的 .gitignore 挡住，只在 CI 里作 artifact 传递），而且这些条目都已登记
   过 `last_recommended`，重跑阶段五会被冷却期整批排除，换回来的是另一批论文，
   那不叫"重新解读"。
2. **只覆盖解读相关字段**：`content`、`claims`、`affiliations`、`title_zh`、
   `quality`、`evidence_level`、`validation`、`cost`。元数据、客观信号、推荐
   类型与条目顺序照抄原文件。
3. **新解读过不了校验就回滚单条**，保留原解读。流水线的处理方式相反（减量
   投放，丢掉就不发），但历史页面不该因为一次重跑少掉条目。
4. **不写 seen 台账、不记周配额**。那两份账记的是"投放过什么"；重解读没有新
   投放，写了会把冷却期和周配额各算两遍。
5. **每个批次一本独立 token 账**。`summarize.budget` 是按一期 12 条定的，
   四天挤在一本账里会在第三天撞上硬闸门。

用法（`DAI_DATA_DIR` 指向 data 分支的工作区，不然改的是本地产物）：

    DAI_DATA_DIR=.data-branch/data .venv/bin/python -m scripts.reread \\
        --date 2026-11-23 --dry-run
"""

from __future__ import annotations

import argparse
import time
from datetime import date
from typing import Any

from .archive import cost_of, shape_item, update_index
from .common import (
    DATA_DIR, HttpClient, content_hash, daily_path, env, iso, load_config, log,
    metrics_path, read_json, setup_logging, utcnow, write_json,
)
from .llm import LLM, BudgetExceeded, Ledger, LLMError
from .summarize import analyze_item, material_client
from .validate import validate_items

# 重解读会覆盖的字段；新解读里没有的（如 material_note）要从原条目删掉，
# 否则会留下一条已经不成立的降级说明
_FIELDS = ("content", "claims", "affiliations", "title_zh", "quality",
           "evidence_level", "validation", "cost", "material_note")

_NOTE_PREFIX = "重解读"


def reread_batch(batch: date, *, limit: int | None = None,
                 dry_run: bool = False) -> dict[str, Any] | None:
    """重解读一个批次，原地更新当期 JSON；文件不存在时返回 None。"""
    path = daily_path(batch)
    payload = read_json(path, default=None)
    if not payload or not payload.get("items"):
        log.error("批次 %s 没有已发布的当期文件：%s", batch, path)
        return None

    targets = payload["items"][:limit] if limit else list(payload["items"])
    log.info("=== 批次 %s 重解读 %d/%d 条 ===",
             batch, len(targets), len(payload["items"]))
    if dry_run:
        for pub in targets:
            log.info("  %s %s（%s / %s）", pub.get("id"),
                     (pub.get("title") or "")[:46],
                     pub.get("recommendation_type"), pub.get("evidence_level"))
        return payload

    started = time.monotonic()
    budget = (load_config("scoring").get("summarize") or {}).get("budget") or {}
    ledger = Ledger(limit_in=budget.get("tokens_in_per_run"),
                    limit_out=budget.get("tokens_out_per_run"))
    llm = LLM.from_config("summarize", ledger=ledger)
    stats: dict[str, Any] = {"input": len(targets), "cached": 0, "failed": 0,
                             "skipped_budget": 0, "levels": {}, "degraded": []}
    rolled_back: list[dict] = []
    fresh: list[dict] = []

    client = material_client()
    try:
        for index, pub in enumerate(targets):
            item = _restore(pub)
            pool = item.get("recommendation_type") or "frontier"
            try:
                analyze_item(pool, item, level="fulltext", llm=llm,
                             client=client, stats=stats)
            except BudgetExceeded as exc:
                stats["skipped_budget"] = len(targets) - index
                log.warning("预算用尽，剩余 %d 条保留原解读：%s",
                            stats["skipped_budget"], exc)
                break
            except LLMError as exc:
                stats["failed"] += 1
                rolled_back.append({"id": pub.get("id"),
                                    "reason": f"解读调用失败：{exc}"})
                log.error("《%s》重解读失败，保留原解读：%s",
                          (pub.get("title") or "")[:40], exc)
                continue
            fresh.append(item)
    finally:
        client.close()

    return _commit(batch, path, payload, fresh, stats, rolled_back,
                   ledger, time.monotonic() - started)


def _restore(pub: dict) -> dict:
    """把已发布条目还原成流水线内部的形状：`content` 改回 `reading`。

    `content_hash` 落盘时被当内部字段剔掉了，这里按它原本的算法（标题 + 摘要
    + 版本号）重算一份，重解读写出的缓存才和正常投放同键——不然同一篇论文
    日后再被推荐时会多付一次钱。
    """
    item = {key: value for key, value in pub.items()
            if key not in ("content", "validation")}
    item["reading"] = {key: value
                       for key, value in (pub.get("content") or {}).items()
                       if key != "reading_minutes"}
    item["content_hash"] = content_hash(pub.get("title"), pub.get("abstract"),
                                        pub.get("arxiv_version"))
    return item


def _commit(batch: date, path: Any, payload: dict, fresh: list[dict],
            stats: dict, rolled_back: list[dict], ledger: Ledger,
            duration: float) -> dict[str, Any]:
    """校验新解读、并回原文件、刷新归档索引与 metrics。"""
    with HttpClient(interval=0.5, timeout=10.0) as client:
        checked = validate_items(fresh, batch=batch, client=client)
    for row in checked["report"].get("dropped") or []:
        rolled_back.append({"id": row.get("id"),
                            "reason": "；".join(row.get("reasons") or [])})

    shaped = {item.get("id"): shape_item(item) for item in checked["items"]}
    updated = 0
    for pub in payload["items"]:
        new = shaped.get(pub.get("id"))
        if not new:
            continue
        for key in _FIELDS:
            if key in new:
                pub[key] = new[key]
            else:
                pub.pop(key, None)
        updated += 1

    now = utcnow()
    report = checked["report"]
    payload["generated_at"] = iso(now)
    payload["notes"] = _notes(payload.get("notes") or [], now, updated,
                              rolled_back)
    payload["validation"] = {
        # 重解读不丢条目：过不了校验的那条回滚成原解读，仍在当期里
        "items_dropped": 0,
        "claims_dropped": int(report.get("dropped_claims") or 0),
        "downgraded": len(report.get("downgraded") or []),
        "link_failures": len(report.get("link_failures") or []),
        "warnings": len(report.get("warnings") or []),
        "reread_rolled_back": len(rolled_back),
    }

    write_json(path, payload)
    if _is_latest(batch):
        write_json(DATA_DIR / "daily" / "latest.json", payload)
    update_index(batch, payload, payload.get("counts") or {})
    cost = _write_metrics(batch, ledger, stats, rolled_back, duration)
    log.info("批次 %s 重解读完成：更新 %d 条，回滚 %d 条，用量 %s，成本 %s",
             batch, updated, len(rolled_back), ledger.report(), cost)
    return payload


def _notes(old: list, now: Any, updated: int, rolled_back: list[dict]) -> list:
    """当期 notes 里只留最近一次重解读说明，重复跑不堆叠。"""
    tail = f"，{len(rolled_back)} 条未过校验、保留原解读" if rolled_back else ""
    note = f"{_NOTE_PREFIX} {iso(now)[:10]}：{updated} 条按当前模板重新生成{tail}"
    return [row for row in old
            if not str(row).startswith(_NOTE_PREFIX)] + [note]


def _is_latest(batch: date) -> bool:
    """`latest.json` 只在重解读的批次确实是最新一期时跟着更新。"""
    latest = read_json(DATA_DIR / "daily" / "latest.json", default=None) or {}
    current = latest.get("arxiv_announce_date")
    return not current or str(current) <= batch.isoformat()


def _write_metrics(batch: date, ledger: Ledger, stats: dict,
                   rolled_back: list[dict], duration: float) -> dict[str, Any]:
    """把这轮用量追加进当日 metrics 的 `rereads`，不动原运行记下的那些键。"""
    usage = ledger.report()
    cost = cost_of(usage, duration)
    path = metrics_path(batch)
    shard = read_json(path, default={}) or {}
    row = dict(shard.get(batch.isoformat()) or {})
    rounds = list(row.get("rereads") or [])
    rounds.append({"generated_at": iso(utcnow()), "llm": usage, "cost": cost,
                   "summarize": stats, "rolled_back": rolled_back,
                   "duration_seconds": round(duration, 1)})
    row["rereads"] = rounds
    shard[batch.isoformat()] = row
    write_json(path, shard)
    return cost


def main() -> int:
    parser = argparse.ArgumentParser(description="用当前 prompt 重解读已发布批次")
    parser.add_argument("--date", nargs="+", required=True, metavar="YYYY-MM-DD",
                        help="要重解读的批次日，可给多个（按给定顺序处理）")
    parser.add_argument("--limit", type=int,
                        help="每个批次最多重解读几条，试运行省钱")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出会动哪些条目，不调模型也不落盘")
    parser.add_argument("--log-level", default=env("DAI_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    setup_logging(args.log_level)

    failed = 0
    for raw in args.date:
        batch = date.fromisoformat(raw)
        if reread_batch(batch, limit=args.limit, dry_run=args.dry_run) is None:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
