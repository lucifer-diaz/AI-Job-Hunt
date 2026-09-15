from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.services.ai_config import resolve as resolve_ai_config
from app.services.llm import LLMClient, LLMError, _is_free_openrouter_model, _task_from_prompt


def _settings(**overrides):
    values = dict(
        llm_provider="openrouter",
        openrouter_api_key="sk-or-test",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_model="openrouter/free",
        openrouter_model_rank=None,
        openrouter_model_tailor=None,
        openrouter_model_review=None,
        openrouter_model_followup=None,
        openrouter_fallback_models="",
        openrouter_require_free=True,
        openrouter_max_tokens=5000,
        openrouter_site_url="https://example.com",
        openrouter_app_name="Job Hunt OS",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_task_detection_and_free_model_detection():
    assert _task_from_prompt("FIT_ANALYSIS\n...") == "rank"
    assert _task_from_prompt("TAILOR_RESUME\n...") == "tailor"
    assert _task_from_prompt("REVIEW_RESUME\n...") == "review"
    assert _task_from_prompt("FOLLOW_UP\n...") == "followup"
    assert _is_free_openrouter_model("openrouter/free")
    assert _is_free_openrouter_model("vendor/model:free")
    assert _is_free_openrouter_model("vendor/model:free:online")
    assert not _is_free_openrouter_model("vendor/paid-model")


def test_openrouter_stage_model_and_fallbacks(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"score": 91}'))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    client = LLMClient(overrides={}, keys={"openrouter": "sk-or-user"})
    client.settings = _settings(
        openrouter_model_rank="nvidia/example:free",
        openrouter_fallback_models="vendor/backup:free,openrouter/free",
    )
    result = client.generate_json("system", "FIT_ANALYSIS\njob")

    assert result["score"] == 91
    assert captured["client"]["api_key"] == "sk-or-user"  # the user's own key, never the server's
    assert captured["client"]["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["client"]["default_headers"]["HTTP-Referer"] == "https://example.com"
    assert captured["request"]["model"] == "nvidia/example:free"
    assert captured["request"]["extra_body"]["models"] == ["vendor/backup:free", "openrouter/free"]
    assert captured["request"]["response_format"] == {"type": "json_object"}


def test_missing_user_key_is_a_clear_error():
    client = LLMClient(overrides={})
    client.settings = _settings()
    with pytest.raises(LLMError, match="add your OpenRouter API key in Settings"):
        client.generate_json("system", "FIT_ANALYSIS\njob")


def test_require_free_blocks_accidental_paid_model():
    client = LLMClient(overrides={}, keys={"openrouter": "sk-or-user"})
    client.settings = _settings(openrouter_model_review="anthropic/some-paid-model")

    with pytest.raises(LLMError, match="OPENROUTER_REQUIRE_FREE=true"):
        client.generate_json("system", "REVIEW_RESUME\nresume")


def test_saved_settings_override_env_and_blank_values_fall_back():
    env = _settings(openrouter_model="openrouter/free", openrouter_model_rank="env/rank:free",
                    openrouter_model_tailor="env/tailor:free")
    # Nothing saved: every value comes from .env.
    plain = resolve_ai_config(env, {})
    assert plain["stage_models"] == {"rank": "env/rank:free", "tailor": "env/tailor:free",
                                     "review": "openrouter/free", "followup": "openrouter/free"}
    assert plain["require_free"] is True and plain["sources"]["model"] == "env"

    saved = resolve_ai_config(env, {"stage_models": {"tailor": "db/tailor:free", "review": "  "},
                                    "require_free": False})
    assert saved["stage_models"]["tailor"] == "db/tailor:free" and saved["stage_sources"]["tailor"] == "settings"
    assert saved["stage_models"]["rank"] == "env/rank:free" and saved["stage_models"]["review"] == "openrouter/free"
    assert saved["require_free"] is False

    client = LLMClient(overrides={"require_free": False, "stage_models": {"review": "vendor/paid"}})
    client.settings = env
    assert client._config()["stage_models"]["review"] == "vendor/paid"
