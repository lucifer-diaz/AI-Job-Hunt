"""Redaction must stay linear: `re` holds the GIL, so one slow pattern freezes every request the server is handling."""
import time

import pytest

from app.services import redaction


@pytest.mark.parametrize("text", ["a" * 200_000, "-" * 200_000, "Ab3_-" * 40_000, "a.b." * 50_000])
def test_long_runs_of_word_characters_are_redacted_in_bounded_time(text):
    started = time.monotonic()
    redaction.redact_text(text, 200_000)
    assert time.monotonic() - started < 3  # was quadratic: minutes at this size


def test_credential_named_fields_are_still_redacted():
    out = redaction.redact_text('{"user_api_key": "abc12345", "x-foo-secret": "s3cretvalue", database_url=postgres_pw}')
    assert "abc12345" not in out and "s3cretvalue" not in out and "postgres_pw" not in out
    assert '"user_api_key": ' in out  # the name stays, only the value goes
