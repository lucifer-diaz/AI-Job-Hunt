"""What happens after a provider says 429.

The point of the backoff is the calls that are *not* made: one rate limit must not turn into a burst of them
while a queue of work keeps asking. Everything here drives the real instrumented client, so it exercises the
path production takes.
"""
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import ExternalAPIRequestLog, ProviderCooldown
from app.services import backoff, external_api
from app.services.backoff import RateLimitPolicy
from app.settings import get_settings
from tests.conftest import ensure_test_user, make_user

client = TestClient(app)


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean(owner):
    with SessionLocal() as db:
        db.execute(delete(ProviderCooldown))
        db.execute(delete(ExternalAPIRequestLog))
        db.commit()
    backoff.reset_cache()
    yield
    backoff.reset_cache()


class Provider:
    """A third party that can be told to rate limit, counting what actually reached it."""

    def __init__(self, retry_after: str | None = None):
        self.calls, self.limit, self.retry_after = 0, False, retry_after

    def transport(self):
        def handle(request):
            self.calls += 1
            if self.limit:
                headers = {"retry-after": self.retry_after} if self.retry_after else {}
                return httpx.Response(429, headers=headers, json={"error": "slow down"})
            return httpx.Response(200, json={"ok": True})

        return httpx.MockTransport(handle)


def call(provider_name, upstream, owner, path="/v1/thing"):
    with external_api.external_client(provider_name, service="api", operation="request",
                                      transport=upstream.transport(), user_id=owner) as http:
        return http.get(f"https://{provider_name.lower()}.test{path}")


def cooldown(owner, provider="OPENROUTER"):
    with SessionLocal() as db:
        return db.scalar(select(ProviderCooldown).where(ProviderCooldown.user_id == owner,
                                                        ProviderCooldown.provider == provider))


# ───────────── the ladder ─────────────

def test_each_rate_limit_in_a_row_waits_longer():
    policy = RateLimitPolicy(base_seconds=30, multiplier=2.0, max_seconds=900)
    assert [round(policy.delay(n).total_seconds()) for n in (1, 2, 3, 4, 5)] == [30, 60, 120, 240, 480]
    assert policy.ladder() == [30, 60, 120, 240, 480]
    assert policy.delay(20).total_seconds() == 900, "the wait is capped, and stays capped"
    assert policy.delay(0).total_seconds() == 30, "the first rate limit is the first rung"


def test_a_policy_is_clamped_to_something_sensible():
    assert backoff.policy_from({"base_seconds": 0, "multiplier": 99, "max_seconds": 1}).base_seconds >= 1
    assert backoff.policy_from({"multiplier": 0.1}).multiplier >= 1.0
    assert backoff.policy_from({"base_seconds": "nonsense"}).base_seconds == get_settings().rate_limit_base_seconds
    assert backoff.policy_from(None).enabled is get_settings().rate_limit_backoff_enabled


# ───────────── the calls that are not made ─────────────

def test_after_a_rate_limit_the_provider_is_left_alone(owner):
    upstream = Provider()
    assert call("OPENROUTER", upstream, owner).status_code == 200
    upstream.limit = True
    assert call("OPENROUTER", upstream, owner).status_code == 429
    reached = upstream.calls

    for _ in range(5):
        with pytest.raises(external_api.RateLimitedCooldown) as refused:
            call("OPENROUTER", upstream, owner)
    assert upstream.calls == reached, "not one of them reached the provider"
    assert refused.value.retry_after_seconds > 0 and refused.value.provider == "OPENROUTER"

    row = cooldown(owner)
    assert row.consecutive == 1 and row.calls_skipped == 5
    assert row.until > utcnow()


def test_the_calls_it_saved_are_visible_in_monitoring(owner):
    upstream = Provider()
    upstream.limit = True
    call("OPENROUTER", upstream, owner)
    with pytest.raises(external_api.RateLimitedCooldown):
        call("OPENROUTER", upstream, owner)

    listed = client.get("/api/monitoring/external/requests?provider=OPENROUTER").json()["items"]
    assert len(listed) == 2
    held = next(row for row in listed if row["http_status_code"] is None)
    assert held["error_type"] == "RATE_LIMIT" and held["status"] == "FAILED"
    detail = client.get(f"/api/monitoring/external/requests/{held['id']}").json()
    assert detail["metadata"]["skipped_by"] == "cooldown"
    assert detail["metadata"]["retry_after_seconds"] > 0

    dashboard = client.get("/api/monitoring/external/dashboard").json()
    assert [c["provider"] for c in dashboard["cooldowns"]] == ["OPENROUTER"]
    assert dashboard["cooldowns"][0]["calls_skipped"] == 1


def test_one_provider_backing_off_never_holds_up_another(owner):
    limited, fine = Provider(), Provider()
    limited.limit = True
    call("OPENROUTER", limited, owner)
    with pytest.raises(external_api.RateLimitedCooldown):
        call("OPENROUTER", limited, owner)

    assert call("APIFY", fine, owner).status_code == 200, "Apify has not been rate limited"
    assert fine.calls == 1
    assert cooldown(owner, "APIFY") is None


def test_one_account_backing_off_never_holds_up_another(owner):
    theirs = make_user(SessionLocal, email=f"other-{uuid.uuid4().hex[:8]}@example.com")
    upstream = Provider()
    upstream.limit = True
    call("OPENROUTER", upstream, owner)
    with pytest.raises(external_api.RateLimitedCooldown):
        call("OPENROUTER", upstream, owner)

    upstream.limit = False
    assert call("OPENROUTER", upstream, theirs).status_code == 200
    assert cooldown(theirs) is None


def test_the_providers_own_retry_after_wins_when_it_asks_for_longer(owner):
    upstream = Provider(retry_after="600")  # ten minutes, far beyond the first rung
    upstream.limit = True
    call("OPENROUTER", upstream, owner)
    left = (cooldown(owner).until - utcnow()).total_seconds()
    assert 540 <= left <= 610, "the provider knows when it will serve again"


def test_an_absurd_retry_after_does_not_park_a_provider_for_ever(owner):
    upstream = Provider(retry_after=str(60 * 60 * 24 * 30))  # a month
    upstream.limit = True
    call("OPENROUTER", upstream, owner)
    left = (cooldown(owner).until - utcnow()).total_seconds()
    assert left <= get_settings().rate_limit_retry_after_max_seconds


def test_a_normal_answer_ends_the_backoff(owner):
    upstream = Provider()
    upstream.limit = True
    call("OPENROUTER", upstream, owner)
    with SessionLocal() as db:  # let the wait pass
        db.query(ProviderCooldown).update({"until": utcnow() - timedelta(seconds=1)})
        db.commit()
    backoff.reset_cache()

    upstream.limit = False
    assert call("OPENROUTER", upstream, owner).status_code == 200
    assert cooldown(owner) is None, "the ladder starts from the bottom next time"


def test_repeated_rate_limits_climb_the_ladder(owner):
    upstream = Provider()
    upstream.limit = True
    for expected in (1, 2, 3):
        call("OPENROUTER", upstream, owner)
        row = cooldown(owner)
        assert row.consecutive == expected
        with SessionLocal() as db:  # each wait passes before the next attempt
            db.query(ProviderCooldown).update({"until": utcnow() - timedelta(seconds=1)})
            db.commit()
        backoff.reset_cache()
    assert row.consecutive == 3


def test_turning_the_backoff_off_lets_every_call_through(owner):
    client.patch("/api/settings", json={"rate_limit_backoff": False})
    upstream = Provider()
    upstream.limit = True
    for _ in range(3):
        assert call("OPENROUTER", upstream, owner).status_code == 429
    assert upstream.calls == 3, "without the backoff every attempt goes to the provider"
    assert cooldown(owner) is None
    client.patch("/api/settings", json={"rate_limit_backoff": True})


# ───────────── the model layer ─────────────

def test_a_model_call_during_a_cooldown_is_a_rate_limit_the_ranker_understands(owner):
    from app.services.llm import LLMClient, LLMRateLimitError
    from app.services.ranker import classify

    backoff.record_limit(owner, "MOCK", policy=RateLimitPolicy(base_seconds=60))
    with SessionLocal() as db:
        db.info["user_id"] = owner
        with pytest.raises(LLMRateLimitError) as raised:
            LLMClient(overrides={"llm_provider": "mock"}, user_id=owner).generate_json("system", "user")
    assert "rate limiting" in str(raised.value)
    category, retryable, _ = classify(raised.value)
    assert retryable and category.value == "RATE_LIMITED", "the work is retried later, not failed"


# ───────────── the settings ─────────────

def test_the_account_sets_its_own_backoff(owner):
    saved = client.patch("/api/settings", json={"rate_limit_base_seconds": 45, "rate_limit_multiplier": 3,
                                                "rate_limit_max_seconds": 600}).json()["rate_limits"]
    assert saved == {"enabled": True, "base_seconds": 45, "multiplier": 3.0, "max_seconds": 600,
                     "ladder": [45, 135, 405, 600, 600]}
    assert client.get("/api/settings").json()["rate_limits"]["base_seconds"] == 45

    upstream = Provider()
    upstream.limit = True
    call("OPENROUTER", upstream, owner)
    left = (cooldown(owner).until - utcnow()).total_seconds()
    assert 40 <= left <= 46, "the account's own first wait is used"

    assert client.patch("/api/settings", json={"rate_limit_multiplier": 50}).status_code == 422
    assert client.patch("/api/settings", json={"rate_limit_base_seconds": 0}).status_code == 422
    assert client.get("/api/settings").json()["rate_limits"]["base_seconds"] == 45, "a refusal changes nothing"
    client.patch("/api/settings", json={"rate_limit_base_seconds": 30, "rate_limit_multiplier": 2,
                                        "rate_limit_max_seconds": 900})


def test_a_broken_backoff_store_never_blocks_a_call(owner, monkeypatch):
    monkeypatch.setattr(backoff, "_session_factory", lambda: (_ for _ in ()).throw(RuntimeError("no database")))
    upstream = Provider()
    assert call("OPENROUTER", upstream, owner).status_code == 200, "the call still goes through"
