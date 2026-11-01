"""Prompt token budgets: trim a built context, deterministically, until its prompt fits the worker's budget.

    WorkerModelConfig.prompt_token_budget (draft → apply, app.services.ai_model_config)
    → budget_for(db, user_id, "RANKER")        the account's active configuration (model_resolution.get)
    → fit(render, steps, budget)               render, measure, drop one item, render again …
    → the prompt that fits, and a Trim record  (what was removed, tokens before and after)

The budget exists for providers that refuse a request larger than a per-minute token allowance (Groq's TPM / ITPM):
the context window alone only refuses such a request (LLMContextOverflowError), it never shrinks it.

Rules:
* No budget, no change. With the budget unset `fit` renders once and returns that prompt: byte-identical to the
  builders without this module.
* Deterministic. Each step removes one item chosen by a fixed rule (lowest relevance score, ties broken by
  position or id), and the steps run in the documented order of their worker (context_builder.RANKING_TRIM_ORDER,
  TAILORING_TRIM_ORDER, ai_advisor.ADVISOR_TRIM_ORDER). No model is called.
* Never drop what a decision rests on: REQUIRED requirements, the employer's sentences for them, the candidate
  identity, the fit analysis, rule findings. A context the steps cannot bring under budget is sent as trimmed as it
  can be, and the gateway's context-window check still refuses it if it is too large for the model.
* Measured the way the gateway measures (llm_telemetry.estimate_tokens over the system and user messages).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.services.llm_telemetry import estimate_tokens

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("jobhunt.prompt_budget")

# The workers whose context builders trim to a budget. The budget of any other worker has no effect (a warning in
# validation says so).
TRIMMED_WORKERS = ("RANKER", "TAILOR", "AI_OPTIMIZATION")
# Re-renders one fit may make: far more than any context has items, so it only guards against a step that lies.
MAX_STEPS = 2000


@dataclass
class Step:
    """One kind of item a context may lose. `drop` removes the least relevant remaining item and says whether it
    removed anything; `floor` items are always kept (enforced by `drop` itself)."""

    kind: str
    drop: Callable[[], bool]


@dataclass
class Trim:
    budget: int | None = None
    tokens_before: int = 0
    tokens_after: int = 0
    removed: dict[str, int] = field(default_factory=dict)

    @property
    def trimmed(self) -> bool:
        return bool(self.removed)

    @property
    def fits(self) -> bool:
        return self.budget is None or self.tokens_after <= self.budget


def prompt_tokens(prompt: Any) -> int:
    """The estimate the gateway's overflow check makes (app.services.llm._litellm_request)."""
    return estimate_tokens(prompt.system) + estimate_tokens(prompt.user)


def budget_for(db: Session, user_id: int | None, worker_type: str) -> int | None:
    """The worker's prompt token budget in the account's active configuration. None when the account does not send
    through the LiteLLM gateway (worker configuration applies only there, like its context window), when no budget
    is set, or when the configuration cannot be read: that never blocks a request, the prompt is sent untrimmed."""
    from app.services import model_resolution
    from app.services.ai_config import resolve as resolve_ai_config
    from app.services.user_settings import ai_overrides
    from app.settings import get_settings

    if user_id is None:
        return None
    try:
        if resolve_ai_config(get_settings(), ai_overrides(db, user_id))["llm_provider"] != "litellm":
            return None
        budget = model_resolution.get(user_id, worker_type).prompt_token_budget
    except Exception:  # fails open, like the rest of resolution's telemetry
        log.warning("prompt token budget of %s could not be read", worker_type, exc_info=True)
        return None
    return budget if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0 else None


def fit(render: Callable[[], Any], steps: list[Step], budget: int | None) -> tuple[Any, Trim]:
    """Render; while the prompt is over `budget`, take the first step that still removes something and render
    again. Returns the last prompt rendered and what was removed."""
    prompt = render()
    size = prompt_tokens(prompt)
    trim = Trim(budget=budget, tokens_before=size, tokens_after=size)
    if not budget:
        return prompt, trim
    rounds = 0
    for step in steps:
        while size > budget and rounds < MAX_STEPS and step.drop():
            rounds += 1
            trim.removed[step.kind] = trim.removed.get(step.kind, 0) + 1
            prompt = render()
            size = prompt_tokens(prompt)
        if size <= budget:
            break
    trim.tokens_after = size
    return prompt, trim


# ───────────── step helpers ─────────────

def drop_last(items: list[Any], floor: int = 0) -> Callable[[], bool]:
    """Remove the last item while more than `floor` remain: for lists already ordered most relevant first."""
    def drop() -> bool:
        if len(items) <= floor:
            return False
        items.pop()
        return True
    return drop


def drop_lowest(items: list[Any], score: Callable[[Any], tuple], floor: int = 0,
                removed: Callable[[Any], None] | None = None) -> Callable[[], bool]:
    """Remove the item with the lowest `score` (a tuple; the smallest goes first) while more than `floor` remain.
    `removed` is told about the item, so a step can keep what depends on it consistent."""
    def drop() -> bool:
        if len(items) <= floor:
            return False
        index = min(range(len(items)), key=lambda i: score(items[i]) + (-i,))
        item = items.pop(index)
        if removed is not None:
            removed(item)
        return True
    return drop
