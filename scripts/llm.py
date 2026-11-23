"""DeepSeek 客户端：JSON 调用、重试、token 计量与预算闸门。

密钥优先取环境变量 `DEEPSEEK_API_KEY`（CI 走 Actions Secrets），本地回落到
仓库根的 `llm_api_key.txt`（格式 `deepseek:sk-...`，已在 .gitignore 中）。

两个模型的能力已对 `/models` 核实：`deepseek-flash`（V4.1-Flash）与
`deepseek-v4-pro` 均为 1M 上下文、393K 最大输出，effort 支持 low/high/max。

调用约定：固定的 system prompt 与模板放在最前，可变的论文正文放最后，
让 DeepSeek 的自动 prefix cache 命中率最大化（计入 `cache_hit_tokens`）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

from .common import ROOT, env, load_config, log

_BASE_URL = "https://api.deepseek.com"
_KEY_FILE = ROOT / "llm_api_key.txt"
_RETRIABLE = (RateLimitError, APITimeoutError, APIConnectionError)


class LLMError(RuntimeError):
    """调用失败或输出不可解析。调用方决定降级还是中止。"""


class BudgetExceeded(LLMError):
    """本次运行的 token 预算已用尽，应保留已完成部分后停止。"""


def api_key() -> str:
    value = env("DEEPSEEK_API_KEY")
    if value:
        return value
    if _KEY_FILE.exists():
        # 文件格式 `provider:key`，key 自身含 `-` 但不含 `:`
        _, _, token = _KEY_FILE.read_text(encoding="utf-8").strip().partition(":")
        if token.strip():
            return token.strip()
    raise LLMError("缺少 DEEPSEEK_API_KEY：设环境变量或写入 llm_api_key.txt")


# ------------------------------------------------------------ token 账本


@dataclass
class Usage:
    """单个模型的累计用量。"""

    calls: int = 0
    failures: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"calls": self.calls, "failures": self.failures,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "cache_hit_tokens": self.cache_hit_tokens}


@dataclass
class Ledger:
    """跨模型汇总用量，并在超出单次运行预算时拦住后续调用。

    预算是硬闸门而非告警：L2 全文调用单条可达 30K token，一次失控的循环
    就能把余额打光，而余额耗尽的报错出现在哪一步是不可预测的。
    """

    limit_in: int | None = None
    limit_out: int | None = None
    models: dict[str, Usage] = field(default_factory=dict)

    def check(self) -> None:
        if self.limit_in is not None and self.total_in >= self.limit_in:
            raise BudgetExceeded(f"输入 token 超预算：{self.total_in}/{self.limit_in}")
        if self.limit_out is not None and self.total_out >= self.limit_out:
            raise BudgetExceeded(f"输出 token 超预算：{self.total_out}/{self.limit_out}")

    @property
    def total_in(self) -> int:
        return sum(u.tokens_in for u in self.models.values())

    @property
    def total_out(self) -> int:
        return sum(u.tokens_out for u in self.models.values())

    def usage_for(self, model: str) -> Usage:
        return self.models.setdefault(model, Usage())

    def report(self) -> dict[str, Any]:
        return {model: usage.as_dict() for model, usage in self.models.items()}


_shared_ledger: Ledger | None = None


def default_ledger() -> Ledger:
    """整次运行共用一个账本，预算取 scoring.yaml 的 summarize.budget。"""
    global _shared_ledger
    if _shared_ledger is None:
        budget = (load_config("scoring").get("summarize") or {}).get("budget") or {}
        _shared_ledger = Ledger(limit_in=budget.get("tokens_in_per_run"),
                                limit_out=budget.get("tokens_out_per_run"))
    return _shared_ledger


# ------------------------------------------------------------ 客户端


_FENCE_RE = re.compile(r"\A```(?:json)?\s*|\s*```\Z")


def _loads(text: str) -> dict[str, Any]:
    """解析模型输出。即便要求了 json_object，仍可能裹代码块标记。"""
    data = json.loads(_FENCE_RE.sub("", text.strip()))
    if not isinstance(data, dict):
        raise ValueError(f"顶层不是 JSON 对象，而是 {type(data).__name__}")
    return data


class LLM:
    """一个模型 + 一组调用参数。限流退避与 token 计量都在这里统一处理。"""

    def __init__(
        self,
        model: str,
        effort: str = "low",
        *,
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 600.0,
        max_retries: int = 3,
        ledger: Ledger | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.ledger = ledger if ledger is not None else default_ledger()
        self.last_call: dict[str, int | str] = {}
        # max_retries=0：退避策略由本类控制，避免与 SDK 的重试叠加成两层
        self._client = OpenAI(api_key=api_key(), base_url=_BASE_URL,
                              timeout=timeout, max_retries=0)

    @classmethod
    def from_config(cls, section: str, **kwargs: Any) -> LLM:
        """按 scoring.yaml 的 `rank` / `summarize` 段构造，显式入参优先。"""
        cfg = load_config("scoring")[section]
        kwargs.setdefault("max_output_tokens",
                          int(cfg.get("max_output_tokens") or 4096))
        kwargs.setdefault("effort", cfg.get("effort", "low"))
        kwargs.setdefault("temperature", float(cfg.get("temperature") or 0.0))
        return cls(cfg["model"], **kwargs)

    def chat_json(self, system: str, user: str, *, label: str = "") -> dict[str, Any]:
        """返回解析后的 JSON 对象。非法 JSON 带着报错原文让模型自己改。"""
        self.ledger.check()
        tag = f"[{label}] " if label else ""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                text = self._complete(messages)
            except _RETRIABLE as exc:
                last = exc
                delay = 5 * 2 ** attempt
                log.warning("%sLLM %s 限流/超时 (%d/%d)，等待 %ds: %s",
                            tag, self.model, attempt + 1, self.max_retries,
                            delay, exc)
                time.sleep(delay)
                continue
            except APIStatusError as exc:
                if exc.status_code < 500:
                    self.ledger.usage_for(self.model).failures += 1
                    raise LLMError(f"{tag}{self.model} 返回 {exc.status_code}: "
                                   f"{str(exc)[:200]}") from exc
                last = exc
                time.sleep(5 * 2 ** attempt)
                continue
            try:
                return _loads(text)
            except ValueError as exc:
                last = exc
                log.warning("%sLLM %s 输出不是合法 JSON (%d/%d): %s",
                            tag, self.model, attempt + 1, self.max_retries, exc)
                messages = messages[:2] + [
                    {"role": "assistant", "content": text[:2000]},
                    {"role": "user", "content":
                        f"上一条回复不是合法 JSON（{exc}）。只输出一个 JSON "
                        "对象，不要代码块标记，不要任何解释文字。"},
                ]
        self.ledger.usage_for(self.model).failures += 1
        raise LLMError(f"{tag}{self.model} 重试耗尽: {last}")

    def _complete(self, messages: list[dict[str, str]]) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=self.max_output_tokens,
            temperature=self.temperature,
            response_format={"type": "json_object"},
            extra_body={"reasoning_effort": self.effort},
        )
        self._record(resp)
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            # 截断的 JSON 修不回来，且重试同样会被截断，直接失败
            raise LLMError(f"{self.model} 输出达到 max_tokens="
                           f"{self.max_output_tokens}，JSON 不完整")
        return choice.message.content or ""

    def _record(self, resp: Any) -> None:
        usage = self.ledger.usage_for(self.model)
        usage.calls += 1
        raw = getattr(resp, "usage", None)
        if raw is None:
            return
        tokens_in = int(getattr(raw, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(raw, "completion_tokens", 0) or 0)
        # prompt_cache_hit_tokens 是 DeepSeek 的扩展字段，不在 SDK 的类型里
        extra = getattr(raw, "model_extra", None) or {}
        cache_hit = int(extra.get("prompt_cache_hit_tokens") or 0)
        usage.tokens_in += tokens_in
        usage.tokens_out += tokens_out
        usage.cache_hit_tokens += cache_hit
        # 本次调用的单独用量：DESIGN 8.6 要求每个条目落 cost 字段，
        # 账本只有按模型的累计值，摊不回条目
        self.last_call = {"model_used": self.model, "tokens_in": tokens_in,
                          "tokens_out": tokens_out, "cache_hit_tokens": cache_hit}




