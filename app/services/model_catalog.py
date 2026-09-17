"""Model prices and context windows, read from one configuration file (app/model_catalog.json or MODEL_CATALOG_FILE).

This is the only place LLM costs are computed. Costs are estimates:
* free models (openrouter/free, any ':free' model, mock) cost 0;
* a model without a catalog entry, or an entry without the prices a request needs, has unknown cost (None);
* the entry used is the latest one whose effective_from is on or before the request time;
* a provider-reported cost (OpenRouter usage accounting) is preferred over the catalog when present.
"""
from __future__ import annotations

import fnmatch
import json
import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.ai_config import is_free_model
from app.settings import get_settings

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "model_catalog.json"
MILLION = Decimal(1_000_000)
_lock = threading.Lock()


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    model: str  # exact name or a glob such as "claude-haiku-4-5*"
    effective_from: date
    input_cost_per_million: Decimal | None = None
    output_cost_per_million: Decimal | None = None
    cached_input_cost_per_million: Decimal | None = None
    cache_write_cost_per_million: Decimal | None = None
    long_context_threshold_tokens: int | None = None
    long_context_input_cost_per_million: Decimal | None = None
    long_context_output_cost_per_million: Decimal | None = None
    long_context_cached_input_cost_per_million: Decimal | None = None
    long_context_cache_write_cost_per_million: Decimal | None = None
    context_window: int | None = None
    source_url: str | None = None

    def matches(self, provider: str, model: str) -> bool:
        return self.provider == provider and fnmatch.fnmatchcase(model, self.model)


@dataclass(frozen=True)
class CostEstimate:
    cost: Decimal | None
    source: str  # free | pricing_table | provider_reported | unknown
    effective_from: date | None = None


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _entry(raw: dict[str, Any]) -> ModelEntry:
    money = {key: _decimal(raw.get(key)) for key in (
        "input_cost_per_million", "output_cost_per_million", "cached_input_cost_per_million",
        "cache_write_cost_per_million", "long_context_input_cost_per_million", "long_context_output_cost_per_million",
        "long_context_cached_input_cost_per_million", "long_context_cache_write_cost_per_million")}
    return ModelEntry(provider=str(raw["provider"]), model=str(raw["model"]),
                      effective_from=date.fromisoformat(str(raw["effective_from"])),
                      long_context_threshold_tokens=raw.get("long_context_threshold_tokens"),
                      context_window=raw.get("context_window"), source_url=raw.get("source_url"), **money)


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[ModelEntry, ...]:
    data = json.loads(Path(path).read_text())
    return tuple(_entry(item) for item in data.get("models", []))


def catalog() -> tuple[ModelEntry, ...]:
    path = get_settings().model_catalog_file or str(DEFAULT_CATALOG)
    with _lock:
        return _load(path)


def reload() -> None:
    """Forget the cached catalog, e.g. after editing the file."""
    _load.cache_clear()


def lookup(provider: str | None, model: str | None, at: datetime | None = None) -> ModelEntry | None:
    """The newest entry for this provider and model that is in effect at `at`."""
    if not provider or not model:
        return None
    day = (at or datetime.now()).date()
    candidates = [e for e in catalog() if e.matches(provider, model) and e.effective_from <= day]
    return max(candidates, key=lambda e: e.effective_from, default=None)


def context_window(provider: str | None, model: str | None, at: datetime | None = None) -> int | None:
    entry = lookup(provider, model, at)
    return entry.context_window if entry else None


def estimate_cost(provider: str | None, model: str | None, *, input_tokens: int | None, output_tokens: int | None,
                  cache_read_tokens: int | None = None, cache_write_tokens: int | None = None,
                  provider_reported_cost: float | Decimal | None = None, at: datetime | None = None) -> CostEstimate:
    """Estimated USD cost of one request. Never guesses: missing tokens or prices give cost None."""
    if provider and model and is_free_model(provider, model):
        return CostEstimate(Decimal(0), "free")
    if provider_reported_cost is not None:
        return CostEstimate(Decimal(str(provider_reported_cost)), "provider_reported")
    entry = lookup(provider, model, at)
    if entry is None or input_tokens is None or output_tokens is None:
        return CostEstimate(None, "unknown", entry.effective_from if entry else None)
    long = bool(entry.long_context_threshold_tokens and input_tokens > entry.long_context_threshold_tokens)
    pick = (lambda short, long_rate: long_rate if long else short)
    input_rate = pick(entry.input_cost_per_million, entry.long_context_input_cost_per_million)
    output_rate = pick(entry.output_cost_per_million, entry.long_context_output_cost_per_million)
    cached_rate = pick(entry.cached_input_cost_per_million, entry.long_context_cached_input_cost_per_million)
    write_rate = pick(entry.cache_write_cost_per_million, entry.long_context_cache_write_cost_per_million)
    cache_read, cache_write = cache_read_tokens or 0, cache_write_tokens or 0
    if input_rate is None or output_rate is None or (cache_read and cached_rate is None) \
            or (cache_write and write_rate is None):
        return CostEstimate(None, "unknown", entry.effective_from)
    # Providers report cached and cache-write tokens inside or beside input tokens; callers pass uncached input.
    total = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
             + Decimal(cache_read) * (cached_rate or 0) + Decimal(cache_write) * (write_rate or 0)) / MILLION
    return CostEstimate(total.quantize(Decimal("0.00000001")), "pricing_table", entry.effective_from)
