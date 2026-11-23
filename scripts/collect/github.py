"""GitHub 采集：trending 解析 + Search API 基线池 + 每日 star 快照。

双通道设计用于绕开"冷启动悖论"：自攒快照只能给已跟踪的仓库算增量，
而刚爆火的新仓库首次出现时没有历史数据，故以 trending 页补足爆发信号。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from bs4 import BeautifulSoup

from ..common import (
    HttpClient,
    SourceError,
    env,
    find_arxiv_id,
    load_config,
    log,
    read_json,
    stars_path,
    write_json,
)

_STARS_TODAY_RE = re.compile(r"([\d,]+)\s+stars?\s+(?:today|this week)")


def collect(batch: date, *, build_baseline: bool = True) -> dict[str, Any]:
    """返回 AI 相关仓库候选，含 trending 命中标记与 star 增量。"""
    cfg = load_config("sources")["github"]
    if not cfg.get("enabled", True):
        return {"ok": True, "items": [], "stats": {"skipped": True}}

    token = env("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        log.warning("未设置 GITHUB_TOKEN，Search API 限速 10 次/分钟，基线池将缩减")

    stats: dict[str, Any] = {"trending": 0, "baseline": 0, "authenticated": bool(token),
                             "requests": 0, "detail_failures": 0}
    repos: dict[str, dict[str, Any]] = {}

    client = HttpClient(
        interval=cfg["request_interval_seconds"],
        timeout=cfg["timeout_seconds"],
        max_retries=cfg["max_retries"],
        headers=headers,
    )
    try:
        # 通道 A：trending（无需认证，解决冷启动）
        for window in cfg["trending_windows"]:
            for name in _scrape_trending(client, cfg, window, stats):
                entry = repos.setdefault(name, {"full_name": name,
                                                "trending_windows": []})
                entry["trending_windows"].append(window)
        stats["trending"] = len(repos)

        # 通道 B：Search API 基线池（提供增量计算的跟踪对象）
        if build_baseline:
            baseline = _search_baseline(client, cfg, token, stats)
            for name, payload in baseline.items():
                entry = repos.setdefault(name, {"full_name": name,
                                                "trending_windows": []})
                entry["payload"] = payload
            stats["baseline"] = len(baseline)

        # 取详情并过滤
        items = []
        for name, seed in repos.items():
            # Search 返回已含 _repo_detail 需要的全部字段，基线池再逐个打
            # /repos/{name} 就是 2500 次白付的请求：按 1 秒间隔要 40 多分钟，
            # CI 实测采集步骤 59 分钟几乎全耗在这里。只有 trending 页抓来的
            # 名字没有 payload，才真的需要单独取一次详情。
            payload = seed.get("payload")
            detail = (_repo_detail(payload) if payload
                      else _fetch_repo(client, cfg, name, stats))
            if detail is None:
                continue
            if not _is_ai_repo(detail, cfg):
                continue
            detail["trending_windows"] = seed["trending_windows"]
            detail["is_trending"] = bool(seed["trending_windows"])
            items.append(detail)
    except SourceError as exc:
        log.error("GitHub 采集失败: %s", exc)
        return {"ok": False, "items": [], "stats": stats, "error": str(exc)}
    finally:
        client.close()

    snapshot = _record_snapshot(batch, items)
    for item in items:
        item.update(_growth(batch, item["full_name"], item["stars"], snapshot))

    stats["kept"] = len(items)
    log.info("GitHub: trending %d，基线 %d，过滤后保留 %d",
             stats["trending"], stats["baseline"], len(items))
    return {"ok": True, "items": items, "stats": stats}


def _scrape_trending(client: HttpClient, cfg: dict, window: str,
                     stats: dict) -> list[str]:
    """解析 trending 页，返回 owner/repo 列表。非官方接口，失败只告警。"""
    try:
        resp = client.get(cfg["trending_url"], params={"since": window})
        stats["requests"] += 1
    except SourceError as exc:
        log.warning("trending(%s) 不可达，跳过该通道: %s", window, exc)
        return []
    if resp.status_code != 200:
        log.warning("trending(%s) 返回 %d", window, resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    names = []
    for row in soup.select("article.Box-row"):
        link = row.select_one("h2 a")
        href = (link.get("href") if link else "") or ""
        name = href.strip("/")
        if name.count("/") == 1:
            names.append(name)
    log.info("trending(%s): %d 个仓库", window, len(names))
    return names


def _search_baseline(client: HttpClient, cfg: dict, token: str | None,
                     stats: dict) -> dict[str, dict]:
    """按 star 区间 × 关键词分段搜索，绕开单次查询 1000 条返回上限。

    返回 {full_name: 搜索结果原文}：调用方直接用这份 payload 建条目，不再
    回头逐个请求 /repos/{name}。
    """
    target = cfg["baseline_target"] if token else 300
    pushed_after = (date.today()
                    - timedelta(days=cfg["pushed_within_days"])).isoformat()
    # 无 token 时限速 10 次/分钟，缩减关键词与分页
    keywords = cfg["keywords"] if token else cfg["keywords"][:3]
    per_bucket_pages = 3 if token else 1

    found: dict[str, dict] = {}
    for bucket in cfg["star_buckets"]:
        for keyword in keywords:
            if len(found) >= target:
                return found
            query = (f'{keyword} in:name,description,readme '
                     f'stars:{bucket} pushed:>{pushed_after}')
            for page in range(1, per_bucket_pages + 1):
                try:
                    resp = client.get(
                        f"{cfg['api_base']}/search/repositories",
                        params={"q": query, "sort": "stars", "order": "desc",
                                "per_page": 100, "page": page},
                    )
                    stats["requests"] += 1
                except SourceError as exc:
                    log.warning("Search API 失败，跳过该查询: %s", exc)
                    break
                if resp.status_code == 403:
                    log.warning("Search API 配额耗尽，基线池提前结束")
                    return found
                if resp.status_code != 200:
                    break
                payload = resp.json().get("items") or []
                if not payload:
                    break
                for item in payload:
                    found.setdefault(item["full_name"], item)
    return found


def _fetch_repo(client: HttpClient, cfg: dict, full_name: str,
                stats: dict) -> dict[str, Any] | None:
    try:
        resp = client.get(f"{cfg['api_base']}/repos/{full_name}")
        stats["requests"] += 1
    except SourceError:
        stats["detail_failures"] += 1
        return None
    # 配额耗尽时立刻中止，否则会把剩余仓库全部记成"失败"并静默返回空结果
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset", "?")
        raise SourceError(
            f"GitHub REST 配额耗尽（limit={resp.headers.get('x-ratelimit-limit')}，"
            f"reset={reset}）。请设置 GITHUB_TOKEN。"
        )
    if resp.status_code != 200:
        stats["detail_failures"] += 1
        return None
    data = resp.json()
    return _repo_detail(data)


def _repo_detail(data: dict) -> dict[str, Any] | None:
    """把 /repos 或 search 的返回整成候选条目；归档/派生/停用的仓库丢弃。

    两个接口的字段名一致（search 的 items 就是精简版 repository 对象），
    所以建条目的逻辑只写一份。
    """
    if data.get("archived") or data.get("fork") or data.get("disabled"):
        return None

    description = data.get("description") or ""
    return {
        "source": "github",
        "full_name": data["full_name"],
        "name": data.get("name"),
        "owner": (data.get("owner") or {}).get("login"),
        "description": description,
        "topics": data.get("topics") or [],
        "language": data.get("language"),
        "stars": int(data.get("stargazers_count") or 0),
        "forks": int(data.get("forks_count") or 0),
        "open_issues": int(data.get("open_issues_count") or 0),
        "created_at": data.get("created_at"),
        "pushed_at": data.get("pushed_at"),
        "license": ((data.get("license") or {}).get("spdx_id")),
        "homepage": data.get("homepage") or None,
        "size_kb": data.get("size"),
        "links": {
            "code": data.get("html_url"),
            "project": data.get("homepage") or None,
        },
        # README 里的 arXiv 链接是论文合并的关键线索，在 normalize 阶段补全
        "arxiv_id_hint": find_arxiv_id(description) or find_arxiv_id(
            data.get("homepage") or ""),
    }


def _is_ai_repo(detail: dict, cfg: dict) -> bool:
    """关键词命中 AI 相关，且不属于资源清单/教程类。"""
    name_lower = detail["full_name"].lower()
    for pattern in cfg["exclude_name_patterns"]:
        if pattern in name_lower:
            return False

    haystack = " ".join([
        name_lower,
        (detail["description"] or "").lower(),
        " ".join(detail["topics"]).lower(),
    ])
    return any(keyword.lower() in haystack for keyword in cfg["keywords"])


# ------------------------------------------------- star 快照与增量


def _record_snapshot(batch: date, items: list[dict]) -> dict[str, Any]:
    """把当日 star 数写入按月分片的快照文件，供后续算真实增量。"""
    path = stars_path(batch)
    shard = read_json(path, default={}) or {}
    key = batch.isoformat()
    for item in items:
        shard.setdefault(item["full_name"], {})[key] = item["stars"]
    write_json(path, shard, indent=0)
    log.info("star 快照：%d 个仓库写入 %s", len(items), path.name)
    return shard


def _growth(batch: date, full_name: str, stars_now: int,
            current_shard: dict) -> dict[str, Any]:
    """用历史快照算 7d / 30d 增量。无历史数据时返回 None 而非 0。"""
    history: dict[str, int] = dict(current_shard.get(full_name) or {})
    # 30 天窗口可能跨月，补读上一月分片
    prev_month = batch.replace(day=1) - timedelta(days=1)
    if prev_month.month != batch.month:
        prev_shard = read_json(stars_path(prev_month), default={}) or {}
        history.update(prev_shard.get(full_name) or {})

    result: dict[str, Any] = {"stars_7d": None, "stars_30d": None,
                              "snapshot_days": len(history)}
    for span, field in ((7, "stars_7d"), (30, "stars_30d")):
        baseline = _nearest_before(history, batch - timedelta(days=span))
        if baseline is not None:
            result[field] = stars_now - baseline
    return result


def _nearest_before(history: dict[str, int], target: date) -> int | None:
    """取不晚于 target 的最近一次快照值；容忍 ±3 天漂移。"""
    best_key, best_gap = None, None
    for key in history:
        try:
            snapped = date.fromisoformat(key)
        except ValueError:
            continue
        gap = abs((snapped - target).days)
        if snapped <= target + timedelta(days=3) and (
            best_gap is None or gap < best_gap
        ):
            best_key, best_gap = key, gap
    if best_key is None or best_gap is None or best_gap > 10:
        return None
    return history[best_key]



