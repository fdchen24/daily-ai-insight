"""阶段七后半：组装当期 JSON、落盘归档、写已推荐台账与 metrics。

三件事与 DESIGN 的对应关系：

1. **当期输出按 8.6 的结构组装**。流水线内部把解读挂在 `reading` 上（prompt
   的输出字段名），落盘时改名为 `content` 并补 `reading_minutes`；内部字段
   （`_material`、`prefilter`、`topics_hint` 等）一律剔除，不进 data 分支。
2. **`arxiv_announce_date` 是当期主键**。同一批次重跑覆盖当天文件、覆盖台账
   当日行、覆盖归档索引里的同日条目，不追加（DESIGN 4.1 与十一）。
3. **台账记真实投放结果**。只有通过校验、确实写进当期 JSON 的条目才登记
   `last_recommended`，冷却期因此不会被"生成了但被丢弃"的条目占掉。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .common import (
    DATA_DIR,
    archive_index_path,
    daily_path,
    iso,
    load_config,
    log,
    metrics_path,
    read_json,
    seen_path,
    utcnow,
    write_json,
)

# 内部字段：解读与校验用得到，但不属于对外数据结构
_INTERNAL = ("_material", "prefilter", "topics_hint", "s2_tldr",
             "previous_angles", "recent_citing_works", "skipped_budget",
             "reading_template", "content_hash")

# 中文阅读速度按每分钟 400 字估（技术文本偏慢），下限 1 分钟
_CHARS_PER_MINUTE = 400


def publish(items: list[dict], *, batch: date, notes: list[str],
            stats: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    """组装并写出当期 JSON、latest.json、归档索引与已推荐台账。"""
    now = utcnow()
    payload_items = [_shape(item) for item in items]
    counts: dict[str, int] = {}
    for item in payload_items:
        name = item.get("recommendation_type") or "frontier"
        counts[name] = counts.get(name, 0) + 1

    payload = {
        "arxiv_announce_date": batch.isoformat(),
        "generated_at": iso(now),
        "counts": counts,
        "notes": notes,
        "items": payload_items,
        "stats": stats,
        "validation": {
            "items_dropped": len(validation.get("dropped") or []),
            "claims_dropped": int(validation.get("dropped_claims") or 0),
            "downgraded": len(validation.get("downgraded") or []),
            "link_failures": len(validation.get("link_failures") or []),
            "warnings": len(validation.get("warnings") or []),
        },
    }
    write_json(daily_path(batch), payload)
    write_json(DATA_DIR / "daily" / "latest.json", payload)
    _update_index(batch, payload, counts)
    _update_seen(batch, payload_items, now)
    log.info("当期写出：%s（%s）", daily_path(batch).relative_to(DATA_DIR), counts)
    return payload


def _shape(item: dict) -> dict[str, Any]:
    """按 DESIGN 8.6 组装单条目，并把 `reading` 改名为 `content`。"""
    reading = dict(item.get("reading") or {})
    reading["reading_minutes"] = _reading_minutes(item)
    out = {key: value for key, value in item.items()
           if key not in _INTERNAL and key not in ("reading",)}
    out["content"] = reading
    out["validation"] = _shape_validation(item)
    out.setdefault("cost", {})
    return out


def _shape_validation(item: dict) -> dict[str, Any]:
    """台账式校验结果：前端只需要三个布尔/计数，细节留在 details 里。"""
    raw = item.get("validation") or {}
    dropped = raw.get("dropped_claims") or []
    links = item.get("links") or {}
    return {
        "metadata_ok": raw.get("metadata") == "ok",
        "links_ok": not any(w.get("kind") == "link"
                            for w in (raw.get("warnings") or [])),
        "links_present": sorted(k for k, v in links.items() if v),
        "dropped_claims": len(dropped) if isinstance(dropped, list) else int(dropped),
        "downgraded_reason": raw.get("downgraded_reason"),
        "warnings": raw.get("warnings") or [],
    }


def _reading_minutes(item: dict) -> int:
    """按解读正文长度估阅读时长——这是解读的阅读时间，不是论文的。"""
    reading = item.get("reading") or {}
    size = 0
    for value in reading.values():
        if isinstance(value, str):
            size += len(value)
        elif isinstance(value, list):
            size += sum(len(str(row.get("detail") or "")) + len(str(row.get("point") or ""))
                        for row in value if isinstance(row, dict))
    return max(1, round(size / _CHARS_PER_MINUTE))


# ------------------------------------------------------------ 索引与台账


def _update_index(batch: date, payload: dict, counts: dict[str, int]) -> None:
    """按年分片的归档索引：每天一行，供前端做归档页和日期跳转。"""
    path = archive_index_path(batch)
    index = read_json(path, default=None) or {"year": batch.year, "days": {}}
    index["days"][batch.isoformat()] = {
        "counts": counts,
        "total": len(payload["items"]),
        "generated_at": payload["generated_at"],
        "titles": [item.get("title_zh") or item.get("title")
                   for item in payload["items"][:12]],
    }
    index["updated_at"] = payload["generated_at"]
    write_json(path, index)


def _update_seen(batch: date, items: list[dict], now: Any) -> None:
    """写 `seen-YYYY-MM.json`：冷却期与经典轮换都读这份台账。

    同批次重跑按 item_id 覆盖，不会把同一天记成两次推荐；跨月的批次写进
    各自的月分片，`recent_seen_months` 会一起读回来。
    """
    path = seen_path(batch)
    shard = read_json(path, default={}) or {}
    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
        record = shard.get(item_id) or {}
        angles = list(record.get("angles") or [])
        why = (item.get("content") or {}).get("why_read") or \
            (item.get("content") or {}).get("why_today")
        if why:
            # 经典复推要求换角度，这里留下历次解读的切入点供下次喂回模型
            angles = (angles + [why[:120]])[-5:]
        shard[item_id] = {
            "last_recommended": iso(now),
            "arxiv_announce_date": batch.isoformat(),
            "recommendation_type": item.get("recommendation_type"),
            "title": item.get("title"),
            "count": int(record.get("count") or 0) + 1,
            "angles": angles,
        }
    write_json(path, shard)


def write_metrics(batch: date, *, llm: dict[str, Any], output: dict[str, int],
                  validation: dict[str, Any], duration_seconds: float,
                  status: str, stages: dict[str, Any] | None = None,
                  notes: list[str] | None = None) -> dict[str, Any]:
    """按 DESIGN 9.3 并入 `data/metrics/YYYY-MM.json` 的当日行。

    与采集阶段共用同一份月分片、同一个日期键：采集写 `sources`/`normalize`/
    `dedup`，这里补 `llm`/`output`/`validation`/`cost`。所以是**合并**而不是
    覆盖——否则独立重跑解读阶段会把当天的采集指标抹掉。
    """
    path = metrics_path(batch)
    shard = read_json(path, default={}) or {}
    row = dict(shard.get(batch.isoformat()) or {})
    row.update({
        "arxiv_announce_date": batch.isoformat(),
        "generated_at": iso(utcnow()),
        "stage": "daily",
        "llm": llm,
        "output": output,
        "stages": stages or {},
        "validation": {
            "items_dropped": len(validation.get("dropped") or []),
            "claims_dropped": int(validation.get("dropped_claims") or 0),
            "downgraded": len(validation.get("downgraded") or []),
            "link_failures": len(validation.get("link_failures") or []),
            "warnings": len(validation.get("warnings") or []),
        },
        "cost": _cost(llm, duration_seconds),
        "pipeline_duration_seconds": round(duration_seconds, 1),
        "status": status,
        "notes": notes or [],
    })
    shard[batch.isoformat()] = row
    write_json(path, shard)
    log.info("metrics 写出：%s status=%s 成本=%s",
             path.relative_to(DATA_DIR), status, row["cost"])
    return row


def _cost(llm: dict[str, Any], duration_seconds: float) -> dict[str, Any]:
    """按 scoring.yaml 的 `pricing` 折算金额。单价未填就如实写未知。

    DeepSeek 分时计价，空闲时段是高峰的一半，所以算钱必须知道这些 token 是
    什么时候花的：取本次运行的中点（调用散布在整段运行里，比用结束时刻更接近
    真实），换成北京时间后落在 `peak_hours_cn` 里就按高峰算。节假日官方按空闲
    计费，但日历判不出来，这里一律按工作日——成本宁可记高不记低。
    """
    cfg = load_config("scoring").get("pricing") or {}
    models = cfg.get("models") or {}
    if not models:
        return {"currency": "CNY", "amount": None,
                "note": "pricing 未配置，成本待填入官方单价后折算"}

    middle = utcnow() - timedelta(seconds=max(duration_seconds, 0) / 2)
    window = _price_window(middle, cfg.get("peak_hours_cn") or [])
    total = 0.0
    unpriced: list[str] = []
    for model, usage in llm.items():
        rate = ((models.get(model) or {}).get(window)) or {}
        if not rate:
            unpriced.append(model)
            continue
        rate_in = float(rate.get("input_per_mtok") or 0.0)
        rate_out = float(rate.get("output_per_mtok") or 0.0)
        rate_cached = float(rate.get("cache_hit_per_mtok") or rate_in)
        hit = int(usage.get("cache_hit_tokens") or 0)
        miss = max(int(usage.get("tokens_in") or 0) - hit, 0)
        total += (miss * rate_in + hit * rate_cached
                  + int(usage.get("tokens_out") or 0) * rate_out) / 1_000_000
    out = {"currency": cfg.get("currency") or "CNY", "amount": round(total, 4),
           "price_window": window}
    if unpriced:
        out["note"] = f"{'、'.join(unpriced)} 未配置单价，未计入金额"
    return out


def _price_window(moment: datetime, peak_hours: list) -> str:
    """判定高峰/空闲。中国不用夏令时，直接按固定 UTC+8 换算，不依赖 tzdata。"""
    local = moment.astimezone(timezone(timedelta(hours=8)))
    if local.weekday() >= 5:
        return "off_peak"
    return "peak" if any(int(a) <= local.hour < int(b)
                         for a, b in peak_hours) else "off_peak"
