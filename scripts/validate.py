"""第六节：机械化事实校验。

v1.0 写了"关键数值校验""AI 幻觉检查"但没有可执行定义，等于没做。这里全部改成
机械可检，不再用另一个 LLM 复查（既贵又不可靠）：

1. **元数据硬核验**：arXiv ID 回查官方条目，标题逐字比对、首位作者包含比对、
   发布日期比对。不一致即丢弃条目——宁可少发一条，不发错一条。
2. **数值溯源**：模型给的每条 claim 都要在它实际看到的材料里字符串反查。
   数值查不到就删掉该 claim 所在句子；一条目丢弃超过 30% 则整条降级为
   `evidence_level: abstract` 并清掉全部数值句。
3. **其余机械检查**：必填字段、字数范围、未翻译残留（连续 ASCII 词）、
   中英混排空格、当期内 3-gram 重叠、链接可达性。

设计取舍：只有元数据不符与必填字段缺失算硬失败（丢条目），其余记 warning
并就地修正。校验层不重新调模型——降级重写在本层用删句完成，避免在流水线
末端再引入一次不可预算的 LLM 调用。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .collect.arxiv import fetch_by_ids
from .common import (
    HttpClient,
    SourceError,
    load_config,
    log,
    normalize_title,
    parse_dt,
    raw_path,
    read_json,
    text_length,
    title_similarity,
)

# 四套模板各自的必填解读字段，缺任一项即丢弃条目
_REQUIRED = {
    "frontier": ("why_read", "task", "findings", "method", "results",
                 "limitations"),
    "survey": ("why_read", "scope", "evolution", "approaches",
               "key_differences", "bottlenecks", "future_directions"),
    "classic": ("why_today", "historical_context", "problem_then",
                "contributions", "core_idea", "influence", "modern_reading"),
    "github": ("why_read", "problem", "features", "architecture",
               "vs_alternatives", "usage", "scope_limits"),
}

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；])")
_CJK = r"一-鿿"
_SPACE_BEFORE_RE = re.compile(rf"([{_CJK}])([A-Za-z0-9])")
_SPACE_AFTER_RE = re.compile(rf"([A-Za-z0-9%)])([{_CJK}])")


def validate_items(items: list[dict], *, batch: date | None = None,
                   client: HttpClient | None = None) -> dict[str, Any]:
    """逐条校验，返回 {"items"(通过的), "report"}。被丢弃的条目不进当期 JSON。"""
    cfg = load_config("scoring")["validate"]
    report: dict[str, Any] = {"checked": len(items), "dropped": [],
                              "dropped_claims": 0, "downgraded": [],
                              "warnings": [], "link_failures": []}
    official = _official_meta(items, batch, report)

    owned = client is None
    client = client or HttpClient(interval=0.5,
                                 timeout=float(cfg.get("link_timeout_seconds") or 10))
    passed: list[dict] = []
    try:
        for item in items:
            problems = _check_item(item, official, cfg, client, report)
            if problems:
                report["dropped"].append({"id": item.get("id"),
                                          "title": item.get("title"),
                                          "reasons": problems})
                log.error("校验丢弃《%s》：%s",
                          (item.get("title") or "")[:40], "；".join(problems))
                continue
            passed.append(item)
    finally:
        if owned:
            client.close()

    _check_overlap(passed, float(cfg.get("ngram_overlap_warn") or 0.4), report)
    log.info("校验：%d/%d 通过（丢弃 claim %d，降级 %d，链接异常 %d）",
             len(passed), len(items), report["dropped_claims"],
             len(report["downgraded"]), len(report["link_failures"]))
    return {"items": passed, "report": report}


def _official_meta(items: list[dict], batch: date | None,
                   report: dict) -> dict[str, dict]:
    """凑出官方元数据表：先吃当期 raw，剩下的才去 Query API 回查。

    采集阶段已经把 arXiv 的原始响应按批次存进 `data/raw/<批次>/arxiv.json`，
    那份记录没经过任何模型，拿来比对标题/作者/日期与现查官方 API 等价。
    之前每期都把 12 个 ID 重新查一遍，而刚做完全量抓取的 IP 正被 arXiv 限流，
    实测连吃 5 个 429（退避 6→96 秒）后整段核验被跳过——费了几分钟还什么都
    没校到。经典池的论文不在当期 raw 里，这些才真的需要联网回查。
    """
    ids = [item["arxiv_id"] for item in items if item.get("arxiv_id")]
    if not ids:
        return {}
    official = _meta_from_raw(batch, set(ids))
    missing = [i for i in ids if i not in official]
    if not missing:
        log.info("arXiv 回查：%d 条全部命中当期 raw，未发请求", len(ids))
        return official
    try:
        official.update(fetch_by_ids(missing))
    except SourceError as exc:
        log.error("arXiv 回查失败，这 %d 条跳过元数据核验：%s", len(missing), exc)
        report["warnings"].append({"scope": "metadata",
                                   "note": f"arXiv 回查失败，未核验：{exc}"})
        # 显式标成"未核验"：留空会被 `_check_metadata` 当成"官方查不到"而丢条目
        for arxiv_id in missing:
            official.setdefault(arxiv_id, None)
    return official


def _meta_from_raw(batch: date | None, wanted: set[str]) -> dict[str, dict]:
    """从当期 raw 里挑出需要的条目，字段名与 `fetch_by_ids` 的返回一致。"""
    if batch is None:
        return {}
    records = read_json(raw_path(batch, "arxiv"), default=None)
    if not isinstance(records, list):
        return {}
    out: dict[str, dict] = {}
    for row in records:
        arxiv_id = row.get("arxiv_id")
        if arxiv_id in wanted:
            out[arxiv_id] = {"title": row.get("title"),
                             "authors": row.get("authors") or [],
                             "published_at": row.get("published_at"),
                             "arxiv_version": row.get("arxiv_version")}
    return out



# ------------------------------------------------------------ 单条目


def _check_item(item: dict, official: dict[str, dict | None], cfg: dict,
                client: HttpClient, report: dict) -> list[str]:
    """返回硬失败原因列表；空列表表示通过。软问题就地修正并记 warning。"""
    hard: list[str] = []
    item["validation"] = {"metadata": "unchecked", "dropped_claims": [],
                          "warnings": []}

    hard += _check_metadata(item, official)
    hard += _check_required(item)
    _check_title_zh(item, hard)

    _drop_unknown_fields(item, report)
    _verify_claims(item, cfg, report)
    _check_abstract_numbers(item, report)
    _check_lengths(item, cfg, report)
    _check_untranslated(item, cfg, report)
    _normalize_spacing(item)
    _check_links(item, client, report)
    return hard


def _check_metadata(item: dict, official: dict[str, dict | None]) -> list[str]:
    """标题、首位作者、发布日期与官方元数据比对。repo 条目无此项。"""
    arxiv_id = item.get("arxiv_id")
    if not arxiv_id or not official:
        return []
    if arxiv_id not in official:
        return [f"arXiv ID {arxiv_id} 在官方 API 查不到"]
    meta = official[arxiv_id]
    if meta is None:  # 回查没拿到，这条不核验也不丢
        return []

    problems: list[str] = []
    if normalize_title(item.get("title")) != normalize_title(meta["title"]):
        # 归一化后仍不等时用 token 相似度兜一层：LaTeX 与连字符的写法差异
        # 不应导致丢弃，真正的张冠李戴会落在低相似度区间
        sim = title_similarity(item.get("title"), meta["title"])
        if sim < 0.9:
            problems.append(f"标题与官方不一致（相似度 {sim:.2f}）："
                            f"官方为《{meta['title'][:60]}》")
    ours = (item.get("authors") or [None])[0]
    theirs = (meta.get("authors") or [None])[0]
    if ours and theirs and ours.strip() not in theirs and theirs not in ours.strip():
        problems.append(f"首位作者不一致：本地 {ours}，官方 {theirs}")
    local_day = (parse_dt(item.get("published_at")) or None)
    official_day = (parse_dt(meta.get("published_at")) or None)
    if local_day and official_day and local_day.date() != official_day.date():
        problems.append(f"发布日期不一致：本地 {local_day.date()}，"
                        f"官方 {official_day.date()}")
    item["validation"]["metadata"] = "ok" if not problems else "mismatch"
    if not problems:
        # 官方版本号比本地新时以官方为准：revision 通道要靠它标注版本
        item["arxiv_version"] = meta.get("arxiv_version") or item.get("arxiv_version")
    return problems


def _check_required(item: dict) -> list[str]:
    template = (item.get("reading_template")
                or item.get("recommendation_type") or "frontier")
    if item.get("content_type") == "repo":
        template = "github"
    fields = _REQUIRED.get(template, _REQUIRED["frontier"])
    reading = item.get("reading") or {}
    missing = [name for name in fields if not reading.get(name)]
    return [f"解读缺必填字段：{'、'.join(missing)}"] if missing else []


def _drop_unknown_fields(item: dict, report: dict) -> None:
    """删掉模板之外的解读字段。

    实测模型会自作主张多加一个 `bottlenecks_note`（值还可能是 null），
    这类字段既没有校验规则也没有前端渲染位置，留着只会让对外的数据结构
    随模型心情变动，所以就地删掉并记一条 warning。
    """
    template = item.get("reading_template") or "frontier"
    allowed = set(_REQUIRED.get(template, _REQUIRED["frontier"]))
    reading = item.get("reading") or {}
    extra = [key for key in reading if key not in allowed]
    for key in extra:
        reading.pop(key, None)
    if extra:
        _warn(item, report, "extra_field",
              f"模板 {template} 之外的字段已删除：{'、'.join(extra)}")


def _check_title_zh(item: dict, hard: list[str]) -> None:
    title_zh = (item.get("title_zh") or "").strip()
    if not title_zh:
        hard.append("中文标题为空")
    elif normalize_title(title_zh) == normalize_title(item.get("title")):
        hard.append("中文标题与原标题相同（未翻译）")


# ------------------------------------------------------------ 数值溯源


def _verify_claims(item: dict, cfg: dict, report: dict) -> None:
    """在模型实际看到的材料里反查每条 claim 的数值与 quote。

    反查范围是 `_material`（摘要头部 + 正文），不是 PDF 全文：模型看不到的
    地方即便真有这个数字，也不能算它说对了。quote 比对去掉空白与大小写，
    因为 PDF 抽取会把换行插在任意位置。
    """
    claims = item.get("claims") or []
    material = item.get("_material") or ""
    if not claims:
        return
    haystack = _squash(material)
    numeric_hay = re.sub(r"\s+", " ", material)
    kept: list[dict] = []
    dropped: list[dict] = []
    for claim in claims:
        missing = [n for n in (claim.get("numbers") or [])
                   if not _number_hit(str(n), numeric_hay)]
        quote = _squash(claim.get("quote") or "")
        quote_ok = bool(quote) and quote in haystack
        if missing or not quote_ok:
            dropped.append({
                "claim": claim.get("claim"),
                "missing_numbers": missing,
                "quote_found": quote_ok,
            })
            continue
        claim["verified"] = True
        kept.append(claim)

    item["claims"] = kept
    if not dropped:
        return
    item["validation"]["dropped_claims"] = dropped
    report["dropped_claims"] += len(dropped)
    ratio = len(dropped) / len(claims)
    numbers = [n for row in dropped for n in (row["missing_numbers"] or [])]
    _strip_sentences(item, numbers or _claim_numbers(dropped))
    log.warning("《%s》丢弃 %d/%d 条 claim（%.0f%%）",
                (item.get("title") or "")[:40], len(dropped), len(claims),
                ratio * 100)
    if ratio > float(cfg.get("max_dropped_claim_ratio") or 0.3):
        _downgrade(item, report, f"丢弃 claim 比例 {ratio:.0%} 超阈值")


def _claim_numbers(dropped: list[dict]) -> list[str]:
    """quote 查不到但数值都对的情况：按 claim 文本里的数字删句。"""
    out: list[str] = []
    for row in dropped:
        out += _NUMBER_RE.findall(row.get("claim") or "")
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _number_hit(number: str, text: str) -> bool:
    """数值反查必须带数字边界，不能用子串包含。

    120K 字符的全文里几乎任何短数字都能作为子串命中——实测注入的伪造值
    "99.7" 就因为落在别的数字内部而被判成"原文有"，只剩 quote 检查在挡。
    这里要求左右两侧都不是数字或小数点，于是 0.86 不会命中 10.864。
    """
    token = re.escape(number.strip().rstrip("%"))
    if not token:
        return False
    return bool(re.search(rf"(?<![\d.]){token}(?![\d])", text))


def _strip_sentences(item: dict, numbers: list[str]) -> None:
    """删掉解读里含这些数值的句子（DESIGN 6.2：数值查不到即删该句）。"""
    targets = {str(n) for n in numbers if str(n).strip()}
    if not targets:
        return
    reading = item.get("reading") or {}
    for key, value in list(reading.items()):
        if isinstance(value, str):
            reading[key] = _drop_from_text(value, targets)
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, dict) and isinstance(row.get("detail"), str):
                    row["detail"] = _drop_from_text(row["detail"], targets)


def _drop_from_text(text: str, targets: set[str]) -> str:
    kept = [s for s in _SENTENCE_SPLIT_RE.split(text)
            if not any(t in s for t in targets)]
    return "".join(kept).strip()


def _downgrade(item: dict, report: dict, reason: str) -> None:
    """整条降级为 abstract：清掉全部数值句与 claims，并留痕供前端标注。

    DESIGN 说"重新生成不含数值的解读"。这里不重新调模型：删句后字段若短于
    下限会在长度检查里记 warning，由前端按 `evidence_level: abstract` 显示；
    在流水线末端再开一次不可预算的 LLM 调用，风险大于收益。
    """
    all_numbers: list[str] = []
    reading = item.get("reading") or {}
    for value in reading.values():
        if isinstance(value, str):
            all_numbers += _NUMBER_RE.findall(value)
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    all_numbers += _NUMBER_RE.findall(row.get("detail") or "")
    _strip_sentences(item, all_numbers)
    item["claims"] = []
    item["evidence_level"] = "abstract"
    item.setdefault("quality", {})["evidence_level"] = "abstract"
    item["validation"]["downgraded_reason"] = reason
    report["downgraded"].append({"id": item.get("id"), "reason": reason})
    log.warning("《%s》降级为 abstract：%s", (item.get("title") or "")[:40], reason)


def _check_abstract_numbers(item: dict, report: dict) -> None:
    """abstract 级禁止出现实验数值：命中即删句并记 warning。"""
    if item.get("evidence_level") != "abstract":
        return
    reading = item.get("reading") or {}
    found: list[str] = []
    for value in reading.values():
        if isinstance(value, str):
            found += _NUMBER_RE.findall(value)
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    found += _NUMBER_RE.findall(row.get("detail") or "")
    # 年份与"1-3 条"这类序数不是实验数值，只拦小数与百分比
    numeric = [n for n in found if "." in n]
    if not numeric:
        return
    _strip_sentences(item, numeric)
    _warn(item, report, "abstract_numbers",
          f"摘要级解读出现实验数值 {numeric[:5]}，已删句")


# ------------------------------------------------------------ 文本检查


def _warn(item: dict, report: dict, kind: str, note: str) -> None:
    item["validation"]["warnings"].append({"kind": kind, "note": note})
    report["warnings"].append({"id": item.get("id"), "kind": kind, "note": note})


def _check_lengths(item: dict, cfg: dict, report: dict) -> None:
    """字数按 `text_length`（中文字 + 英文词各记 1）计，不按字符数。"""
    bounds = cfg.get("lengths") or {}
    reading = item.get("reading") or {}
    for field, pair in bounds.items():
        value = reading.get(field)
        if not isinstance(value, str) or not value:
            continue
        low, high = int(pair[0]), int(pair[1])
        size = text_length(value)
        if size < low or size > high:
            _warn(item, report, "length",
                  f"{field} 字数 {size} 超出 [{low}, {high}]")


_ASCII_RUN_RE = re.compile(r"(?:[A-Za-z][\w.'’-]*[ ,:]+){7,}[A-Za-z][\w.'’-]*")


def _check_untranslated(item: dict, cfg: dict, report: dict) -> None:
    """连续 N 个以上 ASCII 词视为未翻译残留（整段英文漏翻）。"""
    limit = int(cfg.get("untranslated_ascii_words") or 8)
    pattern = (_ASCII_RUN_RE if limit == 8 else
               re.compile(r"(?:[A-Za-z][\w.'’-]*[ ,:]+){%d,}[A-Za-z][\w.'’-]*"
                          % (limit - 1)))
    reading = item.get("reading") or {}
    for field, value in reading.items():
        # 字段可能是 null：prompt 要求的非必填字段模型会直接给 null，
        # 这里按"无文本"跳过，不能当成可迭代对象
        if isinstance(value, str):
            texts = [value]
        elif isinstance(value, list):
            texts = [row.get("detail") or "" for row in value
                     if isinstance(row, dict)]
        else:
            continue
        for text in texts:
            match = pattern.search(text or "")
            if match:
                _warn(item, report, "untranslated",
                      f"{field} 疑似未翻译残留：{match.group(0)[:60]}")
                break


def _normalize_spacing(item: dict) -> None:
    """中英文之间补一个空格；不动已有空格与标点。"""
    reading = item.get("reading") or {}
    for key, value in list(reading.items()):
        if isinstance(value, str):
            reading[key] = _space(value)
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    for sub in ("point", "detail"):
                        if isinstance(row.get(sub), str):
                            row[sub] = _space(row[sub])
    if item.get("title_zh"):
        item["title_zh"] = _space(item["title_zh"])


def _space(text: str) -> str:
    text = _SPACE_BEFORE_RE.sub(r"\1 \2", text)
    return _SPACE_AFTER_RE.sub(r"\1 \2", text)


# ------------------------------------------------------------ 链接与重复


def _check_links(item: dict, client: HttpClient, report: dict) -> None:
    """paper / code / project 逐个请求，期望 2xx/3xx。失败只记 warning。

    不因链接挂掉丢条目：GitHub 对 HEAD 偶发 403、项目页可能临时 502，
    把一篇好论文因此扣下来不划算。失败的链接从 `links` 里移除，前端就不会
    渲染一个点不开的按钮。
    """
    links = item.get("links") or {}
    for key in ("paper", "code", "project", "pdf"):
        url = links.get(key)
        if not url or not str(url).startswith("http"):
            continue
        try:
            resp = client.request("HEAD", url)
            code = resp.status_code
            if code in (403, 405):
                # 部分站点不允许 HEAD，用 GET 再确认一次
                code = client.get(url).status_code
        except SourceError as exc:
            code = 0
            log.warning("链接不可达 %s：%s", url, exc)
        if not (200 <= code < 400):
            report["link_failures"].append({"id": item.get("id"), "url": url,
                                            "status": code})
            _warn(item, report, "link", f"{key} 链接不可达（{code}）：{url}")
            links.pop(key, None)


def _check_overlap(items: list[dict], threshold: float, report: dict) -> None:
    """当期内两条解读的 3-gram 重叠率超阈值即告警（模板化套话的信号）。"""
    grams = [(item, _ngrams(_reading_text(item), 3)) for item in items]
    for i in range(len(grams)):
        for j in range(i + 1, len(grams)):
            left, right = grams[i][1], grams[j][1]
            if not left or not right:
                continue
            overlap = len(left & right) / min(len(left), len(right))
            if overlap > threshold:
                pair = {"a": grams[i][0].get("id"), "b": grams[j][0].get("id"),
                        "overlap": round(overlap, 3)}
                report["warnings"].append({"kind": "ngram_overlap", **pair})
                log.warning("解读重叠率 %.2f 超阈值：%s vs %s",
                            overlap, pair["a"], pair["b"])


def _reading_text(item: dict) -> str:
    reading = item.get("reading") or {}
    parts: list[str] = []
    for value in reading.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts += [f"{row.get('point','')}{row.get('detail','')}"
                      for row in value if isinstance(row, dict)]
    return "".join(parts)


def _ngrams(text: str, size: int) -> set[str]:
    """按字符切 n-gram：中文无词边界，字符级比词级更稳。"""
    clean = re.sub(r"\s+", "", text)
    if len(clean) < size:
        return set()
    return {clean[i:i + size] for i in range(len(clean) - size + 1)}
