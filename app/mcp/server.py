"""The Job Hunt OS MCP server: approved tools only, each run in a session bound to the token's account and audited."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from sqlalchemy.orm import Session

from app.mcp import tools
from app.prompts.mcp_review import build_mcp_review_prompt
from app.prompts.mcp_tailoring import MCP_SERVER_INSTRUCTIONS, build_mcp_tailoring_prompt
from app.services import conversation_actions

log = logging.getLogger("jobhunt.mcp")

SessionFactory = Callable[[], Session]
# Every tool the server exposes. Anything else (SQL, files, shell, settings, keys) is deliberately absent.
TOOL_NAMES = ("list_jobs_for_tailoring", "get_tailoring_context", "save_tailored_resume", "save_bulk_tailored_resumes",
              "list_jobs_for_review", "get_review_context", "save_review", "save_bulk_reviews",
              "get_bulk_operation_status")

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
            status = ("ERROR" if not result.get("ok") else result.get("status", "OK"))
            summary = ({"saved": result.get("saved"), "rejected": result.get("rejected"),
                        "items": [{"job_id": r.get("job_id"), "status": r.get("status"),
                                   "decision": r.get("decision"),
                                   "codes": sorted({e["code"] for e in r.get("errors") or []})} for r in items]}
                       if items is not None else {"error": result["error"]} if not result.get("ok")
                       else {"count": result.get("count", len(result.get("jobs") or []))})
            conversation_actions.record(
                db, tool_name=name, arguments=arguments, result_status=status,
                entity_type=None if name == "get_bulk_operation_status" else "job",
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

    @server.prompt()
    def tailor_jobs(minimum_fit_score: float | None = None, limit: int = 5) -> str:
        """Tailor the resume for the best jobs waiting for tailoring."""
        return build_mcp_tailoring_prompt(minimum_fit_score, limit)

    @server.prompt()
    def review_applications(minimum_fit_score: float | None = None, limit: int = 10) -> str:
        """Review tailored applications waiting for review."""
        return build_mcp_review_prompt(minimum_fit_score, limit)

    return server
