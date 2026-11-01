from datetime import UTC, datetime, timedelta

import pytest

from app.services import rate_limits as rl

NOW = datetime(2026, 10, 25, 22, 0)


@pytest.mark.parametrize("text, kind, after", [
    ('{"error":"You reached the Free usage limit. Pro is $20/month for $60 of usage, with no 5-hour or weekly caps"}',
     rl.USAGE_CAP, None),
    ('{"message":"Rate limit exceeded: free-models-per-day","metadata":{"headers":{"X-RateLimit-Limit":"50",'
     '"X-RateLimit-Remaining":"0","X-RateLimit-Reset":"1792972800000"}}}', rl.QUOTA, datetime(2026, 10, 26)),
    ("on tokens per minute (TPM): Limit 8000, Used 3492, Requested 5626. Please try again in 8.385s.", rl.THROTTLE,
     NOW + timedelta(seconds=8.385)),
    ("Please try again in 7m12.864s.", rl.THROTTLE, NOW + timedelta(minutes=7, seconds=12.864)),
    ("Request too large for model `openai/gpt-oss-20b` on tokens per minute (TPM): Limit 8000, Requested 9197, "
     "please reduce your message size and try again in 1s", rl.TOO_LARGE, None),
    ("litellm.RateLimitError: Rate limit reached", rl.UNKNOWN, None),
])
def test_a_429_is_read_for_its_kind_and_the_reset_the_provider_named(text, kind, after):
    limit = rl.classify(text, now=NOW)
    assert (limit.kind, limit.until) == (kind, after)


def test_headers_win_and_unbelievable_resets_are_ignored():
    assert rl.classify("Rate limit reached", {"retry-after": "30"}, now=NOW).until == NOW + timedelta(seconds=30)
    epoch = (NOW + timedelta(hours=1)).replace(tzinfo=UTC).timestamp()
    assert rl.classify("x", {"x-ratelimit-reset": str(int(epoch))}, now=NOW).until == NOW + timedelta(hours=1)
    assert rl.classify("Please try again in 900h0m0s", now=NOW).until is None  # beyond a week and a day
    assert rl.classify("X-RateLimit-Reset: 1000000000001", now=NOW).until is None  # in the past
    assert rl.classify(None, object(), now=NOW).kind == rl.UNKNOWN  # never raises
