"""The Job Hunt OS MCP server: approved tools only, each run in a session bound to the token's account and audited."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from sqlalchemy.orm import Session

from app.mcp import tools
from app.models import CONVERSATION_ACTION_STATUSES
from app.prompts.mcp_application import build_mcp_application_prompt
from app.prompts.mcp_review import build_mcp_review_prompt
from app.prompts.mcp_tailoring import MCP_SERVER_INSTRUCTIONS, build_mcp_tailoring_prompt
from app.services import conversation_actions

log = logging.getLogger("jobhunt.mcp")

SessionFactory = Callable[[], Session]
# Every tool the server exposes. Anything else (SQL, files, shell, settings, keys) is deliberately absent.
TOOL_NAMES = ("list_jobs_for_tailoring", "get_tailoring_context", "save_tailored_resume", "save_bulk_tailored_resumes",
              "list_jobs_for_review", "get_review_context", "save_review", "save_bulk_reviews",
              "get_bulk_operation_status", "list_jobs_for_application", "get_job_packet",
              "search_application_knowledge", "request_human_answer", "record_application_result")

JobIds = Annotated[list[int], Field(description="Job IDs from list_jobs_for_tailoring or list_jobs_for_review")]


def run_tool(sessions: SessionFactory, user_id: int, name: str, handler: Callable[..., dict[str, Any]],
             arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one tool for the account, turn failures into safe messages, and record it in the audit trail."""
    with sessions() as db:
        db.info["user_id"] = user_id
        try:
            result = handler(db, **arguments)
        except tools.ToolError as exc:
            db.rollback()
            result = {"ok": False, "error": str(exc)}
        except Exception:
            db.rollback()
            log.exception("MCP tool %s failed", name)
            result = {"ok": False, "error": "Job Hunt OS could not complete this request; see the MCP server log"}
        try:
            items = result.get("results")
            job_id = arguments.get("job_id") or next(
                (a.get("job_id") for a in arguments.values() if isinstance(a, dict)), None)
            outcome = result.get("status")
            status = ("ERROR" if not result.get("ok") else outcome if outcome in CONVERSATION_ACTION_STATUSES
                      else "OK")
            summary = ({"saved": result.get("saved"), "rejected": result.get("rejected"),
                        "items": [{"job_id": r.get("job_id"), "status": r.get("status"),
                                   "decision": r.get("decision"),
                                   "codes": sorted({e["code"] for e in r.get("errors") or []})} for r in items]}
                       if items is not None else {"error": result["error"]} if not result.get("ok")
                       else {"outcome": outcome} if isinstance(outcome, str)
                       else {"count": result.get("count", len(result.get("jobs") or []))})
            conversation_actions.record(
                db, tool_name=name, arguments=arguments, result_status=status,
                entity_type=None if name in ("get_bulk_operation_status", "search_application_knowledge") else "job",
                entity_id=job_id if isinstance(job_id, int) else None,
                operation_id=result.get("operation_id") if items is not None else None, result_summary=summary)
        except Exception:  # auditing never hides the tool's answer
            db.rollback()
            log.exception("Could not record MCP action %s", name)
    return result


def build_server(user_id: int, sessions: SessionFactory | None = None) -> FastMCP:
    if sessions is None:
        from app.db import SessionLocal

        sessions = SessionLocal
    server = FastMCP("job-hunt-os", instructions=MCP_SERVER_INSTRUCTIONS)

    def call(name: str, **arguments: Any) -> dict[str, Any]:
        return run_tool(sessions, user_id, name, getattr(tools, name), arguments)

    @server.tool()
    def list_jobs_for_tailoring(
            minimum_fit_score: Annotated[float | None, Field(description="Only jobs with at least this fit score (0-100)")] = None,
            posted_after: Annotated[str | None, Field(description="ISO date, e.g. 2026-09-01")] = None,
            location: Annotated[str | None, Field(description="Substring of the job location")] = None,
            company: Annotated[str | None, Field(description="Substring of the company name")] = None,
            limit: Annotated[int, Field(description="Maximum jobs to return (default 10, capped by MCP_BULK_TAILOR_LIMIT)")] = 10,
    ) -> dict[str, Any]:
        """Jobs waiting for tailoring, highest fit score first, as compact metadata."""
        return call("list_jobs_for_tailoring", minimum_fit_score=minimum_fit_score, posted_after=posted_after,
                    location=location, company=company, limit=limit)

    @server.tool()
    def get_tailoring_context(job_ids: JobIds) -> dict[str, Any]:
        """Compact tailoring context: candidate profile, active resume, and per job the description, fit analysis,
        relevant verified evidence (with IDs), recommended focus and resume gaps."""
        return call("get_tailoring_context", job_ids=job_ids)

    @server.tool()
    def save_tailored_resume(
            result: Annotated[dict[str, Any], Field(description="One tailored result (see save_bulk_tailored_resumes)")],
    ) -> dict[str, Any]:
        """Validate and save one tailored resume as a new version. The master resume is never changed."""
        return call("save_tailored_resume", result=result)

    @server.tool()
    def save_bulk_tailored_resumes(
            results: Annotated[list[dict[str, Any]], Field(description=(
                "Tailored results: {job_id, summary, skills_order, experience_sections: [{role_index, bullets: "
                "[{text, evidence_ids}]}], resume_bullets: [{text, evidence_ids}], cover_letter, "
                "screening_answer_drafts}"))],
    ) -> dict[str, Any]:
        """Validate and save tailored resumes. Each item is saved or rejected with validation errors on its own."""
        return call("save_bulk_tailored_resumes", results=results)

    @server.tool()
    def list_jobs_for_review(
            minimum_fit_score: Annotated[float | None, Field(description="Only jobs with at least this fit score")] = None,
            limit: Annotated[int, Field(description="Maximum jobs (default 20, capped by MCP_BULK_REVIEW_LIMIT)")] = 20,
    ) -> dict[str, Any]:
        """Tailored applications whose latest version has not been reviewed yet."""
        return call("list_jobs_for_review", minimum_fit_score=minimum_fit_score, limit=limit)

    @server.tool()
    def get_review_context(job_ids: JobIds) -> dict[str, Any]:
        """Compact review context: job description, candidate profile and policy, verified evidence, fit analysis
        and the tailored resume."""
        return call("get_review_context", job_ids=job_ids)

    @server.tool()
    def save_review(
            review: Annotated[dict[str, Any], Field(description="One review (see save_bulk_reviews)")],
    ) -> dict[str, Any]:
        """Validate and save one review. Job Hunt OS decides the resulting state."""
        return call("save_review", review=review)

    @server.tool()
    def save_bulk_reviews(
            reviews: Annotated[list[dict[str, Any]], Field(description=(
                "Reviews: {job_id, tailored_resume_version, review_score 0-100, confidence 0-1, unsupported_claims, "
                "missing_requirements, overstated_claims, risks, recommendation: PASS | RETAILOR | SKIP}"))],
    ) -> dict[str, Any]:
        """Validate and save reviews. Job Hunt OS re-runs its evidence checks and decides each job's state."""
        return call("save_bulk_reviews", reviews=reviews)

    @server.tool()
    def get_bulk_operation_status(
            operation_id: Annotated[str | None, Field(description="An operation_id from a bulk save; omit for the latest")] = None,
    ) -> dict[str, Any]:
        """What a bulk save stored or rejected, with each job's current status."""
        return call("get_bulk_operation_status", operation_id=operation_id)

    @server.tool()
    def list_jobs_for_application(
            reviewer_score_above: Annotated[float | None, Field(description=(
                "Only packets whose approved resume the reviewer scored above this (default and minimum 60)"))] = None,
            company: Annotated[str | None, Field(description="Substring of the company name")] = None,
            include_blocked: Annotated[bool, Field(description=(
                "Also list jobs whose last attempt stopped at a blocker that is still open"))] = False,
            offset: Annotated[int, Field(description="Jobs to skip: next_offset from the previous page")] = 0,
            limit: Annotated[int, Field(description="Page size (default 20, capped by MCP_APPLICATION_PAGE_LIMIT)")] = 20,
    ) -> dict[str, Any]:
        """Approved jobs not applied to yet whose approved resume scored above 60 in review, best reviewed first,
        with each job's application URL. Jobs already applied to under another posting are left out."""
        return call("list_jobs_for_application", reviewer_score_above=reviewer_score_above, company=company,
                    include_blocked=include_blocked, offset=offset, limit=limit)

    @server.tool()
    def get_job_packet(
            job_id: Annotated[int, Field(description="A job_id from list_jobs_for_application")],
            formats: Annotated[list[str] | None, Field(description="Files to render: pdf, docx (default both)")] = None,
            include_content: Annotated[bool, Field(description=(
                "Also return each file base64-encoded (default false: use the local path to upload it)"))] = False,
    ) -> dict[str, Any]:
        """The approved resume version for a job, its cover letter, and the resume as uploadable PDF and DOCX files
        written to a local folder. Refused for a job that is not ready to apply to."""
        return call("get_job_packet", job_id=job_id, formats=formats, include_content=include_content)

    @server.tool()
    def search_application_knowledge(
            question: Annotated[str, Field(description="The application form question, as the form words it")],
            limit: Annotated[int, Field(description="Most sources of each kind to return (default 5, max 20)")] = 5,
    ) -> dict[str, Any]:
        """Search saved answers, the verified Evidence Bank and the profile for an ATS question. Returns sourced
        candidates and a status: ANSWERED, NEEDS_CONFIRMATION, PENDING_HUMAN, SUPPORTED or UNKNOWN."""
        return call("search_application_knowledge", question=question, limit=limit)

    @server.tool()
    def request_human_answer(
            job_id: Annotated[int, Field(description="The job whose application form asked the question")],
            question: Annotated[str, Field(description="The question exactly as the form words it")],
            answer_options: Annotated[list[str] | None, Field(description=(
                "The choices the form offers, in its order (omit for free text)"))] = None,
            answer_type: Annotated[str | None, Field(description="text, boolean, choice or number")] = None,
            category: Annotated[str, Field(description=(
                "general, work_authorization, compensation, notice_period, relocation or demographic"))] = "general",
            sensitive: Annotated[bool, Field(description="True for demographic, identity or legal questions")] = False,
            required: Annotated[bool | None, Field(description="Whether the form requires an answer")] = None,
            field_label: Annotated[str | None, Field(description="The form field's label, if it differs")] = None,
            note: Annotated[str | None, Field(description="Anything the candidate should know to answer it")] = None,
    ) -> dict[str, Any]:
        """Save a question nobody has answered yet for the candidate to answer in the Knowledge bank, with the job
        and the form's options. Returns the question_id to pass to record_application_result."""
        return call("request_human_answer", job_id=job_id, question=question, answer_options=answer_options,
                    answer_type=answer_type, category=category, sensitive=sensitive, required=required,
                    field_label=field_label, note=note)

    @server.tool()
    def record_application_result(
            job_id: Annotated[int, Field(description="The job applied to")],
            outcome: Annotated[str, Field(description="submitted (the portal confirmed it) or blocked")],
            confirmation_id: Annotated[str | None, Field(description="The reference the portal showed, if any")] = None,
            blocker_reason: Annotated[str | None, Field(description=(
                "For blocked: captcha, login_required, account_required, needs_human_answer, missing_document, "
                "posting_closed, form_error or other"))] = None,
            detail: Annotated[str | None, Field(description="What happened, in a sentence")] = None,
            question_ids: Annotated[list[int] | None, Field(description=(
                "For needs_human_answer: the question_ids from request_human_answer"))] = None,
    ) -> dict[str, Any]:
        """Record a confirmed submission (once: a job already applied to is never recorded again) or the blocker
        that stopped it."""
        return call("record_application_result", job_id=job_id, outcome=outcome, confirmation_id=confirmation_id,
                    blocker_reason=blocker_reason, detail=detail, question_ids=question_ids)

    @server.prompt()
    def tailor_jobs(minimum_fit_score: float | None = None, limit: int = 5) -> str:
        """Tailor the resume for the best jobs waiting for tailoring."""
        return build_mcp_tailoring_prompt(minimum_fit_score, limit)

    @server.prompt()
    def review_applications(minimum_fit_score: float | None = None, limit: int = 10) -> str:
        """Review tailored applications waiting for review."""
        return build_mcp_review_prompt(minimum_fit_score, limit)

    @server.prompt()
    def apply_to_jobs(limit: int = 5) -> str:
        """Apply to approved jobs with their approved packets, asking me what the Knowledge bank cannot answer."""
        return build_mcp_application_prompt(limit)

    return server
