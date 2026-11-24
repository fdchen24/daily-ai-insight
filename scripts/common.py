"""共享工具：路径、配置、限速 HTTP、时间与标题规范化、分片 JSON 存储。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
# 数据落在独立的 data 分支，CI 里把该分支 checkout 到子目录后用
# DAI_DATA_DIR 指过去；本地开发默认仍是仓库内的 data/。
DATA_DIR = Path(os.environ.get("DAI_DATA_DIR") or (ROOT / "data")).resolve()
CN_TZ = timezone(timedelta(hours=8))

log = logging.getLogger("dai")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


_config_cache: dict[str, Any] = {}


def load_config(name: str) -> dict:
    """加载 config/<name>.yaml，带进程内缓存。"""
    if name not in _config_cache:
        path = CONFIG_DIR / f"{name}.yaml"
        with path.open(encoding="utf-8") as fh:
            _config_cache[name] = yaml.safe_load(fh) or {}
    return _config_cache[name]


_prompt_cache: dict[str, str] = {}


def load_prompt(name: str) -> str:
    """加载 config/prompts/<name>.md 作为 system prompt。

    放在文件而不是代码里：prompt 要反复调，改 Markdown 不必改脚本；
    且固定内容单独成块，便于靠 DeepSeek 的 prefix cache 复用。
    """
    if name not in _prompt_cache:
        path = CONFIG_DIR / "prompts" / f"{name}.md"
        _prompt_cache[name] = path.read_text(encoding="utf-8").strip()
    return _prompt_cache[name]


def prompt_fingerprint(name: str) -> str:
    """prompt 内容的短哈希，用于进缓存键。

    缓存键只按模板名算的话，改完 prompt 重跑会原样复用旧解读——人以为在
    看新 prompt 的效果，实际读的是上一版的输出。把内容摘要进键，改 prompt
    即自然失效，不改则照旧命中。
    """
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------- 时间


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """统一输出带时区的 ISO8601。裸 datetime 按 UTC 补齐。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_dt(value: str | None) -> datetime | None:
    """宽松解析各源的时间字符串，无时区者按 UTC 处理。"""
    if not value:
        return None
    try:
        dt = dateparser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def announce_date(override: str | None = None) -> date:
    """本期对应的 arXiv 公告批次日（UTC 日期）。

    arXiv 在美东 20:00 公告，流水线在 02:30 UTC 运行，即公告之后。
    批次日一律取运行时的 UTC 日期，不依赖本地时区，便于幂等重跑。
    """
    if override:
        return date.fromisoformat(override)
    return utcnow().date()


def days_since(dt: datetime | None, ref: datetime | None = None) -> float | None:
    if dt is None:
        return None
    ref = ref or utcnow()
    return (ref - dt).total_seconds() / 86400.0


# ---------------------------------------------------------------- HTTP


class SourceError(RuntimeError):
    """单个数据源失败。调用方应隔离该源而不是中断流水线。"""


class HttpClient:
    """带最小请求间隔、指数退避和 Retry-After 处理的同步客户端。"""

    def __init__(
        self,
        interval: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> None:
        ua = load_config("sources").get("user_agent", "DailyAIInsight/2.0")
        self.interval = interval
        self.max_retries = max_retries
        self._last_request = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": ua, **(headers or {})},
        )

    def _wait(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.interval:
            time.sleep(self.interval - gap)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._wait()
            try:
                resp = self._client.request(method, url, **kwargs)
                self._last_request = time.monotonic()
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("%s %s 网络错误 (%d/%d): %s",
                            method, url, attempt + 1, self.max_retries, exc)
                time.sleep(2 ** attempt * self.interval)
                continue
            if resp.status_code in (429, 503):
                delay = _retry_after(resp, 2 ** attempt * max(self.interval, 5.0))
                log.warning("%s %s 限流 %s，等待 %.0fs",
                            method, url, resp.status_code, delay)
                time.sleep(delay)
                last_error = SourceError(f"{resp.status_code} on {url}")
                continue
            if resp.status_code >= 500:
                last_error = SourceError(f"{resp.status_code} on {url}")
                time.sleep(2 ** attempt * self.interval)
                continue
            return resp
        raise SourceError(f"{method} {url} 重试耗尽: {last_error}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _retry_after(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("Retry-After")
    if raw and raw.isdigit():
        return min(float(raw), 120.0)
    return default


# ------------------------------------------------- arXiv ID 与标题规范化

_ARXIV_VERSION_RE = re.compile(r"^(?P<base>.+?)(?P<ver>v\d+)?$")
_ARXIV_ID_RE = re.compile(
    r"(?:arxiv[:/\s]*)?(?P<id>\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"
    r"(?P<ver>v\d+)?",
    re.IGNORECASE,
)


def split_arxiv_id(raw: str) -> tuple[str, str | None]:
    """'2510.18927v2' -> ('2510.18927', 'v2')。去重与存储一律用 base。"""
    raw = (raw or "").strip()
    raw = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/)", "", raw, flags=re.I)
    raw = re.sub(r"\.pdf$", "", raw, flags=re.I)
    raw = re.sub(r"^arxiv[:/]\s*", "", raw, flags=re.I)
    m = _ARXIV_VERSION_RE.match(raw)
    if not m:
        return raw, None
    return m.group("base"), m.group("ver")


def find_arxiv_id(text: str | None) -> str | None:
    """从任意文本（评论、URL）中抽取第一个 arXiv ID，返回无版本形式。"""
    if not text:
        return None
    m = _ARXIV_ID_RE.search(text)
    return m.group("id") if m else None


_STOPWORDS = frozenset(
    "a an the of for with on in to and or via using is are be at by from "
    "that this it its as into over under towards toward new".split()
)
_LATEX_RE = re.compile(r"\$[^$]*\$|\\[a-zA-Z]+\{?|[{}]")


def normalize_title(title: str | None) -> str:
    """小写、去 LaTeX、去标点、折叠空白。保留停用词，仅用于逐字比对。"""
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title)
    text = _LATEX_RE.sub(" ", text)
    text = text.lower()
    text = re.sub(r"[^\w\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(title: str | None) -> frozenset[str]:
    """去停用词后的 token 集合，用于 Jaccard 相似度。"""
    return frozenset(
        tok for tok in normalize_title(title).split()
        if tok and tok not in _STOPWORDS
    )


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(left: str | None, right: str | None) -> float:
    return jaccard(title_tokens(left), title_tokens(right))


_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#_/-]*")


def text_length(text: str | None) -> int:
    """解读字段的"字数"：中文字符数 + 英文词/数字各记 1。

    不直接用 `len`：解读要求术语保留英文，实验结果段里 "在 Harmon-1.5B 上
    GenEval 0.86" 这类句子中文字符很少（实测 results 段 203 字符里只有 44 个
    中文字），纯按中文字数会把信息量足够的段落判成过短，按 `len` 又会把
    英文长名当成大量正文。
    """
    if not text:
        return 0
    return len(_CJK_RE.findall(text)) + len(_ASCII_TOKEN_RE.findall(text))


def content_hash(*parts: str | None) -> str:
    """短哈希，用于缓存判定与历史去重摘要。"""
    digest = hashlib.sha256()
    for part in parts:
        digest.update((part or "").encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------- 存储


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        log.error("%s 解析失败，按缺省值处理: %s", path, exc)
        return default


def write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    """原子写入：先写临时文件再 rename，避免任务中断留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=indent,
                      sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# 分片路径：全部按年/月切分，避免单文件每日全量重写导致 git 膨胀

def daily_path(batch: date) -> Path:
    return DATA_DIR / "daily" / f"{batch:%Y}" / f"{batch:%m}" / f"{batch:%d}.json"


def raw_path(batch: date, source: str) -> Path:
    return DATA_DIR / "raw" / f"{batch:%Y-%m-%d}" / f"{source}.json"


def archive_index_path(batch: date) -> Path:
    return DATA_DIR / "archive" / f"index-{batch:%Y}.json"


def seen_path(batch: date) -> Path:
    return DATA_DIR / "state" / f"seen-{batch:%Y-%m}.json"


def seen_digest_path() -> Path:
    return DATA_DIR / "state" / "seen-digest.json"


def week_budget_path() -> Path:
    return DATA_DIR / "state" / "week-budget.json"


def stars_path(batch: date) -> Path:
    return DATA_DIR / "state" / "stars" / f"{batch:%Y-%m}.json"


def citations_path(year_month: str) -> Path:
    """引用信号缓存，按**论文所属年月**分片（如 2026-11）。

    按论文而不是按抓取时间分片，才能让一次查询只读到相关的几个文件；
    老论文的修订版会落到各自的历史分片，但那些分片之后几乎不再变动。
    """
    return DATA_DIR / "state" / "citations" / f"{year_month}.json"


def metrics_path(batch: date) -> Path:
    return DATA_DIR / "metrics" / f"{batch:%Y-%m}.json"


def recent_seen_months(batch: date, months: int = 4) -> list[Path]:
    """冷却期判断只需读最近几个月的分片。"""
    paths, cursor = [], batch
    for _ in range(months):
        paths.append(seen_path(cursor))
        cursor = (cursor.replace(day=1) - timedelta(days=1))
    return paths


_dotenv_cache: dict[str, str] | None = None

# .env 优先；.env.example 作为兜底，因为本地常有人直接把 key 填在模板里。
# 两者都在 .gitignore 中，不会被提交。
_DOTENV_FILES = (".env", ".env.example")


def _dotenv() -> dict[str, str]:
    """本地开发用的 .env（已在 .gitignore 中）。CI 里一律走环境变量。"""
    global _dotenv_cache
    if _dotenv_cache is not None:
        return _dotenv_cache
    pairs: dict[str, str] = {}
    # 倒序读：靠前的文件后写入，从而覆盖兜底文件里的同名值
    for name in reversed(_DOTENV_FILES):
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            # 模板里的占位符（如 sk-xxx、your-token）不算配置
            if value and not value.startswith(("sk-xxx", "your", "<")):
                pairs[key.strip()] = value
    _dotenv_cache = pairs
    return pairs


def env(name: str, default: str | None = None) -> str | None:
    """取密钥：环境变量优先，其次本地 .env，最后 default。"""
    value = os.environ.get(name) or _dotenv().get(name) or default
    return value.strip() if isinstance(value, str) else value


def env_int(name: str, default: int | None = None) -> int | None:
    """取整数型配置。非数字按未设置处理，不让一个笔误中断整条流水线。"""
    raw = env(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("环境变量 %s=%r 不是整数，按未设置处理", name, raw)
        return default


def env_flag(name: str, default: bool = False) -> bool:
    """取布尔型配置。

    GitHub Variables 只能存字符串，而人填的值五花八门（true/True/1/yes/on），
    所以在这里统一归一；填了别的词按 false，并留一条日志说明没生效。
    """
    raw = (env(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    log.warning("环境变量 %s=%r 不是布尔值，按 %s 处理", name, raw, default)
    return default





