"""Groq client built for the free tier.

The free tier is generous in quality but tight in volume (per-model limits on
requests and tokens, per minute and per day). This client is designed around that:

* Rate limiter: stays under each model's requests/minute and tokens/minute.
* Fallback chain: when a model's daily quota runs out, the next model takes over.
* Response cache: every response is stored on disk keyed by the exact request, so
  re-running an evaluation costs nothing. `replay` mode forbids network calls, which
  makes CI deterministic and free.
* Daily ledger: request counts per model and day, committed with the results, so runs
  on different days (or machines) respect the same budget.
* Retries honour Groq's `retry-after` header.

Limits below are the free-tier values at the time of writing and can change;
check https://console.groq.com/settings/limits and override with `limits=`.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://api.groq.com/openai/v1/chat/completions"


@dataclass(frozen=True)
class ModelLimits:
    rpm: int   # requests per minute
    tpm: int   # tokens per minute
    rpd: int   # requests per day


DEFAULT_LIMITS: dict[str, ModelLimits] = {
    "openai/gpt-oss-120b": ModelLimits(30, 8_000, 1_000),
    "openai/gpt-oss-20b": ModelLimits(30, 8_000, 1_000),
    "llama-3.3-70b-versatile": ModelLimits(30, 12_000, 1_000),
    "llama-3.1-8b-instant": ModelLimits(30, 6_000, 14_400),
    "qwen/qwen3-32b": ModelLimits(60, 6_000, 1_000),
    "meta-llama/llama-4-scout-17b-16e-instruct": ModelLimits(30, 30_000, 1_000),
}
UNKNOWN_MODEL_LIMITS = ModelLimits(20, 6_000, 500)
DEFAULT_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "meta-llama/llama-4-scout-17b-16e-instruct"]


class LLMError(RuntimeError):
    pass


class CacheMiss(LLMError):
    """Replay mode was asked for a response that was never recorded."""


class BudgetExhausted(LLMError):
    """Every model in the chain is out of quota (or the run's request budget is used)."""


class _DailyLimit(Exception):
    pass


class _Unavailable(Exception):
    """The account cannot use this model (retired, or not enabled for the key)."""


@dataclass
class Usage:
    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class RateLimiter:
    """Sliding 60-second window over requests and tokens."""

    def __init__(self, rpm: int, tpm: int, clock=time.monotonic, sleep=time.sleep, margin: float = 0.9):
        self.rpm = max(1, int(rpm * margin))
        self.tpm = max(1, int(tpm * margin))
        self.clock, self.sleep = clock, sleep
        self.events: deque[list] = deque()  # [timestamp, tokens]

    def acquire(self, tokens: int) -> list:
        tokens = min(tokens, self.tpm)  # a single oversized request can never fit; let the server decide
        while True:
            now = self.clock()
            while self.events and now - self.events[0][0] >= 60:
                self.events.popleft()
            used = sum(e[1] for e in self.events)
            if len(self.events) < self.rpm and used + tokens <= self.tpm:
                entry = [now, tokens]
                self.events.append(entry)
                return entry
            wait = self.events[0][0] + 60 - now + 0.05 if self.events else 0.05
            self.sleep(max(wait, 0.05))

    @staticmethod
    def settle(entry: list, actual_tokens: int) -> None:
        entry[1] = actual_tokens


class Ledger:
    """Per-day request counts and 'exhausted' flags per model, kept in a JSON file."""

    def __init__(self, path: Path | None, today=None):
        self.path = Path(path) if path else None
        self._today = today or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        self.data: dict = {}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text())

    def _day(self) -> dict:
        d = self.data.setdefault(self._today(), {})
        return d

    def used(self, model: str) -> int:
        return self._day().get(model, {}).get("requests", 0)

    def exhausted(self, model: str) -> bool:
        return bool(self._day().get(model, {}).get("exhausted"))

    def add(self, model: str, n: int = 1) -> None:
        m = self._day().setdefault(model, {})
        m["requests"] = m.get("requests", 0) + n
        self.save()

    def mark_exhausted(self, model: str) -> None:
        self._day().setdefault(model, {})["exhausted"] = True
        self.save()

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))


def _default_post(url, headers, body, timeout):
    return requests.post(url, headers=headers, json=body, timeout=timeout)


def estimate_tokens(messages: list, tools: list | None, max_tokens: int) -> int:
    chars = len(json.dumps(messages)) + (len(json.dumps(tools)) if tools else 0)
    return int(chars / 3.5) + max_tokens


class LLM:
    def __init__(
        self,
        api_key: str | None,
        models: list[str] | None = None,
        cache_dir: Path | None = None,
        mode: str = "live",                  # "live": cache then network; "replay": cache only
        limits: dict[str, ModelLimits] | None = None,
        ledger_path: Path | None = None,
        max_requests: int | None = None,     # network requests allowed in this run
        daily_safety: float = 0.95,          # stop at 95% of the daily quota
        post=None, clock=time.monotonic, sleep=time.sleep, today=None,
    ):
        if mode not in ("live", "replay"):
            raise ValueError("mode must be 'live' or 'replay'")
        if mode == "live" and not api_key:
            raise LLMError("GROQ_API_KEY is not set")
        self.api_key = api_key
        self.models = models or list(DEFAULT_MODELS)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.mode = mode
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.ledger = Ledger(ledger_path, today=today)
        self.max_requests = max_requests
        self.daily_safety = daily_safety
        self.post = post or _default_post
        self.clock, self.sleep = clock, sleep
        self.usage = Usage()
        self._limiters: dict[str, RateLimiter] = {}
        self._unavailable: set[str] = set()

    # ---- helpers -------------------------------------------------------
    def _limits(self, model: str) -> ModelLimits:
        return self.limits.get(model, UNKNOWN_MODEL_LIMITS)

    def _limiter(self, model: str) -> RateLimiter:
        if model not in self._limiters:
            lim = self._limits(model)
            self._limiters[model] = RateLimiter(lim.rpm, lim.tpm, self.clock, self.sleep)
        return self._limiters[model]

    @staticmethod
    def _request(model, messages, tools, tool_choice, max_tokens, temperature) -> dict:
        req = {"model": model, "messages": messages, "temperature": temperature, "max_completion_tokens": max_tokens}
        if tools:
            req["tools"] = tools
            req["tool_choice"] = tool_choice or "auto"
        if model.startswith("openai/gpt-oss"):
            req["reasoning_effort"] = "low"  # keeps token use (and the TPM budget) small
        return req

    def _cache_file(self, req: dict) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.json"

    def _read_cache(self, req: dict):
        f = self._cache_file(req)
        if f and f.exists():
            return json.loads(f.read_text())
        return None

    def _write_cache(self, req: dict, out: dict) -> None:
        f = self._cache_file(req)
        if f:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(out, sort_keys=True))

    # ---- public API ----------------------------------------------------
    def chat(self, messages: list, tools: list | None = None, tool_choice=None, max_tokens: int = 1024, temperature: float = 0.0) -> dict:
        """Returns {'message': {...}, 'usage': {...}, 'model': str, 'cached': bool}."""
        requests_by_model = {m: self._request(m, messages, tools, tool_choice, max_tokens, temperature) for m in self.models}
        for model in self.models:
            hit = self._read_cache(requests_by_model[model])
            if hit:
                self.usage.cache_hits += 1
                return {**hit, "cached": True}
        if self.mode == "replay":
            raise CacheMiss("no recorded response for this request (prompts or tools changed since the last recording)")

        for model in self.models:
            lim = self._limits(model)
            if model in self._unavailable or self.ledger.exhausted(model) or self.ledger.used(model) >= lim.rpd * self.daily_safety:
                continue
            if self.max_requests is not None and self.usage.requests >= self.max_requests:
                raise BudgetExhausted("request budget for this run is used up")
            try:
                out = self._call(model, requests_by_model[model], messages, tools)
            except _DailyLimit:
                self.ledger.mark_exhausted(model)
                continue
            except _Unavailable:
                self._unavailable.add(model)
                continue
            self._write_cache(requests_by_model[model], out)
            return {**out, "cached": False}
        raise BudgetExhausted("every model in the chain is out of quota for today")

    # ---- network -------------------------------------------------------
    def _call(self, model: str, req: dict, messages: list, tools: list | None) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        limiter = self._limiter(model)
        body = dict(req)
        for attempt in range(7):
            entry = limiter.acquire(estimate_tokens(messages, tools, req["max_completion_tokens"]))
            resp = self.post(API_URL, headers, body, 120)
            status = resp.status_code
            if status == 200:
                data = resp.json()
                usage = data.get("usage") or {}
                limiter.settle(entry, int(usage.get("total_tokens") or entry[1]))
                self.ledger.add(model)
                self.usage.requests += 1
                self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
                msg = data["choices"][0]["message"]
                keep = {k: msg[k] for k in ("role", "content", "tool_calls") if msg.get(k) is not None}
                keep.setdefault("role", "assistant")
                return {"message": keep, "model": model,
                        "usage": {"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0)}}
            text = _error_text(resp)
            if status == 429:
                if _is_daily_limit(text, resp):
                    raise _DailyLimit(text)
                self.usage.retries += 1
                self.sleep(min(_retry_after(resp) or 2 ** attempt, 65))
                continue
            if status in (403, 404) and re.search(r"model_not_found|does not exist|model_permission|decommission", text, re.I):
                raise _Unavailable(text)
            if status == 400 and "reasoning_effort" in text and "reasoning_effort" in body:
                body.pop("reasoning_effort")
                continue
            if status in (500, 502, 503, 504):
                self.usage.retries += 1
                self.sleep(min(2 ** attempt, 30))
                continue
            raise LLMError(f"Groq returned {status}: {text[:300]}")
        raise LLMError(f"giving up on {model} after repeated failures")


def _error_text(resp) -> str:
    try:
        return json.dumps(resp.json())
    except Exception:
        return getattr(resp, "text", "") or ""


def _retry_after(resp) -> float | None:
    v = (getattr(resp, "headers", None) or {}).get("retry-after")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


_DAILY = re.compile(r"per day|\(RPD\)|\(TPD\)|requests per day|tokens per day", re.I)


def _is_daily_limit(text: str, resp) -> bool:
    if _DAILY.search(text):
        return True
    ra = _retry_after(resp)
    return ra is not None and ra > 120  # a wait of minutes means a daily, not per-minute, limit
