"""Per-user data isolation, enforced for every database session that belongs to a user.

A session belongs to a user when `session.info["user_id"]` is set: app.auth.get_user_db does this for
every signed-in request, and the scheduler does it for the work it runs on a user's behalf. For such a
session:

* every ORM SELECT, UPDATE and DELETE only touches that user's rows, including child rows (tailored
  resumes, applications, application events, follow-ups, workflow history) that are owned via their job;
* `Session.get()` returns None for another user's row, even when it is already in the identity map
  (see app.db.AppSession);
* new rows of owned models are stamped with the user automatically.

Sessions without a user (migrations, the scheduler's system-wide passes, tests of internals) see all rows.
Because the filter lives here rather than in each endpoint, a query that forgets to filter by user
still cannot return another user's data.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session, with_loader_criteria

from app.models import (AppSetting, Application, ApplicationEvent, CandidateProfile, Evidence, FollowUp, Job,
                        KnowledgeAnswer, ResumeDocument, ResumeVariant, SearchRun, WorkflowTransition)

OWNED_MODELS = (CandidateProfile, AppSetting, ResumeDocument, KnowledgeAnswer, Evidence, Job, SearchRun)
JOB_CHILDREN = (ResumeVariant, Application, WorkflowTransition)
APPLICATION_CHILDREN = (ApplicationEvent, FollowUp)
# Core tables (not ORM entities) inside criteria, so the criteria never re-apply to their own subqueries.
_jobs = Job.__table__
_applications = Application.__table__


class NoUserContext(RuntimeError):
    """An operation that needs to know whose data it works on ran without a user."""


def require_user_id(db: Session) -> int:
    user_id = db.info.get("user_id")
    if user_id is None:
        raise NoUserContext("This operation needs a signed-in user or an explicit owner")
    return user_id


def owned_by(obj: Any, user_id: int) -> bool:
    if isinstance(obj, OWNED_MODELS):
        return obj.user_id == user_id
    if isinstance(obj, JOB_CHILDREN):
        return obj.job is not None and obj.job.user_id == user_id
    if isinstance(obj, APPLICATION_CHILDREN):
        return obj.application is not None and obj.application.job is not None and obj.application.job.user_id == user_id
    return True


@event.listens_for(Session, "do_orm_execute")
def _only_the_users_rows(state) -> None:
    user_id = state.session.info.get("user_id")
    if user_id is None or state.is_column_load or state.is_relationship_load:
        return  # relationship loads inherit the criteria of the statement that loaded their parent
    if not (state.is_select or state.is_update or state.is_delete):
        return
    state.statement = state.statement.options(
        *[with_loader_criteria(model, lambda cls: cls.user_id == user_id, include_aliases=True)
          for model in OWNED_MODELS],
        *[with_loader_criteria(model, lambda cls: cls.job_id.in_(
            select(_jobs.c.id).where(_jobs.c.user_id == user_id)), include_aliases=True)
          for model in JOB_CHILDREN],
        *[with_loader_criteria(model, lambda cls: cls.application_id.in_(
            select(_applications.c.id).join(_jobs, _jobs.c.id == _applications.c.job_id)
            .where(_jobs.c.user_id == user_id)), include_aliases=True)
          for model in APPLICATION_CHILDREN],
    )


@event.listens_for(Session, "before_flush")
def _stamp_owner(session: Session, _flush_context, _instances) -> None:
    user_id = session.info.get("user_id")
    if user_id is None:
        return
    for obj in session.new:
        if isinstance(obj, OWNED_MODELS) and obj.user_id is None:
            obj.user_id = user_id
