"""阶段六：L2 深度分析与中文解读（合并单次调用，DESIGN 4.6 / 五）。

四个实现决定：

1. **一条目一次调用**。v1.0 把"深度评分"和"生成解读"分成两步，各读一遍全文，
   最贵的 token 付两次。这里一次调用同时拿回 `scores`、`reading` 和 `claims`。
2. **正常路径不做 L2 初筛**。DESIGN 9.1 列了"L2 初筛 30–40 条 @ intro_conclusion"
   与"解读 10–12 条 @ fulltext"，但阶段五的配额已经把当日压到约 12 条，再插一轮
   初筛等于为同一批条目付两次钱。因此只有入场条目数超过 `final_count` 时才启用
   初筛分支（`screen_count` 上限、intro_conclusion 取材），正常日子直接读全文。
3. **按 `content_hash` 复用解读**。重跑、经典复推和 README 未变的仓库都命中缓存
   （DESIGN 9.2 第 5–7 条）；缓存键含模板、取材层级与模型名，摘要级解读不会被当成
   全文级复用，换模型也不会复用上一个模型的输出。
4. **预算用尽就停，不回滚**。`BudgetExceeded` 发生时已完成的解读照常返回，
   未处理条目标 `skipped_budget`，由调用方决定减量投放。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from .common import DATA_DIR, HttpClient, load_config, load_prompt, log, write_json
from .llm import LLM, BudgetExceeded, LLMError
from .material import Material, build_material
from .rank import composite_score

_CACHE_DIR = DATA_DIR / "state" / "readings"

# 四套模板按内容类型与推荐类型选取；repo 条目一律走 github 模板
_TEMPLATES = {"frontier": "frontier", "survey": "survey", "classic": "classic"}


def summarize_selected(selected: dict[str, list[dict]], *, batch: date,
                       llm: LLM | None = None) -> dict[str, Any]:
    """对阶段五选出的条目逐条生成解读，返回 {"items", "stats"}。"""
    cfg = load_config("scoring")["summarize"]
    entries = [(name, item) for name, pool in selected.items() for item in pool]
    entries.sort(key=lambda row: _score(row[1]), reverse=True)

    final_count = int(cfg.get("final_count") or 12)
    screened: list[tuple[str, dict]] = []
    stats: dict[str, Any] = {"input": len(entries), "cached": 0, "failed": 0,
                             "skipped_budget": 0, "levels": {}, "degraded": []}

    if len(entries) > final_count:
        entries, screen_stats = _screen(entries, cfg, llm, stats)
        stats["screen"] = screen_stats
    entries = entries[:final_count]

    llm = llm or LLM.from_config("summarize")
    client = HttpClient(interval=3.0, timeout=90.0)
    done: list[dict] = []
    try:
        for name, item in entries:
            try:
                _analyze(name, item, level="fulltext", llm=llm, client=client,
                         stats=stats)
            except BudgetExceeded as exc:
                log.warning("预算用尽，剩余条目不再解读：%s", exc)
                stats["skipped_budget"] = len(entries) - len(done)
                item["skipped_budget"] = True
                break
            except LLMError as exc:
                log.error("解读失败，条目丢弃：%s（%s）",
                          (item.get("title") or "")[:40], exc)
                stats["failed"] += 1
                continue
            done.append(item)
    finally:
        client.close()

    log.info("L2 解读：%d/%d 条完成（缓存 %d，失败 %d，预算中止 %d）",
             len(done), stats["input"], stats["cached"], stats["failed"],
             stats["skipped_budget"])
    return {"items": done, "stats": stats}


# ------------------------------------------------------------ L2 初筛


def _screen(entries: list[tuple[str, dict]], cfg: dict, llm: LLM | None,
            stats: dict) -> tuple[list[tuple[str, dict]], dict]:
    """条目超额时的廉价一轮：intro_conclusion 取材，按综合分重排。

    初筛结果不丢弃——同一条目后面读全文时会用新材料覆盖 `reading`，
    这里只借 L2 的评分把顺序定下来。
    """
    limit = int(cfg.get("screen_count") or 30)
    pool = entries[:limit]
    llm = llm or LLM.from_config("summarize")
    client = HttpClient(interval=3.0, timeout=90.0)
    ok = 0
    try:
        for name, item in pool:
            try:
                _analyze(name, item, level="intro_conclusion", llm=llm,
                         client=client, stats=stats)
                ok += 1
            except BudgetExceeded:
                log.warning("初筛阶段预算用尽，按现有分数排序")
                break
            except LLMError as exc:
                log.warning("初筛失败，保留 L1 分数：%s", exc)
    finally:
        client.close()
    pool.sort(key=lambda row: _score(row[1]), reverse=True)
    return pool, {"screened": len(pool), "scored": ok}


# ------------------------------------------------------------ 单条解读


def _analyze(pool: str, item: dict, *, level: str, llm: LLM,
             client: HttpClient, stats: dict) -> None:
    """取材 → 调模型 → 写回条目。缓存命中则跳过调用。"""
    template = _template_for(pool, item)
    # 校验层按模板决定必填字段，不再从 recommendation_type 反推
    item["reading_template"] = template
    material = build_material(item, level=level, client=client)
    stats["levels"][material.level] = stats["levels"].get(material.level, 0) + 1
    if material.note:
        stats["degraded"].append(
            {"id": item.get("id"), "note": material.note})

    cached = _read_cache(item, template, material.level, llm.model)
    if cached is not None:
        stats["cached"] += 1
        # 复用历史解读不产生 token；写成 0 而不是留空，metrics 才对得上
        item["cost"] = {"model_used": f"cache({llm.model})", "tokens_in": 0,
                        "tokens_out": 0, "cache_hit_tokens": 0}
        _write_back(item, cached, material)
        return

    system = load_prompt(template)
    data = llm.chat_json(system, _user_message(item, material),
                         label=f"{template}:{item.get('id')}")
    item["cost"] = dict(llm.last_call)
    _write_cache(item, template, material.level, llm.model, data)
    _write_back(item, data, material)


def _template_for(pool: str, item: dict) -> str:
    if item.get("content_type") == "repo":
        return "github"
    return _TEMPLATES.get(pool, "frontier")


def _user_message(item: dict, material: Material) -> str:
    """固定段在 system prompt 里，这里只放可变材料，让 prefix cache 命中。"""
    rows = [f"evidence_level: {material.level}"]
    if material.level == "abstract":
        rows.append("（材料仅有摘要：禁止输出任何实验数值，claims 必须为空数组）")
    angles = item.get("previous_angles") or []
    if angles:
        rows.append("本站历次解读角度（必须换角度）：\n- " + "\n- ".join(angles))
    recent = item.get("recent_citing_works") or []
    if recent:
        rows.append("近 180 天引用该论文的工作：\n- " + "\n- ".join(recent[:10]))
    rows.append(material.text)
    return "\n\n".join(rows)


def _write_back(item: dict, data: dict, material: Material) -> None:
    """把模型输出并入条目，并用 L2 评分覆盖 L1 的那两个维度后重算总分。"""
    scores = data.get("scores") or {}
    quality = item.setdefault("quality", {})
    dims = quality.setdefault("dimensions", {})
    for key in ("research_value", "novelty", "technical_depth"):
        value = scores.get(key)
        if value is not None:
            dims[key] = float(value)
        elif key == "technical_depth":
            dims[key] = None
    quality["l2_reasons"] = {
        key: scores.get(f"{key}_reason")
        for key in ("research_value", "novelty", "technical_depth")
        if scores.get(f"{key}_reason")
    }
    quality["evidence_level"] = material.level
    quality["score"] = composite_score(dims, load_config("scoring")["weights"])

    item["title_zh"] = data.get("title_zh")
    item["reading"] = data.get("reading") or {}
    item["claims"] = data.get("claims") or []
    item["evidence_level"] = material.level
    if material.note:
        item["material_note"] = material.note
    # 校验层要反查 claims 的 quote 与数值。反查范围必须是"模型实际看到的材料"
    # （含摘要头部）：实测模型会引用 arXiv 摘要里的 LaTeX 原文（`$\rightarrow$`），
    # 只拿 PDF 正文反查会把这类合法引用误判成虚构。材料不入最终 JSON。
    item["_material"] = material.text


# ------------------------------------------------------------ 缓存


def _cache_key(item: dict, template: str, level: str, model: str) -> str:
    """缓存键含模型名：换模型后旧解读不该被当成新模型的输出复用。

    评分与文风都随模型变，而 metrics 里的 `model_used` 又要如实反映这条解读
    是谁生成的，所以模型名必须进键，不能只按 content_hash + 模板 + 取材层级。
    """
    digest = item.get("content_hash") or item.get("id") or "unknown"
    return f"{template}-{level}-{model}-{str(digest).replace('/', '--')}"


def _read_cache(item: dict, template: str, level: str, model: str) -> dict | None:
    """经典复推要求换角度，带 previous_angles 的条目不吃缓存。"""
    if item.get("previous_angles"):
        return None
    path = _CACHE_DIR / f"{_cache_key(item, template, level, model)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("解读缓存损坏，重新生成：%s", path.name)
        return None
    return data if isinstance(data, dict) and data.get("reading") else None


def _write_cache(item: dict, template: str, level: str, model: str,
                 data: dict) -> None:
    write_json(_CACHE_DIR / f"{_cache_key(item, template, level, model)}.json",
               data)


def _score(item: dict) -> float:
    return float((item.get("quality") or {}).get("score") or 0.0)
