"""arXiv 采集：OAI-PMH 按日期范围增量拉取。

选用 OAI-PMH 而非 Search API + sortBy=submittedDate，因为后者在深分页时
存在重复与缺页，不适合每天上千条的全量采集。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Iterator

from ..common import (
    HttpClient,
    SourceError,
    iso,
    load_config,
    log,
    parse_dt,
    split_arxiv_id,
)

OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"
RAW_NS = "{http://arxiv.org/OAI/arXivRaw/}"

_AUTHOR_SPLIT_RE = re.compile(r",\s*|\s+and\s+", re.IGNORECASE)
_CODE_URL_RE = re.compile(r"https?://(?:github\.com|gitlab\.com)/[\w.\-/]+")


def collect(
    batch: date,
    *,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    """拉取 [batch - lookback, batch] 的 OAI 记录并按分类过滤。

    返回 {"ok", "items", "stats", "error"}；失败不抛出，由调用方隔离。
    """
    cfg = load_config("sources")["arxiv"]
    if not cfg.get("enabled", True):
        return {"ok": True, "items": [], "stats": {"skipped": True}}

    lookback = lookback_days if lookback_days is not None else cfg["lookback_days"]

    high = set(cfg["categories_high"])
    medium = set(cfg["categories_medium"])
    # arXiv 的 OAI 对多日窗口不可靠：实测 from=11-19&until=11-23 只返回
    # 11-19 一天的记录且不给 resumptionToken（静默丢数据），而
    # from=11-22&until=11-23 在 11-22 无记录时直接返回 noRecordsMatch
    # （尽管 11-23 有 1192 条）。因此一律逐日查询，from == until。
    window = [batch - timedelta(days=offset) for offset in range(lookback + 1)]
    # arXiv 周六周日不公告：实测 2026-11-21（周六）与 11-22（周日）的
    # datestamp 均为 0 条，而 11-20（周五）1078 条、11-23（周一）910 条。
    # 跳过这两天，每天每个 set 省一次必然空的请求。
    days = [day for day in window if day.weekday() < 5]
    weekends = [day.isoformat() for day in window if day.weekday() >= 5]
    stats = {"fetched": 0, "deleted": 0, "kept": 0, "requests": 0,
             "days": [day.isoformat() for day in days],
             "skipped_weekends": weekends,
             "by_day": {}, "by_set": {}, "empty_days": []}
    items: dict[str, dict[str, Any]] = {}

    client = HttpClient(
        interval=cfg["request_interval_seconds"],
        timeout=cfg["timeout_seconds"],
        max_retries=cfg["max_retries"],
    )
    try:
        for day in days:
            day_before = len(items)
            for set_spec in cfg["sets"]:
                set_before = len(items)
                for record in _iter_records(client, cfg, set_spec, day, stats):
                    parsed = _parse_record(record, stats)
                    if parsed is None:
                        continue
                    cats = set(parsed["categories"])
                    if not (cats & high or cats & medium):
                        continue
                    parsed["category_tier"] = "high" if cats & high else "medium"
                    # 同一论文可能同时出现在 cs 与 stat，按 ID 合并
                    items.setdefault(parsed["arxiv_id"], parsed)
                stats["by_set"][set_spec] = (
                    stats["by_set"].get(set_spec, 0) + len(items) - set_before
                )
            added = len(items) - day_before
            stats["by_day"][day.isoformat()] = added
            if added == 0:
                stats["empty_days"].append(day.isoformat())
    except SourceError as exc:
        log.error("arXiv 采集失败: %s", exc)
        return {"ok": False, "items": list(items.values()),
                "stats": stats, "error": str(exc)}
    finally:
        client.close()

    stats["kept"] = len(items)
    log.info("arXiv: 抓取 %d 条，过滤后保留 %d 条（%d 次请求，按日 %s）",
             stats["fetched"], stats["kept"], stats["requests"], stats["by_day"])
    return {"ok": True, "items": list(items.values()), "stats": stats}


def _iter_records(
    client: HttpClient,
    cfg: dict,
    set_spec: str,
    day: date,
    stats: dict,
) -> Iterator[ET.Element]:
    """遍历单日单 set 的全部分页，自动跟随 resumptionToken。"""
    stamp = day.isoformat()
    params: dict[str, str] = {
        "verb": "ListRecords",
        "set": set_spec,
        "metadataPrefix": cfg["metadata_prefix"],
        "from": stamp,
        "until": stamp,
    }
    page = 0
    while True:
        resp = client.get(cfg["oai_endpoint"], params=params)
        stats["requests"] += 1
        if resp.status_code != 200:
            raise SourceError(f"OAI {set_spec}@{stamp} HTTP {resp.status_code}")
        root = ET.fromstring(resp.text)

        error = root.find(f"{OAI_NS}error")
        if error is not None:
            code = error.get("code", "")
            # 周末与节假日无公告，属正常情况
            if code == "noRecordsMatch":
                log.debug("arXiv %s@%s 无记录", set_spec, stamp)
                return
            raise SourceError(f"OAI error {code}@{stamp}: {error.text}")

        listing = root.find(f"{OAI_NS}ListRecords")
        if listing is None:
            return
        page += 1
        yield from listing.findall(f"{OAI_NS}record")

        token_el = listing.find(f"{OAI_NS}resumptionToken")
        token = (token_el.text or "").strip() if token_el is not None else ""
        if not token:
            return
        # 续传请求只允许带 verb 与 resumptionToken
        params = {"verb": "ListRecords", "resumptionToken": token}


def _text(node: ET.Element | None, tag: str) -> str | None:
    if node is None:
        return None
    child = node.find(f"{RAW_NS}{tag}")
    if child is None or child.text is None:
        return None
    value = re.sub(r"\s+", " ", child.text).strip()
    return value or None


def _parse_record(record: ET.Element, stats: dict) -> dict[str, Any] | None:
    header = record.find(f"{OAI_NS}header")
    if header is not None and header.get("status") == "deleted":
        stats["deleted"] += 1
        return None

    meta = record.find(f"{OAI_NS}metadata")
    raw = meta.find(f"{RAW_NS}arXivRaw") if meta is not None else None
    if raw is None:
        return None
    stats["fetched"] += 1

    raw_id = _text(raw, "id")
    title = _text(raw, "title")
    abstract = _text(raw, "abstract")
    if not raw_id or not title or not abstract:
        return None

    arxiv_id, _ = split_arxiv_id(raw_id)

    # <version version="vN"><date>...</date></version>，按版本号排序
    versions: list[tuple[int, str | None]] = []
    for node in raw.findall(f"{RAW_NS}version"):
        label = node.get("version", "")
        num = int(label.lstrip("v")) if label.lstrip("v").isdigit() else 0
        date_el = node.find(f"{RAW_NS}date")
        versions.append((num, date_el.text if date_el is not None else None))
    versions.sort(key=lambda pair: pair[0])

    published = parse_dt(versions[0][1]) if versions else None
    updated = parse_dt(versions[-1][1]) if versions else None
    version_count = max((num for num, _ in versions), default=1)

    categories = (_text(raw, "categories") or "").split()
    comment = _text(raw, "comments")
    datestamp = None
    if header is not None:
        ds = header.find(f"{OAI_NS}datestamp")
        datestamp = ds.text if ds is not None else None

    return {
        "source": "arxiv",
        "arxiv_id": arxiv_id,
        "arxiv_version": f"v{version_count}",
        "version_count": version_count,
        "title": title,
        "authors": _parse_authors(_text(raw, "authors")),
        "abstract": abstract,
        "published_at": iso(published),
        "updated_at": iso(updated),
        "primary_category": categories[0] if categories else None,
        "categories": categories,
        "comment": comment,
        "doi": _text(raw, "doi"),
        "journal_ref": _text(raw, "journal-ref"),
        "license": _text(raw, "license"),
        "oai_datestamp": datestamp,
        "links": {
            "paper": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
            "code": _extract_code_url(comment),
            "project": None,
        },
    }


def _parse_authors(raw: str | None) -> list[str]:
    """arXivRaw 的 authors 是自由文本，如 'A B, C D and E F'。"""
    if not raw:
        return []
    raw = re.sub(r"\(.*?\)", "", raw)  # 去掉机构括注
    names = [name.strip() for name in _AUTHOR_SPLIT_RE.split(raw)]
    return [name for name in names if name and len(name) > 1][:40]


def _extract_code_url(comment: str | None) -> str | None:
    """comment 字段常含代码链接，如 'Code: https://github.com/x/y'。"""
    if not comment:
        return None
    match = _CODE_URL_RE.search(comment)
    if not match:
        return None
    return match.group(0).rstrip(".,;)")




# --------------------------------------------- 按 ID 回查（供校验层核验元数据）

_QUERY_API = "https://export.arxiv.org/api/query"
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def fetch_by_ids(arxiv_ids: list[str], *,
                 client: HttpClient | None = None) -> dict[str, dict[str, Any]]:
    """用 Query API 的 id_list 回查官方元数据，返回 {arxiv_id: {...}}。

    校验层要拿官方标题/作者/日期和解读条目逐字比对，而 OAI-PMH 只能按日期
    范围取，拿不到单条。这里用 Query API：一次最多 10 个 ID（实测 20 个容易
    被 429），批间隔由 HttpClient 的 interval 控制。查不到的 ID 不出现在返回里，
    调用方据此判定"ID 不存在"。
    """
    out: dict[str, dict[str, Any]] = {}
    ids = [split_arxiv_id(i)[0] for i in arxiv_ids if i]
    if not ids:
        return out
    owned = client is None
    client = client or HttpClient(interval=6.0, timeout=60.0, max_retries=5)
    try:
        for start in range(0, len(ids), 10):
            chunk = ids[start:start + 10]
            resp = client.get(_QUERY_API, params={"id_list": ",".join(chunk),
                                                  "max_results": len(chunk)})
            if resp.status_code != 200:
                raise SourceError(f"arXiv Query API 返回 {resp.status_code}")
            out.update(_parse_atom(resp.text))
    finally:
        if owned:
            client.close()
    log.info("arXiv 回查：请求 %d 条，命中 %d 条", len(ids), len(out))
    return out


def _parse_atom(xml: str) -> dict[str, dict[str, Any]]:
    root = ET.fromstring(xml)
    out: dict[str, dict[str, Any]] = {}
    for entry in root.findall(f"{_ATOM_NS}entry"):
        raw_id = (entry.findtext(f"{_ATOM_NS}id") or "").rsplit("/", 1)[-1]
        base, version = split_arxiv_id(raw_id)
        if not base:
            continue
        title = " ".join((entry.findtext(f"{_ATOM_NS}title") or "").split())
        authors = [" ".join((node.findtext(f"{_ATOM_NS}name") or "").split())
                   for node in entry.findall(f"{_ATOM_NS}author")]
        out[base] = {
            "arxiv_id": base,
            "arxiv_version": version,
            "title": title,
            "authors": [a for a in authors if a],
            "published_at": iso(parse_dt(entry.findtext(f"{_ATOM_NS}published"))),
            "updated_at": iso(parse_dt(entry.findtext(f"{_ATOM_NS}updated"))),
        }
    return out
