"""阶段四：L1 批内相对排序（deepseek-flash，effort=low）。

把初筛后的 100–150 条压到 50 条上下，交给昂贵的 L2。核心做法见 DESIGN 4.4：
**不让模型独立打分**，而是每批 15 条在同一上下文里排序并给 tier。独立打分的
分数压缩（多数落 7–8）和跨批不可比，是 v1.0 最大的失真来源。

三个实现决定：

1. **批内用序号而不是真实 ID**。让模型复述 `arxiv-2510.18927` 既费 token 又
   容易抄错，错一位就无法回填；序号 1–15 可以逐一校验是否恰好出现一次。
2. **tier 决定分段，批内名次只在段内插值**。这样跨批可比（A 永远高于 B），
   同时保留批内的细粒度区分。段宽取到下一档基准分的一半，段间不重叠。
3. **一批失败不拖垮整池**：该批按 B 档基准分计入排序，并计入
   `failed_batches`，而不是整池丢弃或整次运行中止。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .common import load_config, load_prompt, log
from .llm import LLM, BudgetExceeded, LLMError

_POOL_PROMPTS = {"frontier": "rank_frontier",
                 "survey": "rank_survey",
                 "classic": "rank_classic"}
# L1 只看摘要级材料（DESIGN 9.1）：截断到约 0.4K token，批量输入才可控
_ABSTRACT_CHARS = 1200
# 各 tier 的"上一档基准分"，A 以 100 封顶
_TIER_CEILING = {"A": 100.0, "B": 90.0, "C": 75.0}


def rank_pools(pools: dict[str, list[dict]], *,
               llm: LLM | None = None) -> dict[str, Any]:
    """对三池分别排序并按 `rank.keep` 截断，返回 {"pools":..., "stats":...}。"""
    cfg = load_config("scoring")
    rank_cfg = cfg["rank"]
    llm = llm or LLM.from_config("rank")

    ranked_pools: dict[str, list[dict]] = {}
    stats: dict[str, Any] = {}
    for name, items in pools.items():
        if not items:
            ranked_pools[name] = []
            stats[name] = {"input": 0, "kept": 0}
            continue
        ranked, pool_stats = _rank_pool(name, list(items), cfg, llm)
        keep = int(rank_cfg["keep"].get(name) or len(ranked))
        kept = ranked[:keep]
        for position, item in enumerate(kept, 1):
            item["quality"]["rank_in_pool"] = position
        ranked_pools[name] = kept
        pool_stats["kept"] = len(kept)
        stats[name] = pool_stats
    log.info("L1 排序完成：%s",
             {name: f"{s['input']}→{s['kept']}" for name, s in stats.items()})
    return {"pools": ranked_pools, "stats": stats}


def _rank_pool(name: str, items: list[dict], cfg: dict,
               llm: LLM) -> tuple[list[dict], dict[str, Any]]:
    system = load_prompt(_POOL_PROMPTS[name])
    size = int(cfg["rank"]["batch_size"])
    batches = [items[i:i + size] for i in range(0, len(items), size)]
    failed = 0
    for index, batch in enumerate(batches, 1):
        label = f"rank:{name}#{index}"
        try:
            verdicts = _rank_batch(llm, system, name, batch, label)
        except BudgetExceeded:
            # 预算闸门：剩余批次按中位基准计分，但已排好的不回退
            log.error("%s 触及 token 预算，剩余 %d 批按中位基准计分",
                      label, len(batches) - index + 1)
            for remaining in batches[index - 1:]:
                _apply(remaining, [], cfg)
                failed += 1
            break
        except LLMError as exc:
            log.warning("%s 排序失败，该批按中位基准计分：%s", label, exc)
            verdicts = []
            failed += 1
        _apply(batch, verdicts, cfg)

    ranked = sorted(items, key=lambda it: it["quality"]["score"], reverse=True)
    tiers = Counter(it["quality"]["tier"] or "-" for it in items)
    return ranked, {"input": len(items), "batches": len(batches),
                    "failed_batches": failed, "tiers": dict(sorted(tiers.items()))}


# ------------------------------------------------------------ 单批调用


def _rank_batch(llm: LLM, system: str, pool: str, batch: list[dict],
                label: str) -> list[tuple[int, str, str]]:
    data = llm.chat_json(system, _render_batch(pool, batch), label=label)
    order, missing = _parse_ranking(data, len(batch))
    if len(missing) * 2 > len(batch):
        # 过半条目没被评价，说明模型没读懂任务，当失败处理而不是硬塞 C
        raise LLMError(f"{label} 仅返回 {len(batch) - len(missing)}/{len(batch)} 条")
    if missing:
        log.warning("%s 漏评 %d 条，按 C 置于批末：%s", label, len(missing), missing)
    return order


def _parse_ranking(data: dict, count: int) -> tuple[list[tuple[int, str, str]],
                                                    list[int]]:
    """数组顺序即排名。越界、重复、非 A/B/C 的 tier 一律就地修正而非报错。"""
    order: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for row in data.get("ranking") or []:
        if not isinstance(row, dict):
            continue
        try:
            no = int(row.get("no"))
        except (TypeError, ValueError):
            continue
        if not 1 <= no <= count or no in seen:
            continue
        tier = str(row.get("tier") or "").strip().upper()[:1]
        reason = " ".join(str(row.get("reason") or "").split())[:100]
        seen.add(no)
        order.append((no, tier if tier in ("A", "B", "C") else "C", reason))
    missing = [no for no in range(1, count + 1) if no not in seen]
    order.extend((no, "C", "模型未给出评价") for no in missing)
    return order, missing


def _render_batch(pool: str, batch: list[dict]) -> str:
    """序号从 1 开始；固定说明放最前，可变条目在后（利于 prefix cache）。"""
    lines = [f"本批 {len(batch)} 条候选，请按相对质量从好到差排序。", ""]
    for no, item in enumerate(batch, 1):
        signals = item.get("signals") or {}
        meta = [f"type={item.get('content_type') or 'paper'}"]
        if pool != "classic":
            meta.append(f"channel={item.get('channel')}")
            meta.append(f"cat={item.get('primary_category') or '-'}")
        else:
            meta.append(f"year={item.get('year') or '-'}")
        if item.get("topics"):
            # 用顿号而不是斜杠：主题名本身含 "/"（如 架构/训练/推理加速）
            meta.append("topics=" + "、".join(item["topics"]))
        lines.append(f"[{no}] " + " ".join(meta))
        lines.append(f"title: {item.get('title') or ''}")
        lines.append(f"signals: {_render_signals(pool, signals)}")
        if pool == "classic" and item.get("previous_angles"):
            lines.append("previous_angles: " + "；".join(item["previous_angles"]))
        body = " ".join((item.get("abstract") or "").split())
        label = "note" if pool == "classic" else "abstract"
        lines.append(f"{label}: {body[:_ABSTRACT_CHARS] or '（缺失）'}")
        lines.append("")
    return "\n".join(lines)


def _render_signals(pool: str, signals: dict) -> str:
    def show(key: str) -> str:
        value = signals.get(key)
        return "—" if value is None else str(value)

    if pool == "survey":
        return (f"refs={show('reference_count')} citations={show('citation_count')} "
                f"upvotes={show('hf_upvotes')}")
    if pool == "classic":
        return f"citations={show('citation_count')}"
    return (f"upvotes={show('hf_upvotes')} citations={show('citation_count')} "
            f"stars_7d={show('repo_stars_7d')}")


# ------------------------------------------------------------ 分数装配


def _apply(batch: list[dict], order: list[tuple[int, str, str]],
           cfg: dict) -> None:
    """把排序结果写回条目的 `quality`。order 为空表示该批排序失败。

    失败批次不按"缺失维度"处理：剔除 L1 后重新归一化，等于让客观分独占
    权重，高 upvotes 的条目会凭空抬到 A 档之上。改为按 B 档基准分计——
    没有判断就按中位对待，强客观信号仍能竞争，弱的也不会浮上来。
    """
    tier_of: dict[int, tuple[str | None, str, float]] = {}
    if order:
        per_tier: dict[str, list[int]] = {}
        for no, tier, _ in order:
            per_tier.setdefault(tier, []).append(no)
        for no, tier, reason in order:
            group = per_tier[tier]
            tier_of[no] = (tier, reason,
                           _tier_score(tier, group.index(no), len(group), cfg))
    else:
        neutral = float(cfg["tier_scores"]["B"])
        tier_of = {no: (None, "L1 排序失败，按中位基准计分", neutral)
                   for no in range(1, len(batch) + 1)}

    for no, item in enumerate(batch, 1):
        tier, reason, l1 = tier_of[no]
        quality = item.setdefault("quality", {})
        dims = quality.setdefault("dimensions", {})
        # L1 只有一次综合判断，研究价值与创新性同时取它；L2 会各自覆盖
        dims["research_value"] = l1
        dims["novelty"] = l1
        dims.setdefault("technical_depth", None)
        pre = item.get("prefilter") or {}
        dims["impact"] = pre.get("impact")
        dims["completeness"] = pre.get("completeness")
        quality["tier"] = tier
        quality["l1_reason"] = reason
        quality["evidence_level"] = quality.get("evidence_level") or "abstract"
        quality["score"] = composite_score(dims, cfg["weights"])


def _tier_score(tier: str, index: int, size: int, cfg: dict) -> float:
    """段内按名次线性插值。段宽取到上一档基准分的一半，保证段间不重叠。"""
    base = float(cfg["tier_scores"][tier])
    span = (_TIER_CEILING[tier] - base) / 2.0
    offset = 0.0 if size <= 1 else (1.0 - index / (size - 1)) * span
    return round(base + offset, 1)


def composite_score(dimensions: dict[str, float | None],
                    weights: dict[str, float]) -> float:
    """按 DESIGN 4.4 的权重加权，缺失维度剔除后重新归一化。

    不把缺失项按 0 计：L1 阶段 `technical_depth` 必然为空（没读正文），按 0
    计会让所有未经 L2 的条目凭空矮 20 分，与 L2 条目无法同场比较。
    """
    total = 0.0
    used = 0.0
    for name, weight in weights.items():
        value = dimensions.get(name)
        if value is None:
            continue
        total += float(value) * float(weight)
        used += float(weight)
    return round(total / used, 1) if used else 0.0
