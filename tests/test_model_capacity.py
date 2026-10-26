import uuid

import pytest
from sqlalchemy import delete

from app.db import SessionLocal
from app.models import LLMRequestLog
from app.services import model_capacity, model_health, rate_limits
from tests.conftest import ensure_test_user

TPM = ("LLMRateLimitError: GroqException - {\"error\":{\"message\":\"Request too large for model `openai/gpt-oss-120b` in "
       "organization `org_x` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested {n}, please "
       "reduce your message size and try again.\"}}")
OTPM = ("Request too large for model `qwen/qwen3.8-27b` in organization `org_x` service tier `on_demand` on output "
        "tokens per minute (OTPM): Limit 1000, Requested {n}. The request's expected output tokens exceed the limit")


def snapshot(**ranker):
    return {"models": {"Groq": {"enabled": True, "model_type": "CUSTOM", "context_window": 131072},
                       "roomy": {"enabled": True, "model_type": "CUSTOM", "context_window": 262144},
                       "tiny": {"enabled": True, "model_type": "CUSTOM", "context_window": 2048},
                       "vectors": {"enabled": True, "model_type": "EMBEDDING", "context_window": 8192},
                       "off": {"enabled": False, "model_type": "CUSTOM", "context_window": 262144}},
            "tiers": {}, "routing": {},
            "workers": {"RANKER": {"mode": "FIXED", "primary_model": "Groq", "enabled": True, **ranker}}}


@pytest.fixture
def owner():
    user = ensure_test_user().id
    yield user
    with SessionLocal() as db:
        db.execute(delete(LLMRequestLog).where(LLMRequestLog.request_id.like("capacity-test-%")))
        db.commit()


def log(owner, *, status="FAILED", message=None, model="Groq", tokens_in=None, tokens_out=None, estimate=None):
    with SessionLocal() as db:
        db.add(LLMRequestLog(request_id=f"capacity-test-{uuid.uuid4()}", user_id=owner, provider="litellm",
                             stage="OTHER", requested_model=model, model=model, worker_type="RANKER", status=status,
                             error_type="RATE_LIMIT" if status == "FAILED" else None, error_message=message,
                             input_tokens=tokens_in, output_tokens=tokens_out, estimated_context_tokens=estimate,
                             latency_ms=100, created_at=model_health.utcnow()))
        db.commit()


def report(owner, **ranker):
    with SessionLocal() as db:
        return model_capacity.report(db, owner, snapshot(**ranker))


def test_the_providers_stated_limit_is_read_from_a_too_large_refusal():
    limit = rate_limits.size_limit(TPM.replace("{n}", "9197"))
    assert (limit.deployment, limit.kind, limit.limit, limit.requested, limit.counts) == \
        ("openai/gpt-oss-120b", "TPM", 8000, 9197, "total")
    assert rate_limits.size_limit("Rate limit reached … Please try again in 8s") is None  # a throttle, not a size


def test_an_answer_cap_is_offered_only_when_the_workers_answers_fit_under_it(owner):
    for _ in range(3):
        log(owner, message=OTPM.replace("{n}", "4096"), estimate=3000)
        log(owner, status="SUCCEEDED", model="roomy", tokens_in=3000, tokens_out=600)
    [group] = report(owner)
    assert group["worker_type"] == "RANKER" and group["model"] == "Groq" and group["count"] == 3
    assert group["output_cap"] == {"value": 1000, "typical_output": 600, "known_safe": True, "applied": False,
                                   "current": None}
    assert report(owner, max_output_tokens=900)[0]["output_cap"]["applied"]  # the fix is in place
    names = [m["name"] for m in group["models"]]
    assert names[0] == "roomy" and group["models"][0]["proven"]  # it already answered requests this size
    assert "tiny" not in names and "vectors" not in names and "off" not in names and "Groq" not in names


def test_no_cap_when_it_would_cut_answers_short_or_the_prompt_alone_is_over(owner):
    log(owner, message=TPM.replace("{n}", "12000"), estimate=6000)
    log(owner, status="SUCCEEDED", model="roomy", tokens_in=6000, tokens_out=5000)
    [group] = report(owner)
    assert group["output_cap"] is None and not group["input_over"]  # 8000 − 6600 leaves less than the answers need
    log(owner, message=TPM.replace("{n}", "15000"), estimate=9000)
    log(owner, message=TPM.replace("{n}", "15000"), estimate=9000)
    assert report(owner)[0]["input_over"]


def test_a_model_the_worker_no_longer_uses_is_not_reported(owner):
    log(owner, message=TPM.replace("{n}", "9000"), estimate=5000, model="old")
    assert report(owner) == []
