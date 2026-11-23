"""分级取材：把一条候选变成喂给 L2 的正文材料（DESIGN 4.6）。

三个层级对应三种成本：`abstract`（0.5K token，不下载任何东西）、
`intro_conclusion`（5–8K，用于约 30 条的 L2 初筛）、`fulltext`（20–30K，
只给最终入选的 10–12 条）。取材层级会原样写进条目的 `evidence_level`，
解读里能说什么、校验层反查什么，都以它为准——**只有摘要时不得声称读过全文**。

PDF 下载失败不是硬错误：降级到 `abstract` 并记录原因，由解读阶段按
"不得输出实验数值"的约束继续处理，不让单条 PDF 404 掐断当天流水线。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .common import DATA_DIR, HttpClient, SourceError, load_config, log

_PDF_DIR = DATA_DIR / "state" / "pdf"
# PDF 文本缓存随 data 分支长期保留，按文件数封顶：12 条/天 × 60KB 一年是
# 250MB，clone 会越来越慢。保留最近 500 篇覆盖冷却期内的全部重跑场景。
_PDF_CACHE_KEEP = 500

# Intro / Conclusion 的节标题写法很杂：有编号（"1 Introduction"、"I. INTRODUCTION"）、
# 有复数（"Conclusions"）、有合写（"Conclusion and Future Work"）。
_SECTION_RE = re.compile(
    r"^\s*(?:(?:\d{1,2}|[IVX]{1,4})[.、)]?\s+)?"
    r"(introduction|related\s+work|conclusions?|discussions?\s+and\s+conclusions?|"
    r"conclusions?\s+and\s+(?:future|outlook)[\w\s]*|summary)\s*$",
    re.IGNORECASE,
)
_CAPTION_RE = re.compile(r"^\s*(table|figure|fig\.)\s*\d+[.:]\s*\S", re.IGNORECASE)
_WANTED = ("introduction", "related work", "conclusion", "summary", "discussion")


@dataclass
class Material:
    """取材结果。`text` 直接进 prompt，`fulltext` 供校验层做数值反查。"""

    level: str
    text: str
    fulltext: str = ""
    pages: int = 0
    note: str | None = None

    @property
    def has_body(self) -> bool:
        return self.level in ("intro_conclusion", "fulltext")


def build_material(item: dict, *, level: str,
                   client: HttpClient | None = None) -> Material:
    """按 level 取材；PDF 不可用时静默降级为 abstract 并在 note 里写明原因。"""
    head = _head(item)
    if level == "abstract" or item.get("content_type") == "repo":
        return Material(level="abstract", text=head)

    cfg = (load_config("scoring")["summarize"].get("material") or {})
    try:
        pdf_text, pages = _pdf_text(item, client)
    except SourceError as exc:
        log.warning("取材降级：%s 无法取得 PDF（%s）",
                    item.get("arxiv_id") or item.get("id"), exc)
        return Material(level="abstract", text=head, note=f"PDF 不可用：{exc}")

    if level == "fulltext":
        limit = int(cfg.get("fulltext_chars") or 120000)
        body = pdf_text[:limit]
        return Material(level="fulltext", text=f"{head}\n\n## 论文全文\n{body}",
                        fulltext=pdf_text, pages=pages,
                        note="全文超长已截断" if len(pdf_text) > limit else None)

    limit = int(cfg.get("intro_conclusion_chars") or 24000)
    excerpt = _excerpt(pdf_text, limit)
    if not excerpt:
        # 抽到了页面但认不出节标题（常见于单栏 workshop 稿），退回取前后两段
        excerpt = (pdf_text[: limit // 2] + "\n…\n" + pdf_text[-limit // 2:])[:limit]
    return Material(level="intro_conclusion",
                    text=f"{head}\n\n## 正文节选\n{excerpt}",
                    fulltext=pdf_text, pages=pages)


# ------------------------------------------------------------ 组装


def _head(item: dict) -> str:
    """标题 + 元数据 + 摘要。三个层级共用，放在 prompt 固定段之后。"""
    rows = [f"# {item.get('title') or '(无标题)'}"]
    authors = item.get("authors") or []
    if authors:
        shown = "、".join(authors[:6]) + ("等" if len(authors) > 6 else "")
        rows.append(f"作者：{shown}")
    for label, key in (("arXiv", "arxiv_id"), ("主分类", "primary_category"),
                       ("发布", "published_at"), ("版本", "arxiv_version")):
        if item.get(key):
            rows.append(f"{label}：{item[key]}")
    if item.get("journal_ref"):
        rows.append(f"期刊/会议：{item['journal_ref']}")
    if item.get("comment"):
        rows.append(f"作者备注：{item['comment']}")
    links = item.get("links") or {}
    for label, key in (("代码", "code"), ("项目页", "project")):
        if links.get(key):
            rows.append(f"{label}：{links[key]}")
    rows.append(f"\n## 摘要\n{item.get('abstract') or '(无摘要)'}")
    return "\n".join(rows)


def _excerpt(text: str, limit: int) -> str:
    """只保留 Intro / Related Work / Conclusion 与表格 caption。

    不做语义切分：按行扫节标题，命中 `_WANTED` 就开始收，遇到下一个不在
    名单里的节标题就停。表格 caption 单独收集——实验结论的数字多半在那里，
    而 L2 初筛不读全文，caption 是唯一能看到数值的地方。
    """
    kept: list[str] = []
    captions: list[str] = []
    collecting = False
    for line in text.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            name = match.group(1).lower()
            collecting = any(w in name for w in _WANTED)
            if collecting:
                kept.append(f"\n### {line.strip()}")
            continue
        if _CAPTION_RE.match(line):
            captions.append(line.strip())
        elif collecting:
            kept.append(line)
    body = "\n".join(kept).strip()
    if not body:
        return ""
    if captions:
        body += "\n\n### 表格与图表说明\n" + "\n".join(captions[:40])
    return body[:limit]


# ------------------------------------------------------------ PDF


def _pdf_text(item: dict, client: HttpClient | None) -> tuple[str, int]:
    """下载并抽取 PDF 文本，按 arXiv ID 缓存到 data/state/pdf。

    缓存的是抽取后的 .txt 而不是 PDF 本身：重跑时省掉下载和解析两步，
    且文本体积只有 PDF 的几分之一，不会把 data 分支撑大。

    放 state 而不是 raw：data 分支忽略 raw/（那是可重算的中间产物），
    缓存落在那里等于每次 CI 都重抽一遍——实测一篇 42 页的论文抽取要
    2 分 38 秒，12 条就是半小时，整期因此撞上 job 的 90 分钟上限。
    """
    key = (item.get("arxiv_id") or item.get("id") or "").replace("/", "--")
    if not key:
        raise SourceError("条目没有 arxiv_id，无法定位 PDF")
    cached = _PDF_DIR / f"{key}.txt"
    if cached.exists():
        text = cached.read_text(encoding="utf-8")
        if text.strip():
            return text, text.count("\f") + 1

    url = (item.get("links") or {}).get("pdf") or (
        f"https://arxiv.org/pdf/{item['arxiv_id']}" if item.get("arxiv_id") else None)
    if not url:
        raise SourceError("条目没有 PDF 链接")

    owned = client is None
    client = client or HttpClient(interval=3.0, timeout=90.0)
    try:
        resp = client.get(url)
        if resp.status_code != 200:
            raise SourceError(f"PDF 返回 {resp.status_code}")
        text, pages = _extract(resp.content)
    finally:
        if owned:
            client.close()

    _PDF_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    _prune_pdf_cache()
    log.info("取材：%s 抽出 %d 页 / %d 字符", key, pages, len(text))
    return text, pages


def _prune_pdf_cache() -> None:
    """超出上限时按修改时间删最旧的几篇。删错了代价只是下次重抽一遍。"""
    files = sorted(_PDF_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    for path in files[:max(0, len(files) - _PDF_CACHE_KEEP)]:
        path.unlink(missing_ok=True)


def _extract(blob: bytes) -> tuple[str, int]:
    """pymupdf 抽文本。参考文献之后全部截掉——占全文近三成且对解读无用。"""
    import pymupdf  # 延迟导入：只有真要读 PDF 的运行才需要它

    try:
        doc = pymupdf.open(stream=blob, filetype="pdf")
    except Exception as exc:  # pymupdf 的异常类型随版本变化
        raise SourceError(f"PDF 解析失败：{exc}") from exc
    with doc:
        # 页内自带的 \f 要抹掉：缓存命中时只能靠数 \f 还原页数，
        # 留着它会让同一篇论文两次取材报出不同页数（实测 63 页报成 71）。
        pages = [page.get_text("text").replace("\f", " ") for page in doc]
    text = "\f".join(pages)
    if not text.strip():
        raise SourceError("PDF 无可抽取文本（可能是扫描件）")
    return _drop_references(text), len(pages)


_REF_RE = re.compile(r"^\s*(?:\d{1,2}[.、)]?\s*)?(references|bibliography)\s*$",
                     re.IGNORECASE | re.MULTILINE)


def _drop_references(text: str) -> str:
    """从最后一个 References 标题处截断，保留其后的 Appendix 判断留给调用方。"""
    matches = list(_REF_RE.finditer(text))
    if not matches:
        return text
    cut = matches[-1].start()
    # 参考文献出现在前 40% 处说明匹配到了正文引述，不截
    return text if cut < len(text) * 0.4 else text[:cut]


def material_stats(materials: dict[str, Material]) -> dict[str, Any]:
    levels: dict[str, int] = {}
    for material in materials.values():
        levels[material.level] = levels.get(material.level, 0) + 1
    return {"levels": levels,
            "degraded": [key for key, m in materials.items() if m.note]}
