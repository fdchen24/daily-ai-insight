"""阶段三至七入口：候选池 → 初筛 → L1 → 配额 → L2 解读 → 校验 → 落盘。

用法：
    python -m scripts.run_daily                      # 用当期候选池
    python -m scripts.run_daily --date 2026-11-23
    python -m scripts.run_daily --dry-run            # 只跑到配额，不调 L2
    python -m scripts.run_daily --limit 3            # 限制解读条数（试运行省钱）

采集单独一个入口（`scripts.collect_all`）：采集受网络与配额影响最大，重跑
它不该连带重算 LLM；反过来调 prompt 后重跑解读，也不该再抓一遍一千篇论文。
两个入口共用同一份候选池文件与同一份 metrics 当日行。

周预算只在**成功落盘之后**记账（`commit_week_budget`）：校验丢弃的条目不该
占掉本周的综述/经典名额。
"""

from __future__ import annotations

import argparse
import time
from datetime import date
from typing import Any

from .archive import publish, write_metrics
from .collect import openalex
from .common import (
    DATA_DIR,
    HttpClient,
    announce_date,
    env,
    env_flag,
    env_int,
    log,
    read_json,
    setup_logging,
)
from .filter import classic_pool, filter_candidates, impact_score
from .llm import default_ledger
from .rank import rank_pools
from .select import commit_week_budget, select_daily
from .summarize import summarize_selected
from .validate import validate_items


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily AI Insight 每日流水线")
    parser.add_argument("--date", help="arXiv 公告批次日 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        default=env_flag("DAI_DRY_RUN"),
                        help="只跑到阶段五，不调用 L2，不落盘")
    parser.add_argument("--limit", type=int, default=env_int("DAI_SUMMARIZE_LIMIT"),
                        help="最多解读几条，用于试运行")
    parser.add_argument("--log-level", default=env("DAI_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    setup_logging(args.log_level)
    batch = announce_date(args.date)
    started = time.monotonic()
    log.info("=== 批次 %s 开始每日流水线 ===", batch)
    return run(batch, args, started)


def run(batch: date, args: argparse.Namespace, started: float) -> int:
    path = DATA_DIR / "candidates" / f"{batch:%Y-%m-%d}.json"
    payload = read_json(path)
    if not payload or not payload.get("items"):
        log.error("候选池不存在或为空：%s（先跑 scripts.collect_all）", path)
        return 1

    stages: dict[str, Any] = {}

    # --- 阶段三：规则初筛
    filtered = filter_candidates(payload["items"], batch=batch)
    pools = filtered["pools"]
    pools["classic"] = _classic_pool(batch)
    stages["filter"] = filtered["stats"]
    stages["filter"]["classic_pool"] = len(pools["classic"])

    # --- 阶段四：L1 批内相对排序
    ranked = rank_pools(pools)
    stages["rank"] = ranked["stats"]

    # --- 阶段五：配额与多样性重排
    chosen = select_daily(ranked["pools"], batch=batch)
    stages["select"] = chosen["stats"]
    notes = list(chosen["notes"])
    if args.dry_run:
        log.info("dry-run：到阶段五为止，选出 %s",
                 {k: len(v) for k, v in chosen["selected"].items()})
        return 0

    selected = chosen["selected"]
    if args.limit:
        selected = _truncate(selected, args.limit)
        notes.append(f"试运行：本期限制解读 {args.limit} 条")

    # --- 阶段六：L2 深度分析与中文解读
    summarized = summarize_selected(selected, batch=batch)
    stages["summarize"] = summarized["stats"]
    if summarized["stats"].get("skipped_budget"):
        notes.append(f"token 预算用尽，本期少发 "
                     f"{summarized['stats']['skipped_budget']} 条")
    if not summarized["items"]:
        log.error("无条目完成解读，保留上一期内容")
        _metrics(batch, stages, {}, {}, started, "failed", notes)
        return 1

    # --- 阶段七：机械校验
    with HttpClient(interval=0.5, timeout=10.0) as client:
        checked = validate_items(summarized["items"], client=client)
    stages["validate"] = {k: v for k, v in checked["report"].items()
                          if k != "warnings"}
    if checked["report"]["dropped"]:
        notes.append(f"校验丢弃 {len(checked['report']['dropped'])} 条："
                     f"元数据或必填字段不符，减量投放不降门槛")

    # --- 落盘与记账
    published = publish(checked["items"], batch=batch, notes=notes,
                        stats=stages, validation=checked["report"])
    commit_week_budget(batch, published["counts"])
    _metrics(batch, stages, published["counts"], checked["report"], started,
             "ok", notes)
    log.info("=== 批次 %s 完成：投放 %s ===", batch, published["counts"])
    return 0


def _classic_pool(batch: date) -> list[dict]:
    """装配经典池并回填引用信号。

    经典论文不走采集阶段（它们不在当天的 arXiv 批次里），引用数要在这里单独
    查一次 OpenAlex——没有它，经典池的 impact 维度全是 0，排序会变成按条目
    在 classics.yaml 里的顺序。只有 10 条，走的也是同一份月度缓存。
    """
    pool = classic_pool(batch=batch)
    ids = [item["arxiv_id"] for item in pool if item.get("arxiv_id")]
    if not ids:
        return pool
    result = openalex.collect(ids)
    if not result.get("ok"):
        log.warning("经典池引用信号回填失败，按无信号继续：%s", result.get("error"))
        return pool
    for item in pool:
        extra = result["items"].get(item.get("arxiv_id") or "")
        if not extra:
            continue
        item["signals"] = {**(item.get("signals") or {}), **extra}
        item.setdefault("prefilter", {})["impact"] = round(impact_score(item), 1)
    return pool


def _truncate(selected: dict[str, list[dict]], limit: int) -> dict[str, list[dict]]:
    """按池顺序截断到总计 limit 条，前沿优先（试运行用）。"""
    out: dict[str, list[dict]] = {}
    left = limit
    for name in ("frontier", "survey", "classic"):
        pool = selected.get(name) or []
        out[name] = pool[:max(left, 0)]
        left -= len(out[name])
    return out


def _metrics(batch: date, stages: dict, output: dict, validation: dict,
             started: float, status: str, notes: list[str]) -> None:
    write_metrics(batch, llm=default_ledger().report(), output=output,
                  validation=validation, stages=stages,
                  duration_seconds=time.monotonic() - started,
                  status=status, notes=notes)


if __name__ == "__main__":
    raise SystemExit(main())
