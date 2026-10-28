"""Helpers for the application execution tests (Phase 5): a local form server that counts every request by method,
a fake application browser that reads the fixture pages and records what was filled, an approved job, saved answers,
and `drive`, which runs the real `application_execution` scheduler pass until nothing changes.

Nothing here reaches the internet: every page is served from tests/fixtures/application_forms.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.infrastructure.application_browser.service import (
    ApplicationPage,
    ApplicationSessionManager,
    PageLoad,
)
from app.models import (
    ApplicationExecutionTask,
    CandidateProfile,
    Job,
    KnowledgeAnswer,
    ResumeVariant,
)
from app.services import scheduled_jobs as sj
from app.services import workflow
from app.services.knowledge import question_key
from app.settings import get_settings
from tests.conftest import make_user

FORMS = Path(__file__).resolve().parent / "fixtures" / "application_forms"
RESUME = """# Asha Rao
asha@example.test

## Experience
### Senior Engineer | Acme Corp | 2021 - Present
- Scaled the payments API to 25k requests per second with Python and Kafka
"""
PYTHON_QUESTION = "How many years of Python experience do you have?"
AUTH_QUESTION = "Are you legally authorized to work in Singapore?"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def form(name: str = "greenhouse_form") -> str:
    return (FORMS / f"{name}.html").read_text()


# ───────────── a local form server ─────────────

class FormServer:
    """Serves the fixture pages at /<name>.html and counts every request by method. A POST anywhere (the form's
    action, a beacon) would be a submission attempt: the tests assert there are none."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _answer(self, method: str) -> None:
                server.requests.append((method, self.path))
                name = self.path.lstrip("/").split("?")[0]
                path = FORMS / name
                if method == "GET" and name.endswith(".html") and path.exists():
                    data, status = path.read_bytes(), 200
                else:
                    data, status = b"<html><body>nothing here</body></html>", 404 if method == "GET" else 200
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "application_session=from-the-form; Path=/")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._answer("GET")

            def do_POST(self) -> None:
                self._answer("POST")

            def do_PUT(self) -> None:
                self._answer("PUT")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, name: str = "greenhouse_form") -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}.html"

    @property
    def posts(self) -> list[str]:
        return [path for method, path in self.requests if method != "GET"]

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ───────────── a fake application browser ─────────────

class FakePage(ApplicationPage):
    """Reads a fixture page; fills are recorded, never sent. A locator must match an element of the page."""

    def __init__(self, browser: FakeSessions):
        self._browser = browser
        self._html = ""
        self._soup: BeautifulSoup | None = None
        self.values: dict[str, Any] = {}

    async def goto(self, url: str) -> PageLoad:
        self._browser.loads.append(url)
        if url in self._browser.failures:  # a page that does not load: NavigationFailed(category, message)
            from app.infrastructure.browser.service import NavigationFailed

            raise NavigationFailed(*self._browser.failures[url])
        short = url.rsplit("/", 1)[-1].removesuffix(".html")
        self._html = form(self._browser.pages.get(url) or self._browser.pages.get(short) or short)
        self._soup = BeautifulSoup(self._html, "html.parser")
        title = self._soup.title.get_text() if self._soup.title else ""
        return PageLoad(url=url, final_url=url, status=200, title=title)

    async def html(self) -> str:
        return self._html

    def _require(self, locator: str) -> None:
        assert self._soup is not None and self._soup.select_one(locator) is not None, f"no element at {locator}"

    async def fill(self, locator: str, text: str) -> None:
        self._require(locator)
        self.values[locator] = text

    async def select(self, locator: str, value: str) -> None:
        self._require(locator)
        self.values[locator] = value

    async def check(self, locator: str) -> None:
        self._require(locator)
        self.values[locator] = True

    async def upload(self, locator: str, path: str) -> None:
        self._require(locator)
        assert Path(path).exists(), "uploads only an existing local file"
        self.values[locator] = Path(path).name

    async def value_of(self, locator: str) -> str:
        return str(self.values.get(locator) or "")

    async def is_checked(self, locator: str) -> bool:
        return self.values.get(locator) is True

    @property
    def blocked_requests(self) -> int:
        return 0


class FakeSessions(ApplicationSessionManager):
    """Hands out FakePages. `pages` maps a URL (or a page name) to the fixture to serve instead."""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.failures: dict[str, tuple[str, str]] = {}
        self.loads: list[str] = []
        self.opened = self.closed = 0
        self.filled: list[dict[str, Any]] = []

    def is_available(self) -> bool:
        return True

    @asynccontextmanager
    async def session(self, *, user_id: int, ats: str, job_id: int | None = None):
        self.opened += 1
        page = FakePage(self)
        try:
            yield page
        finally:
            self.closed += 1
            if page.values:
                self.filled.append(dict(page.values))


# ───────────── accounts, jobs and answers ─────────────

def configure(monkeypatch, mode: str = "assist") -> None:
    settings = get_settings()
    for name, value in (("agent_graphs_enabled", True), ("application_execution_mode", mode),
                        ("llm_provider", "mock"), ("rag_retrieval_mode", "off"), ("rag_vector_enabled", False),
                        ("job_insights_enabled", False), ("browser_use_webui_enabled", False),
                        ("application_auth_sessions_enabled", False), ("application_login_domains", "")):
        monkeypatch.setattr(settings, name, value)


def owner(sessions) -> int:
    user_id = make_user(sessions)
    with sessions() as db:
        db.add(CandidateProfile(user_id=user_id, name="Asha Rao", email="asha@example.test", phone="+65 5555 0100",
                                linkedin_url="https://www.linkedin.com/in/asha-rao-test", master_resume=RESUME,
                                work_authorization="Singapore PR"))
        db.commit()
    return user_id


def approved_job(sessions, user_id: int, url: str, **fields: Any) -> int:
    """A job whose packet was approved through workflow.approve_job (so it has an approval snapshot and digest)."""
    from app.services.lifecycle import WorkflowState as S

    with sessions() as db:
        db.info["user_id"] = user_id
        job = Job(user_id=user_id, source="test", source_ref=uuid.uuid4().hex, title="Senior Backend Engineer",
                  company="Examplecorp", location="Singapore", url=url, description="Python", fit_score=80,
                  status="tailored", state=S.QUALIFIED, **fields)
        db.add(job)
        db.flush()
        db.add(ResumeVariant(user_id=user_id, job_id=job.id, version=1, markdown=RESUME,
                             cover_letter="Dear Examplecorp, I build payment systems.", content={},
                             reviewer_score=90))
        db.commit()
        workflow.approve_job(db, job)
        return job.id


def answer(sessions, user_id: int, question: str, text: str, *, sensitive: bool = False,
           verified: bool = True) -> int:
    with sessions() as db:
        db.info["user_id"] = user_id
        row = KnowledgeAnswer(user_id=user_id, question=question, question_key=question_key(question), answer=text,
                              verified=verified, sensitive=sensitive, category="general", answer_type="text")
        db.add(row)
        db.commit()
        return row.id


def standard_answers(sessions, user_id: int) -> dict[str, int]:
    return {"python": answer(sessions, user_id, PYTHON_QUESTION, "8"),
            "auth": answer(sessions, user_id, AUTH_QUESTION, "Yes", sensitive=True)}


def enqueue(sessions, user_id: int, job_id: int, mode: str | None = None) -> int:
    from app.services import application_execution

    with sessions() as db:
        db.info["user_id"] = user_id
        task = application_execution.enqueue(db, db.get(Job, job_id), execution_mode=mode)
        db.commit()
        return task.id


def task_of(sessions, job_id: int) -> ApplicationExecutionTask:
    with sessions() as db:
        task = db.scalar(select(ApplicationExecutionTask).where(ApplicationExecutionTask.job_id == job_id)
                         .order_by(ApplicationExecutionTask.id.desc()).limit(1))
        db.expunge(task)
        return task


def ctx(sessions, worker: str | None = None) -> sj.JobContext:
    return sj.JobContext(sessions=sessions, trigger="schedule", now=utcnow(),
                         worker_id=worker or f"test:{uuid.uuid4().hex[:6]}", interval_minutes=1)


async def drive_async(sessions, passes: int = 6) -> list[dict[str, Any]]:
    out = []
    for _ in range(passes):
        result = await sj.application_execution_job(ctx(sessions))
        out.append(result)
        if not result.get("claimed"):
            break
    return out


def drive(sessions, passes: int = 6) -> list[dict[str, Any]]:
    """Run the scheduler pass until nothing is claimed."""
    return asyncio.run(drive_async(sessions, passes))
