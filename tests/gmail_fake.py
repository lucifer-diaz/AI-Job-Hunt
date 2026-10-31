"""A fake Google (OAuth token endpoint and the Gmail API reads the inbox uses), served through httpx.MockTransport.

The instrumentation layer (app.services.external_api) still wraps every call, so telemetry and redaction are exercised
exactly as in production. Gmail's search language is evaluated just far enough for the inbox's own queries: `after:`,
`from:(a OR b)` and quoted phrases.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs

import httpx

from app.services.gmail import GMAIL_READONLY_SCOPE

ACCESS_TOKEN = "ya29.test-access-token-0123456789"
REFRESH_TOKEN = "1//test-refresh-token-0123456789abcdef"
CLIENT_SECRET = "GOCSPX-test-client-secret-0123"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@dataclass
class FakeMessage:
    id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime
    headers: dict[str, str] = field(default_factory=dict)
    html: bool = False

    def resource(self, fmt: str) -> dict[str, Any]:
        headers = [{"name": "From", "value": self.sender}, {"name": "Subject", "value": self.subject},
                   {"name": "To", "value": "me@example.com"},
                   {"name": "Date", "value": format_datetime(self.received_at.replace(tzinfo=UTC))},
                   *({"name": k, "value": v} for k, v in self.headers.items())]
        payload: dict[str, Any] = {"mimeType": "multipart/alternative", "headers": headers}
        if fmt == "full":
            mime = "text/html" if self.html else "text/plain"
            payload["parts"] = [{"mimeType": mime, "body": {"data": _b64(self.body)}}]
        return {"id": self.id, "threadId": self.thread_id, "snippet": self.body[:120],
                "internalDate": str(int(self.received_at.replace(tzinfo=UTC).timestamp() * 1000)), "payload": payload}

    def text(self) -> str:
        return f"{self.subject}\n{self.body}".lower()


class FakeGoogle:
    def __init__(self, mailbox: str = "me@example.com"):
        self.mailbox = mailbox
        self.messages: dict[str, FakeMessage] = {}
        self.requests: list[httpx.Request] = []
        self.queries: list[str] = []
        self.token_response: tuple[int, dict[str, Any]] | None = None  # override the refresh-token answer
        self.fail_list: list[int] = []  # status codes the next messages.list calls answer with, in order
        self.scope = GMAIL_READONLY_SCOPE
        self.bad_queries: list[str] = []  # searches Gmail refuses with HTTP 400

    def add(self, message_id: str, *, sender: str, subject: str, body: str, received_at: datetime,
            thread: str | None = None, **extra: Any) -> FakeMessage:
        message = FakeMessage(message_id, thread or f"t{message_id}", sender, subject, body, received_at, **extra)
        self.messages[message_id] = message
        return message

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ───────────── routing ─────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "oauth2.googleapis.com" and path == "/token":
            return self._token(request)
        if host == "oauth2.googleapis.com" and path == "/revoke":
            return httpx.Response(200, json={})
        if host != "gmail.googleapis.com":
            return httpx.Response(404)
        tail = path.removeprefix("/gmail/v1/users/me")
        if tail == "/profile":
            return httpx.Response(200, json={"emailAddress": self.mailbox, "historyId": "4242"})
        if tail == "/messages":
            if self.fail_list:
                code = self.fail_list.pop(0)
                return httpx.Response(code, json={"error": {"code": code}}, headers={"retry-after": "60"})
            return self._list(request)
        if match := re.fullmatch(r"/messages/([^/]+)", tail):
            message = self.messages.get(match.group(1))
            if message is None:
                return httpx.Response(404)
            return httpx.Response(200, json=message.resource(request.url.params.get("format", "full")))
        if match := re.fullmatch(r"/threads/([^/]+)", tail):
            found = [m for m in self.messages.values() if m.thread_id == match.group(1)]
            return httpx.Response(200, json={"messages": [{"id": m.id, "threadId": m.thread_id} for m in found]})
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") == "authorization_code":
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN,
                                             "scope": self.scope, "expires_in": 3599, "token_type": "Bearer"})
        if self.token_response is not None:
            code, body = self.token_response
            return httpx.Response(code, json=body)
        return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "scope": self.scope, "expires_in": 3599})

    def _list(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        self.queries.append(query)
        if self.bad_queries and any(bad in query for bad in self.bad_queries):
            return httpx.Response(400, json={"error": {"code": 400, "message": "Invalid query"}})
        found = [m for m in sorted(self.messages.values(), key=lambda m: m.received_at, reverse=True)
                 if self.matches(query, m)]
        if not found and "fields" in request.url.params:
            return httpx.Response(204)  # what Gmail answers when a partial (fields=) response is empty
        return httpx.Response(200, json={"messages": [{"id": m.id, "threadId": m.thread_id} for m in found]})

    @staticmethod
    def matches(query: str, message: FakeMessage) -> bool:
        after = re.search(r"after:(\d+)", query)
        if after and message.received_at.replace(tzinfo=UTC).timestamp() < int(after.group(1)):
            return False
        sender = message.sender.lower()
        senders = re.search(r"from:\(([^)]*)\)|from:(\S+)", query)
        if senders:
            options = [o.strip().lower() for o in (senders.group(1) or senders.group(2)).split(" OR ")]
            if not any(o and o in sender for o in options):
                return False
        found = lambda p: p.lower() in message.text() or p.lower() in sender
        # ("a" OR "b"): any one of the phrases; every other quoted phrase must be there.
        for group in re.findall(r'\(("[^)]*")\)', query):
            if not any(found(p) for p in re.findall(r'"([^"]+)"', group)):
                return False
            query = query.replace(f"({group})", "")
        phrases = re.findall(r'"([^"]+)"', query)
        return all(found(p) for p in phrases)

    # ───────────── what the tests look at ─────────────

    def gmail_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "gmail.googleapis.com"]

    def fetched(self, message_id: str, fmt: str | None = None) -> int:
        return sum(1 for r in self.gmail_requests() if r.url.path.endswith(f"/messages/{message_id}")
                   and (fmt is None or r.url.params.get("format") == fmt))

    def token_requests(self) -> list[dict[str, str]]:
        return [{k: v[0] for k, v in parse_qs(r.content.decode()).items()} for r in self.requests
                if r.url.path == "/token"]


def dump(value: Any) -> str:
    return json.dumps(value, default=str)
