"""阶段五：配额、周预算账本与多样性重排（DESIGN 4.5）。

把 L1 留下的 ~56 条收到当日实际投放量：前沿 10 篇/日，综述与经典各 5 篇/周。

三个实现决定：

1. **周预算按天记账，不按累计计数**。账本存 `{week: {date: {type: n}}}`，
   当日用量以日期为键覆盖写入，于是同一批次重跑是覆盖而不是追加——这是
   DESIGN 十一"重跑为覆盖而非追加"在配额上的对应要求。
2. **周内均摊而不是先到先得**。剩余名额除以本周剩余工作日向上取整，
   避免周一两篇、周五无内容；`weekly_daily_cap` 是上限而非目标。
3. **机构约束暂不生效**。机构名有两个来源：OpenAlex 消歧后的 `signals.institutions`，
   以及 L2 解读从论文首页抄回的顶层 `affiliations`（详情页优先用后者）。前者在
   预印本上错得不少（实测 InstructGPT 被标成 Alignment Research Center），后者
   要到阶段六才产出、选取时还不存在。两条都不够当配额依据，所以代码按字段存在
   才启用，并在 notes 里如实记录"未启用"，不假装约束已生效。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from .common import (
    iso,
    load_config,
    log,
    read_json,
    title_similarity,
    utcnow,
    week_budget_path,
    write_json,
)

_WEEKLY = {"survey": "survey_per_week", "classic": "classic_per_week"}


def select_daily(pools: dict[str, list[dict]], *, batch: date,
                 now: Any = None) -> dict[str, Any]:
    """按配额与多样性选出当日投放条目，不写账本（交 commit_week_budget）。"""
    cfg = load_config("scoring")
    quotas, diversity = cfg["quotas"], cfg["diversity"]
    now = now or utcnow()
    allowance = _allowances(batch, quotas)

    selected: dict[str, list[dict]] = {}
    notes: list[str] = []
    stats: dict[str, Any] = {"allowance": dict(allowance), "dropped": {}}
    for name, items in pools.items():
        want = allowance.get(name, 0)
        picked, dropped = _pick(items, want, diversity)
        for item in picked:
            item["recommendation_type"] = name
            item["recommended_at"] = iso(now)
        selected[name] = picked
        stats["dropped"][name] = dropped
        if want and len(picked) < want:
            notes.append(f"{name} 仅选出 {len(picked)}/{want} 篇："
                         f"候选不足或受多样性约束，减量投放不降门槛")
    if not any(_affiliations(it) for pool in pools.values() for it in pool):
        stats["affiliation_constraint"] = "inactive_no_data"
    log.info("配额选取：%s（名额 %s）",
             {k: len(v) for k, v in selected.items()}, allowance)
    return {"selected": selected, "notes": notes, "stats": stats}


# ------------------------------------------------------------ 名额


def _allowances(batch: date, quotas: dict) -> dict[str, int]:
    """前沿按日配额；综述与经典按周账本均摊到本周剩余工作日。"""
    out = {"frontier": int(quotas["frontier_per_day"])}
    if batch.weekday() >= 5:
        # arXiv 周六周日不公告（DESIGN 3.1），当天没有新内容可投
        log.info("%s 是周末，周配额本日不投放", batch)
        return {"frontier": 0, "survey": 0, "classic": 0}

    ledger = _read_ledger(batch)
    cap = int(quotas["weekly_daily_cap"])
    left = _weekdays_left(batch)
    for name, key in _WEEKLY.items():
        used = _used_this_week(ledger, batch, name)
        remaining = max(int(quotas[key]) - used, 0)
        spread = math.ceil(remaining / left) if left else remaining
        if spread > cap:
            log.warning("%s 本周剩余 %d 篇、仅剩 %d 个工作日，单日需 %d 篇，"
                        "超过 weekly_daily_cap=%d，以补足周配额为先",
                        name, remaining, left, spread, cap)
        out[name] = min(remaining, max(spread, 0))
    return out


def _weekdays_left(batch: date) -> int:
    """含当天在内，本 ISO 周还剩几个工作日。"""
    return sum(1 for offset in range(7 - batch.weekday())
               if (batch + timedelta(days=offset)).weekday() < 5)


def _used_this_week(ledger: dict, batch: date, name: str) -> int:
    """当天自身的记录不计入已用量，使同批次重跑表现为覆盖。"""
    days = (ledger.get("days") or {})
    today = batch.isoformat()
    return sum(int((counts or {}).get(name) or 0)
               for day, counts in days.items() if day != today)


def _week_key(batch: date) -> str:
    year, week, _ = batch.isocalendar()
    return f"{year}-W{week:02d}"


def _read_ledger(batch: date) -> dict:
    """账本只保留当前 ISO 周；跨周自动归零，不累积历史。"""
    ledger = read_json(week_budget_path(), default={}) or {}
    if ledger.get("week") != _week_key(batch):
        return {"week": _week_key(batch), "days": {}}
    return ledger


def commit_week_budget(batch: date, counts: dict[str, int]) -> None:
    """投放成功后才记账：校验失败而丢弃的条目不该占用周配额。"""
    ledger = _read_ledger(batch)
    days = ledger.setdefault("days", {})
    days[batch.isoformat()] = {name: int(counts.get(name) or 0)
                               for name in _WEEKLY}
    write_json(week_budget_path(), ledger)
    log.info("周预算账本 %s 更新：%s", ledger["week"], days[batch.isoformat()])


# ------------------------------------------------------------ 多样性


def _pick(items: list[dict], want: int,
          diversity: dict) -> tuple[list[dict], dict[str, int]]:
    """按分数从高到低取，逐条检查多样性约束。items 已由 L1 排好序。

    相似度只用区间下界：DESIGN 把 0.75–0.90 定为"高度相似研究"，更高则是
    同一篇论文，本应在采集期去重。但真漏过来一条，也该按重复丢弃而不是
    因为"超出区间"放行，所以这里判 `>= 0.75`。
    """
    low = float((diversity.get("dup_similarity_range") or [0.75])[0])
    max_topic = int(diversity["max_per_topic"])
    max_affil = int(diversity["max_per_affiliation"])

    picked: list[dict] = []
    dropped = {"topic": 0, "affiliation": 0, "similar": 0}
    topic_count: dict[str, int] = {}
    affil_count: dict[str, int] = {}
    for item in sorted(items, key=_score, reverse=True):
        if len(picked) >= want:
            break
        topics = item.get("topics") or []
        if any(topic_count.get(t, 0) >= max_topic for t in topics):
            dropped["topic"] += 1
            continue
        affils = _affiliations(item)
        if any(affil_count.get(a, 0) >= max_affil for a in affils):
            dropped["affiliation"] += 1
            continue
        if _too_similar(item, picked, float(low)):
            dropped["similar"] += 1
            continue
        picked.append(item)
        for t in topics:
            topic_count[t] = topic_count.get(t, 0) + 1
        for a in affils:
            affil_count[a] = affil_count.get(a, 0) + 1
    return picked, dropped


def _score(item: dict) -> float:
    return float((item.get("quality") or {}).get("score") or 0.0)


def _affiliations(item: dict) -> list[str]:
    """读顶层 `affiliations`。阶段六的 L2 解读才写这个键，阶段五读到的恒为空，
    于是该约束等于不启用。

    有意不读 `signals.institutions`：那份数据只够展示，不够当配额依据（见模块
    开头第 3 条）。要启用就先把机构名清洗成可靠的机构 ID 并挪到选取之前，
    再改这里。
    """
    raw = item.get("affiliations") or []
    return [str(a).strip().lower() for a in raw if str(a).strip()]


def _too_similar(item: dict, picked: list[dict], threshold: float) -> bool:
    title = item.get("title")
    for other in picked:
        if title_similarity(title, other.get("title")) >= threshold:
            log.info("多样性：丢弃与《%s》高度相似的《%s》",
                     (other.get("title") or "")[:40], (title or "")[:40])
            return True
    return False
