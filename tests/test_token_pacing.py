"""Groq's rate limits, as documented (console.groq.com/docs/rate-limits) and as LiteLLM 1.102.1 forwards them.

Requests are counted per DAY, tokens per minute; resets are durations ("7.66s", "2m59.56s"). A request the minute
cannot take is held until the token reset instead of being sent into a certain 429, and a request larger than the
whole per-minute limit fails once, as a client error, instead of climbing the cooldown ladder.
"""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import ExternalAPIRequestLog, LLMRequestLog, ProviderCooldown, ProviderRateWindow
from app.services import backoff, external_api, failover, model_health, ranker, token_pacing
from app.services.external_api import RateLimitedCooldown, duration_seconds, external_client
from app.services.lifecycle import ErrorCategory
from app.services.llm import LLMContextOverflowError, LLMRateLimitError
from app.services.model_resolution import resolve
from tests.conftest import ensure_test_user
from tests.litellm_fake import forwarded, groq_raw
from tests.test_ai_models import snapshot
from tests.test_litellm_gateway import gateway, ranking_prompt, run  # noqa: F401


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with SessionLocal() as db:
            for model in (ProviderRateWindow, ProviderCooldown, ExternalAPIRequestLog):
                db.execute(delete(model))
            db.commit()
        backoff.reset_cache()
        token_pacing.reset_cache()
        model_health.reset_cache()
        failover.reset_cache()
    wipe()
    yield
    wipe()


class Gateway:
    """The LiteLLM proxy answering one model, counting what reached it."""

    def __init__(self):
        self.calls, self.answers = 0, []

    def transport(self):
        def handle(request):
            self.calls += 1
            status, headers, body = self.answers.pop(0) if self.answers else (200, {}, {"ok": True})
            return httpx.Response(status, headers=headers, json=body)
        return httpx.MockTransport(handle)


def chat(gw, owner, need=None, model="groq-120b"):
    """One model request through the instrumented client, the way llm._litellm_request sends it."""
    metadata = {"model": model, **({"tokens_needed": need} if need is not None else {})}
    with external_client("LITELLM", service="llm", operation="chat_completion", transport=gw.transport()) as client:
        return client.post("http://litellm.test/v1/chat/completions", json={"model": model},
                           extensions={"user_id": owner, "metadata": metadata})


def rows():
    with SessionLocal() as db:
        return db.scalars(select(ExternalAPIRequestLog).order_by(ExternalAPIRequestLog.id)).all()


def window(owner, model="groq-120b"):
    with SessionLocal() as db:
        return db.scalar(select(ProviderRateWindow).where(ProviderRateWindow.user_id == owner,
                                                          ProviderRateWindow.provider == f"LITELLM:{model}"))


# ───────────── reading the headers ─────────────

@pytest.mark.parametrize("raw, seconds", [("7.66s", 7.66), ("2m59.56s", 179.56), ("1h2m", 3720.0), ("120ms", 0.12),
                                          ("6m0s", 360.0), ("2", 2.0), ("0.5", 0.5), ("", None), ("soon", None),
                                          (None, None), ("Wed, 21 Oct 2026 07:28:00 GMT", None)])
def test_durations_are_read_as_groq_and_openai_write_them(raw, seconds):
    assert duration_seconds(raw) == (pytest.approx(seconds) if seconds is not None else None)


def test_groqs_token_window_is_recorded_and_its_daily_request_count_is_labelled(owner):
    gw = Gateway()
    gw.answers.append((200, forwarded(groq_raw(1200, requests_remaining=987)), {"ok": True}))
    chat(gw, owner)
    row = rows()[-1]
    assert row.rate_limit_remaining == 1200  # the per-minute tokens left, not the 987 requests left today
    assert timedelta(seconds=7) < row.rate_limit_reset_at - row.completed_at < timedelta(seconds=8)
    assert row.request_metadata["rate_limits"] == {
        "tokens_per_minute_limit": 8000, "tokens_per_minute_remaining": 1200, "tokens_reset_seconds": 7.66,
        "requests_limit": 1000, "requests_remaining": 987, "requests_reset_seconds": 179.56, "requests_window": "day"}
    stored = window(owner)
    assert stored.tokens_remaining == 1200 and stored.tokens_limit == 8000 and stored.requests_window == "day"


def test_a_groq_429_through_litellm_obeys_the_forwarded_retry_after(owner):
    gw = Gateway()  # on a refusal LiteLLM forwards Retry-After only as llm_provider-retry-after
    gw.answers.append((429, forwarded(groq_raw(0, retry_after="120"), refused=True), {"error": "slow down"}))
    chat(gw, owner)
    row = rows()[-1]
    assert row.error_type == "RATE_LIMIT"
    assert timedelta(seconds=119) < row.rate_limit_reset_at - row.completed_at <= timedelta(seconds=120)
    with SessionLocal() as db:
        cooldown = db.scalar(select(ProviderCooldown).where(ProviderCooldown.provider == "LITELLM:GROQ-120B"))
    assert cooldown.until - row.completed_at >= timedelta(seconds=119)  # the provider's wait beat the 30s ladder


def test_openai_style_headers_keep_the_request_count(owner):
    gw = Gateway()
    gw.answers.append((200, {"x-ratelimit-limit-requests": "60", "x-ratelimit-remaining-requests": "59",
                             "x-ratelimit-reset-requests": "1s", "x-ratelimit-limit-tokens": "150000",
                             "x-ratelimit-remaining-tokens": "149000", "x-ratelimit-reset-tokens": "6m0s"}, {}))
    with external_client("OPENROUTER", transport=gw.transport()) as client:
        client.get("https://openrouter.test/x", extensions={"user_id": owner})
    row = rows()[-1]
    assert row.rate_limit_remaining == 59  # as before: OpenAI's request counter is per minute
    assert timedelta(0) < row.rate_limit_reset_at - row.completed_at <= timedelta(seconds=1)
    assert row.request_metadata["rate_limits"]["tokens_per_minute_remaining"] == 149000
    assert "requests_window" not in row.request_metadata["rate_limits"]  # unknown, never guessed
    assert window(owner) is None  # only gateway model requests are paced


@pytest.mark.parametrize("headers, remaining, reset_seconds", [
    ({"anthropic-ratelimit-requests-remaining": "49",
      "anthropic-ratelimit-requests-reset": "2026-10-26T12:00:30Z"}, 49, None),
    ({"x-ratelimit-remaining": "7", "x-ratelimit-reset": "30"}, 7, 30),
    ({"ratelimit-remaining": "3", "retry-after": "12"}, 3, 12),
])
def test_other_providers_headers_are_read_as_before(owner, headers, remaining, reset_seconds):
    gw = Gateway()
    gw.answers.append((200, headers, {}))
    with external_client("ANTHROPIC", transport=gw.transport()) as client:
        client.get("https://provider.test/x", extensions={"user_id": owner})
    row = rows()[-1]
    assert row.rate_limit_remaining == remaining
    if reset_seconds is None:
        assert row.rate_limit_reset_at is None
    else:
        assert row.rate_limit_reset_at - row.completed_at == timedelta(seconds=reset_seconds)
    assert "rate_limits" not in (row.request_metadata or {})


# ───────────── pacing ─────────────

def test_a_minute_the_last_answer_says_is_spent_is_waited_out_without_a_429(owner):
    gw = Gateway()
    gw.answers.append((200, forwarded(groq_raw(1500, tokens_reset="42s")), {"ok": True}))
    chat(gw, owner, need=6000)
    with pytest.raises(RateLimitedCooldown) as held:
        chat(gw, owner, need=6000)
    assert gw.calls == 1  # nothing was sent into a certain 429
    assert 40 <= held.value.retry_after_seconds <= 42 and "6000" in str(held.value)
    skipped = rows()[-1]
    assert skipped.request_metadata["skipped_by"] == "token_pacing" and skipped.http_status_code is None
    with SessionLocal() as db:
        assert db.scalar(select(ProviderCooldown)) is None  # a planned wait never climbs the 429 ladder
    chat(gw, owner, need=1000)  # a small request still fits the minute ...
    assert gw.calls == 2 and window(owner).tokens_remaining == 500  # ... and its tokens stay spent
    assert window(owner).tokens_reserved == 0  # answered (without headers): no longer in flight


def test_the_reservation_is_shared_so_two_workers_never_both_spend_the_minute(owner):
    gw = Gateway()
    gw.answers.append((200, forwarded(groq_raw(7000, tokens_reset="50s")), {"ok": True}))
    chat(gw, owner, need=100)
    assert token_pacing.reserve(owner, "LITELLM:groq-120b", 6000) == (None, 6000)  # the first worker takes it
    wait, taken = token_pacing.reserve(owner, "LITELLM:groq-120b", 6000)
    assert wait and not taken  # the second waits for the reset


def test_an_answer_keeps_the_reservations_of_requests_still_in_flight(owner):
    key = "LITELLM:groq-120b"
    token_pacing.observe(owner, key, token_pacing.Window(tokens_limit=8000, tokens_remaining=8000,
                                                         tokens_reset_seconds=50))
    assert token_pacing.reserve(owner, key, 3500) == (None, 3500)  # worker A
    assert token_pacing.reserve(owner, key, 3500) == (None, 3500)  # worker B, still in flight
    # A's answer: the provider has seen A only, so it says 4500 left; B's 3500 stays reserved
    token_pacing.observe(owner, key, token_pacing.Window(tokens_limit=8000, tokens_remaining=4500,
                                                         tokens_reset_seconds=45), released=3500)
    assert window(owner).tokens_remaining == 1000 and window(owner).tokens_reserved == 3500
    wait, taken = token_pacing.reserve(owner, key, 3500)
    assert wait and not taken  # a third request would overrun the minute


def test_a_failed_request_gives_its_reservation_back(owner):
    gw = Gateway()
    gw.answers.append((200, forwarded(groq_raw(7000, tokens_reset="50s")), {"ok": True}))
    chat(gw, owner, need=10)

    def down(request):
        raise httpx.ConnectError("refused", request=request)
    with external_client("LITELLM", service="llm", transport=httpx.MockTransport(down)) as client, \
            pytest.raises(httpx.ConnectError):
        client.post("http://litellm.test/v1/chat/completions", json={},
                    extensions={"user_id": owner, "metadata": {"model": "groq-120b", "tokens_needed": 6000}})
    assert window(owner).tokens_remaining == 7000 and window(owner).tokens_reserved == 0


def test_a_fallbacks_window_is_kept_under_the_model_that_answered(owner):
    gw = Gateway()
    gw.answers.append((200, forwarded(groq_raw(7000, tokens_reset="50s")), {"ok": True}))
    chat(gw, owner, need=10)  # the primary's own window
    gw.answers.append((200, {**forwarded(groq_raw(0, tokens_reset="50s")), "x-litellm-model-group": "groq-20b"},
                       {"ok": True}))
    chat(gw, owner, need=1000)  # LiteLLM's fallback answered, with a spent window of its own
    primary, fallback = window(owner), window(owner, "groq-20b")
    assert primary.tokens_remaining == 7000 and primary.tokens_reserved == 0  # the reservation went back
    assert fallback.tokens_remaining == 0
    assert chat(gw, owner, need=1000).status_code == 200  # the primary is not held for the fallback's minute


def test_after_the_reset_or_without_a_window_the_request_is_sent(owner):
    gw = Gateway()
    assert chat(gw, owner, need=6000).status_code == 200  # nothing known: send
    gw.answers.append((200, forwarded(groq_raw(100, tokens_reset="1s")), {"ok": True}))
    chat(gw, owner, need=10)
    later = utcnow() + timedelta(seconds=5)
    assert token_pacing.reserve(owner, "LITELLM:groq-120b", 6000, now=later) == (None, 0)  # the window has reset


def test_a_request_larger_than_the_whole_limit_is_sent_once_for_the_providers_reason(owner):
    gw = Gateway()
    gw.answers.append((200, forwarded(groq_raw(100, tokens_reset="50s")), {"ok": True}))
    chat(gw, owner, need=10)
    assert token_pacing.reserve(owner, "LITELLM:groq-120b", 9500) == (None, 0)  # waiting could never make it fit


def test_pacing_fails_open(owner, monkeypatch):
    gw = Gateway()
    gw.answers.append((200, forwarded(groq_raw(0, tokens_reset="50s")), {"ok": True}))
    chat(gw, owner, need=10)

    def broken():
        raise RuntimeError("database down")
    monkeypatch.setattr(token_pacing, "_session_factory", lambda: broken)
    assert chat(gw, owner, need=6000).status_code == 200 and gw.calls == 2


def test_a_task_a_person_runs_now_is_not_held(owner):
    gw = Gateway()
    gw.answers.append((200, forwarded(groq_raw(0, tokens_reset="50s")), {"ok": True}))
    chat(gw, owner, need=10)
    with backoff.bypass():
        assert chat(gw, owner, need=6000).status_code == 200


def test_a_model_waiting_for_its_window_moves_behind_its_backups(owner):
    w = resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast"}), "RANKER")
    assert failover.apply(w, owner, tokens_needed=6000) is w
    token_pacing.observe(owner, "LITELLM:mid", token_pacing.Window(tokens_limit=8000, tokens_remaining=200,
                                                                   tokens_reset_seconds=40))
    moved = failover.apply(w, owner, tokens_needed=6000)
    assert moved.model == "fast" and moved.failed_over_from == "mid" and "token window" in moved.note
    assert failover.apply(w, owner, tokens_needed=100) is w  # a request the minute can still take stays put
    assert model_health.states(owner) == {}  # an overlay: route health is untouched


def test_held_requests_never_reach_the_scheduled_health_sweep(owner):
    import uuid

    now = model_health.utcnow()
    with SessionLocal() as db:
        for _ in range(6):
            db.add(LLMRequestLog(request_id=str(uuid.uuid4()), user_id=owner, provider="litellm",
                                 requested_model="paced-model", model="paced-model", worker_type="RANKER",
                                 status="FAILED", error_type="RATE_LIMIT", cost_source="not_sent", created_at=now))
        db.commit()
        stats = model_health.window_stats(db, owner, "paced-model", since=now - timedelta(minutes=5))
        db.execute(delete(LLMRequestLog).where(LLMRequestLog.requested_model == "paced-model"))
        db.commit()
    assert stats.samples == 0


def test_a_held_request_never_counts_against_the_models_health(owner):
    for _ in range(5):
        model_health.record_outcome({"user_id": owner, "provider": "litellm", "requested_model": "mid",
                                     "status": "FAILED", "error_type": "RATE_LIMIT", "worker_type": "RANKER",
                                     "cost_source": "not_sent"})
    assert not model_health.resting(owner, "mid", "RANKER")


# ───────────── a request larger than the whole per-minute limit ─────────────

TOO_LARGE = ("Request too large for model `openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` on "
             "tokens per minute (TPM): Limit 8000, Requested 9500, please reduce your message size and try again.")


@pytest.mark.parametrize("status", [413, 429])  # Groq's own 413, or the 429 LiteLLM maps it to
def test_too_large_is_a_client_error_not_a_cooldown(owner, status):
    gw = Gateway()
    gw.answers.append((status, forwarded(groq_raw(8000), refused=True),
                       {"error": {"message": TOO_LARGE, "type": "tokens", "code": "rate_limit_exceeded"}}))
    chat(gw, owner, need=9500)
    row = rows()[-1]
    assert row.error_type == "CLIENT_ERROR" and row.status == "FAILED"
    assert row.request_metadata["refused"] == "REQUEST_TOO_LARGE"
    assert row.request_metadata["tokens_requested"] == 9500 and row.request_metadata["tokens_per_minute_limit"] == 8000
    with SessionLocal() as db:
        assert db.scalar(select(ProviderCooldown)) is None
    assert window(owner) is None  # a refusal says nothing about the minute's window
    assert chat(gw, owner, need=10).status_code == 200  # nothing is held afterwards


def test_too_large_is_recognised_only_by_its_message():
    assert external_api.too_large(TOO_LARGE)["tokens_requested"] == 9500
    assert external_api.too_large("Rate limit reached for model on tokens per minute (TPM): try again in 7s") is None
    assert external_api.too_large(b"") is None


def test_through_the_gateway_too_large_fails_once_with_its_reason(gateway, litellm):  # noqa: F811
    uid = gateway.user_id
    model = "tier-2-reasoning"
    litellm.too_large.add(model)
    with pytest.raises(LLMContextOverflowError) as refused:
        run(gateway, ranking_prompt())
    assert "per-minute token limit" in str(refused.value) and "waiting cannot help" in str(refused.value)
    assert ranker.classify(refused.value) == (ErrorCategory.VALIDATION, False, False)  # one failure, no retries
    assert not model_health.resting(uid, model, "RANKER")
    with SessionLocal() as db:
        assert db.scalar(select(ProviderCooldown).where(ProviderCooldown.user_id == uid)) is None
        last = db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == uid)
                          .order_by(LLMRequestLog.id.desc())).first()
    assert last.error_type == "CONTEXT_OVERFLOW"


def test_through_the_gateway_a_spent_minute_waits_instead_of_a_429(gateway, litellm):  # noqa: F811
    uid = gateway.user_id
    model = "tier-2-reasoning"
    litellm.provider_headers[model] = [groq_raw(0, tokens_reset="45s")]
    run(gateway, ranking_prompt())
    sent = len(litellm.chat_bodies)
    with pytest.raises(LLMRateLimitError):  # the ranker's usual "try later", without a request
        run(gateway, ranking_prompt(2))
    assert len(litellm.chat_bodies) == sent
    assert not model_health.resting(uid, model, "RANKER")
    with SessionLocal() as db:
        held = db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == uid)
                          .order_by(LLMRequestLog.id.desc())).first()
    assert held.error_type == "RATE_LIMIT"
