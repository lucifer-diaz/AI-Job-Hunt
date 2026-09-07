from __future__ import annotations

import json
import re
from typing import Any

from app.settings import get_settings


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
    return "default"


def _csv_models(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_free_openrouter_model(model: str) -> bool:
    # The free router itself and explicit :free variants have zero token price.
    clean = model.strip()
    return clean == "openrouter/free" or ":free" in clean.split("@", 1)[0]


class LLMClient:
    def __init__(self):
        self.settings = get_settings()

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        provider = self.settings.llm_provider.lower()
        if provider == "mock":
            return self._mock(user)
        if provider == "openai":
            return self._openai(system, user)
        if provider == "anthropic":
            return self._anthropic(system, user)
        if provider == "openrouter":
            return self._openrouter(system, user, task=_task_from_prompt(user))
        raise LLMError(f"Unsupported LLM_PROVIDER={provider}")

    def _openai(self, system: str, user: str) -> dict[str, Any]:
        if not self.settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY is missing")
        from openai import OpenAI

        client = OpenAI(api_key=self.settings.openai_api_key)
        response = client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        return _extract_json(response.choices[0].message.content or "{}")

    def _anthropic(self, system: str, user: str) -> dict[str, Any]:
        if not self.settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is missing")
        from anthropic import Anthropic

        client = Anthropic(api_key=self.settings.anthropic_api_key)
        msg = client.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=5000,
            system=system + "\nReturn only valid JSON. No markdown fences.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(block, "text", "") for block in msg.content)
        return _extract_json(text)

    def _openrouter_model_for_task(self, task: str) -> str:
        overrides = {
            "rank": self.settings.openrouter_model_rank,
            "tailor": self.settings.openrouter_model_tailor,
            "review": self.settings.openrouter_model_review,
            "followup": self.settings.openrouter_model_followup,
        }
        return overrides.get(task) or self.settings.openrouter_model

    def _openrouter(self, system: str, user: str, task: str = "default") -> dict[str, Any]:
        if not self.settings.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is missing")

        primary = self._openrouter_model_for_task(task)
        fallbacks = _csv_models(self.settings.openrouter_fallback_models)
        all_models = [primary, *fallbacks]

        # Avoid accidental spend when the user intends to run on free inference.
        if self.settings.openrouter_require_free:
            paid = [model for model in all_models if not _is_free_openrouter_model(model)]
            if paid:
                raise LLMError(
                    "OPENROUTER_REQUIRE_FREE=true but paid/non-free model(s) were configured: "
                    + ", ".join(paid)
                    + ". Use openrouter/free or a :free model, or explicitly set OPENROUTER_REQUIRE_FREE=false."
                )

        from openai import OpenAI

        headers: dict[str, str] = {"X-Title": self.settings.openrouter_app_name}
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url

        client = OpenAI(
            api_key=self.settings.openrouter_api_key,
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
        return {
            "summary": "Tailored engineering leader with production AI and backend experience.",
            "skills": ["Python", "FastAPI", "AWS", "Kubernetes"],
            "experience_bullets": ["Built and operated production backend and AI systems."],
            "selected_evidence_ids": [],
            "cover_letter": "I am excited to apply because the role aligns with my production engineering and AI experience.",
        }
