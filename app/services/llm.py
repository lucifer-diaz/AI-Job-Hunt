from __future__ import annotations

import json
import re
from typing import Any

from typing import TYPE_CHECKING

from app.services.ai_config import is_free_model, resolve as resolve_ai_config
from app.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise LLMError("Model did not return JSON")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError("Model returned malformed JSON") from exc


def _task_from_prompt(user: str) -> str:
    if "FIT_ANALYSIS" in user:
        return "rank"
    if "TAILOR_RESUME" in user:
        return "tailor"
    if "REVIEW_RESUME" in user:
        return "review"
    if "FOLLOW_UP" in user:
        return "followup"
    if "EXTRACT_EVIDENCE" in user:
        return "tailor"
    return "default"


def _csv_models(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_free_openrouter_model(model: str) -> bool:
    # The free router itself and explicit :free variants have zero token price.
    return is_free_model("openrouter", model)


class LLMClient:
    """Calls the AI provider chosen in a user's settings, with that user's own API key."""

    def __init__(self, overrides: dict[str, Any] | None = None, keys: dict[str, str | None] | None = None):
        self.settings = get_settings()
        self.overrides = overrides or {}
        self.keys = keys or {}

    @classmethod
    def for_user(cls, db: Session, user_id: int | None = None) -> LLMClient:
        """A client for the user the session belongs to (or an explicit user)."""
        from app.services.user_settings import ai_overrides, user_keys

        return cls(overrides=ai_overrides(db, user_id), keys=user_keys(db, user_id))

    def _config(self) -> dict[str, Any]:
        return resolve_ai_config(self.settings, self.overrides)

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        config = self._config()
        provider = config["llm_provider"]
        task = _task_from_prompt(user)
        model = config["stage_models"].get(task) or config["model"]
        if provider == "mock":
            return self._mock(user)
        if provider == "openai":
            return self._openai(system, user, model)
        if provider == "anthropic":
            return self._anthropic(system, user, model)
        if provider == "openrouter":
            return self._openrouter(system, user, task=task, model=model, require_free=config["require_free"])
        raise LLMError(f"Unsupported LLM_PROVIDER={provider}")

    def _openai(self, system: str, user: str, model: str) -> dict[str, Any]:
        if not self.keys.get("openai"):
            raise LLMError("OPENAI_API_KEY is missing: add your OpenAI API key in Settings")
        from openai import OpenAI

        client = OpenAI(api_key=self.keys["openai"])
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        return _extract_json(response.choices[0].message.content or "{}")

    def _anthropic(self, system: str, user: str, model: str) -> dict[str, Any]:
        if not self.keys.get("anthropic"):
            raise LLMError("ANTHROPIC_API_KEY is missing: add your Anthropic API key in Settings")
        from anthropic import Anthropic

        client = Anthropic(api_key=self.keys["anthropic"])
        msg = client.messages.create(
            model=model,
            max_tokens=5000,
            system=system + "\nReturn only valid JSON. No markdown fences.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(block, "text", "") for block in msg.content)
        return _extract_json(text)

    def _openrouter(self, system: str, user: str, task: str, model: str, require_free: bool) -> dict[str, Any]:
        if not self.keys.get("openrouter"):
            raise LLMError("OPENROUTER_API_KEY is missing: add your OpenRouter API key in Settings")

        primary = model
        fallbacks = _csv_models(self.settings.openrouter_fallback_models)
        all_models = [primary, *fallbacks]

        # Avoid accidental spend when the user intends to run on free inference.
        if require_free:
            paid = [name for name in all_models if not _is_free_openrouter_model(name)]
            if paid:
                raise LLMError(
                    "OPENROUTER_REQUIRE_FREE=true but paid/non-free model(s) were configured: "
                    + ", ".join(paid)
                    + ". Use openrouter/free or a :free model, or turn off the free-only guard in Settings."
                )

        from openai import OpenAI

        headers: dict[str, str] = {"X-Title": self.settings.openrouter_app_name}
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url

        client = OpenAI(
            api_key=self.keys["openrouter"],
            base_url=self.settings.openrouter_base_url,
            default_headers=headers,
        )

        extra_body: dict[str, Any] = {}
        if fallbacks:
            # OpenRouter extension: model-level fallbacks. Provider-level
            # failover remains enabled by default as well.
            extra_body["models"] = fallbacks

        request_kwargs: dict[str, Any] = {
            "model": primary,
            "messages": [
                {"role": "system", "content": system + "\nReturn only valid JSON. No markdown fences."},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.openrouter_max_tokens,
        }
        if extra_body:
            request_kwargs["extra_body"] = extra_body

        try:
            response = client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            raise LLMError(f"OpenRouter request failed for task={task}, model={primary}: {exc}") from exc

        content = response.choices[0].message.content or "{}"
        return _extract_json(content)

    def _mock(self, user: str) -> dict[str, Any]:
        # Useful for local smoke tests without API spend.
        if "FIT_ANALYSIS" in user:
            return {
                "score": 80,
                "summary": "Good overall fit based on the supplied profile.",
                "strong_matches": ["Python", "backend systems"],
                "gaps": [],
                "must_have_missing": [],
                "keywords": ["Python", "FastAPI"],
                "application_strategy": "Emphasize production ownership and measurable outcomes.",
                "confidence": 0.8,
            }
        if "REVIEW_RESUME" in user:
            return {
                "score": 86,
                "reject_reasons": [],
                "strengths": ["Relevant evidence", "Clear outcomes"],
                "improvements": ["Keep bullets concise"],
            }
        if "FOLLOW_UP" in user:
            return {"draft": "Hi, I wanted to follow up on my application and reiterate my interest in the role. Best regards."}
        if "EXTRACT_EVIDENCE" in user:
            lines = [line.strip(" -•*\t") for line in user.split("MASTER RESUME:", 1)[-1].splitlines()]
            claims = [line for line in lines if len(line) > 25 and re.search(r"\d", line)][:12]
            return {"claims": [{"claim": c, "company": "", "role": "", "skills": []} for c in claims]}
        return {
            "summary": "Tailored engineering leader with production AI and backend experience.",
            "skills": ["Python", "FastAPI", "AWS", "Kubernetes"],
            "experience": [{"role_index": 0, "bullets": ["Built and operated production backend and AI systems."]}],
            "experience_bullets": ["Built and operated production backend and AI systems."],
            "selected_evidence_ids": [],
            "cover_letter": "I am excited to apply because the role aligns with my production engineering and AI experience.",
        }
