"""Effective AI configuration: values saved in Settings win, `.env` fills every gap.

Overrides live in each user's `AppSetting.preferences["ai"]` (app.services.user_settings.ai_overrides). A blank or missing value means
"not set in the app" and falls back to the matching environment variable.
"""

from __future__ import annotations

from typing import Any

PROVIDERS = ("openrouter", "openai", "anthropic", "gemini", "mock")
STAGES = (("rank", "Rank"), ("tailor", "Tailor"), ("review", "Review"), ("followup", "Follow-up"))


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def is_free_model(provider: str, model: str) -> bool:
    if provider == "mock":
        return True
    clean = model.strip()
    return clean == "openrouter/free" or ":free" in clean.split("@", 1)[0]


def env_defaults(settings: Any) -> dict[str, Any]:
    """What the app would use with nothing saved in Settings."""
    provider = (settings.llm_provider or "openrouter").lower()
    stages = {stage: _clean(getattr(settings, f"openrouter_model_{stage}", None)) for stage, _ in STAGES}
    return {
        "llm_provider": provider,
        "models": {name: _clean(getattr(settings, f"{name}_model", "")) for name in PROVIDERS if name != "mock"},
        # OPENROUTER_MODEL_<STAGE> overrides only apply to OpenRouter.
        "stage_models": stages,
        # Provider-independent stage overrides (RANK_MODEL), used with any provider.
        "any_provider_stage_models": {"rank": _clean(getattr(settings, "rank_model", None))},
        "require_free": bool(settings.openrouter_require_free),
    }


def resolve(settings: Any, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    overrides = overrides or {}
    env = env_defaults(settings)
    provider = _clean(overrides.get("llm_provider")).lower()
    if provider not in PROVIDERS:
        provider = env["llm_provider"]

    saved_default = _clean(overrides.get("model"))
    env_default = env["models"].get(provider, "")
    default_model = saved_default or env_default

    saved_stages = overrides.get("stage_models") or {}
    stage_models: dict[str, str] = {}
    stage_sources: dict[str, str] = {}
    for stage, _ in STAGES:
        saved = _clean(saved_stages.get(stage))
        env_stage = (env["any_provider_stage_models"].get(stage)
                     or (env["stage_models"][stage] if provider == "openrouter" else ""))
        if provider == "mock":
            stage_models[stage], stage_sources[stage] = "mock", "default"
        elif saved:
            stage_models[stage], stage_sources[stage] = saved, "settings"
        elif env_stage and not saved_default:
            stage_models[stage], stage_sources[stage] = env_stage, "env"
        else:
            stage_models[stage], stage_sources[stage] = default_model, "default"

    require_free = overrides.get("require_free")
    return {
        "llm_provider": provider,
        "model": default_model,
        "stage_models": stage_models,
        "stage_sources": stage_sources,
        "require_free": env["require_free"] if require_free is None else bool(require_free),
        "sources": {
            "llm_provider": "settings" if _clean(overrides.get("llm_provider")) else "env",
            "model": "settings" if saved_default else "env",
            "require_free": "env" if require_free is None else "settings",
        },
    }
