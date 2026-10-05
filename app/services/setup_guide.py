"""The interactive setup guide ("Setup quest"): what an account still has to do before the hunt runs by itself.

Every quest is checked against the account's real data, deterministically, so the guide can never be ticked off by
clicking through it. XP and levels are presentation only; they unlock nothing and change no workflow.

The guide's own state (welcome screen seen, tracker hidden, which completed quests were already celebrated) lives in
`AppSetting.preferences["setup_guide"]`, so a quest is celebrated once per account, on any device.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CandidateProfile,
    Evidence,
    GmailConnection,
    Job,
    KnowledgeAnswer,
    Resume,
    ResumeDocument,
    SavedSearch,
    SearchRun,
)
from app.services.user_settings import ai_ready, ai_overrides, settings_for, user_keys
from app.settings import get_settings

PREFERENCE_KEY = "setup_guide"

# (minimum XP, title). Titles are flavour text for the guide's header.
LEVELS = [(0, "Rookie"), (150, "Job Seeker"), (400, "Pathfinder"), (650, "Hunter"), (850, "Offer Magnet")]


@dataclass(frozen=True)
class Quest:
    id: str
    chapter: str
    title: str
    summary: str
    why: str
    steps: tuple[str, ...]
    xp: int
    # Where the work is done: a workspace screen and, for Settings, Discovery and Knowledge, its section.
    screen: str
    section: str = ""
    optional: bool = False
    # A recommended route, shown as a callout above the steps; commands to copy; reference links.
    recommended: str = ""
    snippet: str = ""
    links: tuple[tuple[str, str], ...] = ()
    check: Callable[[Session, int, CandidateProfile | None], tuple[bool, str]] = field(default=lambda *_: (False, ""))


def _count(db: Session, stmt) -> int:
    return db.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def _profile(db: Session, user_id: int) -> CandidateProfile | None:
    return db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))


def _check_profile(db, user_id, p):
    missing = [label for label, value in (("name", p and p.name), ("headline", p and p.headline)) if not (value or "").strip()]
    return (not missing, "Name and headline saved" if not missing else f"Add your {' and '.join(missing)}")


def _check_preferences(db, user_id, p):
    roles, places = (p.target_roles or []) if p else [], (p.preferred_locations or []) if p else []
    if roles and places:
        return True, f"{len(roles)} role{'s' if len(roles) != 1 else ''} · {len(places)} location{'s' if len(places) != 1 else ''}"
    return False, "Add at least one target role and one location"


def _check_resume(db, user_id, p):
    files = _count(db, select(ResumeDocument.id).where(ResumeDocument.user_id == user_id))
    if files:
        return True, f"{files} resume file{'s' if files != 1 else ''} uploaded"
    if p and (p.master_resume or "").strip():
        return True, "Master resume text saved"
    return False, "Upload a PDF, DOCX or TXT resume"


def _check_verified(db, user_id, p):
    resumes = _count(db, select(Resume.id).where(Resume.user_id == user_id,
                                                 Resume.verification_status.in_(("VERIFIED", "PARTIALLY_VERIFIED"))))
    facts = _count(db, select(Evidence.id).where(Evidence.user_id == user_id, Evidence.verified.is_(True)))
    if resumes or facts:
        return True, f"{facts} verified fact{'s' if facts != 1 else ''}"
    return False, "Verify the facts AI may use from your resume"


def _check_ai(db, user_id, p):
    from app.services.ai_config import resolve as resolve_ai_config

    provider = resolve_ai_config(get_settings(), ai_overrides(db, user_id))["llm_provider"]
    if provider == "mock":
        return False, "Using the mock model: no real AI yet"
    ready, why = ai_ready(db, user_id)
    if not ready:
        return False, why
    return True, ("LiteLLM gateway connected" if provider == "litellm"
                  else f"{provider} connected · LiteLLM + Ollama Cloud is recommended")


def _check_apify(db, user_id, p):
    return (True, "Apify token saved") if user_keys(db, user_id).get("apify") else (False, "Save your Apify token")


def _check_search(db, user_id, p):
    n = _count(db, select(SavedSearch.id).where(SavedSearch.user_id == user_id))
    return (True, f"{n} search setup{'s' if n != 1 else ''}") if n else (False, "Save a search setup")


def _check_run(db, user_id, p):
    n = _count(db, select(SearchRun.id).where(SearchRun.user_id == user_id))
    return (True, f"{n} run{'s' if n != 1 else ''} so far") if n else (False, "Run a search")


def _check_triage(db, user_id, p):
    n = _count(db, select(Job.id).where(Job.user_id == user_id, Job.triaged_at.is_not(None)))
    return (True, f"{n} job{'s' if n != 1 else ''} triaged") if n else (False, "Shortlist, maybe or skip a job")


def _check_answers(db, user_id, p):
    n = _count(db, select(KnowledgeAnswer.id).where(KnowledgeAnswer.user_id == user_id, KnowledgeAnswer.verified.is_(True)))
    return (True, f"{n} verified answer{'s' if n != 1 else ''}") if n else (False, "Verify one reusable ATS answer")


def _check_gmail(db, user_id, p):
    if not get_settings().gmail_oauth_configured:
        return False, "Not available on this server (Google OAuth is not configured)"
    conn = db.scalar(select(GmailConnection).where(GmailConnection.user_id == user_id))
    if conn and conn.status == "CONNECTED":
        return True, f"Connected{f' as {conn.email_address}' if conn.email_address else ''}"
    return False, "Connect Gmail, read-only"


QUESTS: tuple[Quest, ...] = (
    Quest("profile", "Who you are", "Introduce yourself",
          "Your name and a one-line headline.",
          "Every resume, cover letter and answer starts from who you are.",
          ("Open Settings → Profile.", "Fill in your name and a headline such as “Senior Backend Engineer”.", "Save."),
          50, "settings", "profile", check=_check_profile),
    Quest("preferences", "Who you are", "Pick your target",
          "The roles and places you want next.",
          "Target roles and locations seed every search and tell the ranker what a good fit is.",
          ("Open Settings → Job preferences.", "Add up to five target roles.", "Add the cities you'd work in, or Remote.", "Save."),
          50, "settings", "preferences", check=_check_preferences),
    Quest("resume", "Your story", "Upload your resume",
          "The experience behind every application.",
          "Tailoring rewrites your real resume; it never invents one.",
          ("Open Settings → Resumes.", "Upload a PDF, DOCX or TXT file.", "Check the extracted text looks right."),
          100, "settings", "resumes", check=_check_resume),
    Quest("verify", "Your story", "Verify your facts",
          "Confirm what the AI is allowed to say about you.",
          "Only verified facts reach the Tailor, so nothing on a resume is something you didn't do.",
          ("Open Settings → Resume Data.", "Read each parsed role, skill and achievement.", "Verify the ones that are true; fix or reject the rest."),
          100, "settings", "resume-data", check=_check_verified),
    Quest("ai", "Power up", "Connect an AI model",
          "Recommended: Ollama Cloud through a local LiteLLM gateway.",
          "Ranking, tailoring and review need a real model. One Ollama account behind LiteLLM covers most of them: "
          "nemotron-3-nano:30b-cloud runs in Ollama Cloud, so you need no GPU, and LiteLLM lets you change models "
          "later in AI Orchestration → Models without touching code.",
          ("Sign in at ollama.com and create an API key under Settings → Keys.",
           "Install LiteLLM and start it with this repository's Ollama Cloud config (commands below).",
           "In AI Orchestration → Models & routing → LiteLLM connection, enter http://localhost:4000 and your LiteLLM "
           "master key, then press Test Connection.",
           "Press Sync models from LiteLLM, switch on Use LiteLLM gateway, and assign nemotron-3-nano:30b-cloud to "
           "every tier.",
           "Prefer a direct key? An OpenRouter, OpenAI, Anthropic or Gemini key in Settings → API keys also "
           "completes this quest."),
          100, "orchestration", "models", check=_check_ai,
          recommended="Ollama account + LiteLLM · nemotron-3-nano:30b-cloud",
          snippet=("export OLLAMA_API_KEY=...  # ollama.com/settings/keys\n"
                   "export LITELLM_MASTER_KEY=sk-jobhunt-local\n"
                   "pip install 'litellm[proxy]'\n"
                   "litellm --config integrations/litellm/config.ollama-cloud.yaml --port 4000"),
          links=(("Ollama API keys", "https://ollama.com/settings/keys"),
                 ("Nemotron 3 Nano on Ollama", "https://ollama.com/library/nemotron-3-nano"),
                 ("LiteLLM proxy docs", "https://docs.litellm.ai/docs/simple_proxy"))),
    Quest("apify", "Power up", "Connect job discovery",
          "An Apify token to search the job boards.",
          "Apify runs the scrapers that search LinkedIn, Indeed, Glassdoor and more for you.",
          ("Create a free account at apify.com.", "Copy your API token from Settings → Integrations there.",
           "Paste it into Settings → API keys → Apify here."),
          75, "settings", "api-keys", check=_check_apify),
    Quest("search", "The hunt", "Plan your first search",
          "Roles × locations × boards, on a schedule.",
          "A saved setup searches for you on its own schedule, so new jobs are waiting each morning.",
          ("Open Discovery → Search setup.", "Add roles and locations, pick boards.", "Choose a schedule and save."),
          100, "discovery", "setup", check=_check_search),
    Quest("run", "The hunt", "Launch the hunt",
          "Run a search and watch jobs arrive.",
          "The first run fills your pipeline; ranking starts on its own as jobs land.",
          ("Open Discovery → Search setup.", "Press Run on a saved setup.", "Watch the run history fill up."),
          75, "discovery", "setup", check=_check_run),
    Quest("triage", "The hunt", "Triage your first job",
          "Shortlist, maybe or skip.",
          "Your choices teach the domain filter what to keep, and shortlisted jobs move on to tailoring.",
          ("Open Discovery → Triage queue.", "Read the fit reasons.", "Press S to shortlist, M for maybe or X to skip."),
          75, "discovery", "triage", check=_check_triage),
    Quest("answers", "Ready to apply", "Stock your answer bank",
          "Answer an ATS question once, reuse it everywhere.",
          "Verified answers are pasted into every apply kit, so forms take seconds.",
          ("Open the Knowledge bank.", "Answer a common question such as notice period or visa status.", "Mark it verified."),
          75, "knowledge", "answers", check=_check_answers),
    Quest("gmail", "Bonus", "Watch your inbox",
          "Optional: read-only Gmail updates on your applications.",
          "Recruiter replies move your tracker along and cancel follow-ups you no longer need.",
          ("Open Settings → Gmail inbox.", "Press Connect Gmail and allow read-only access."),
          50, "settings", "gmail", optional=True, check=_check_gmail),
)
QUEST_IDS = {q.id for q in QUESTS}


def guide_state(db: Session, user_id: int) -> dict[str, Any]:
    raw = (settings_for(db, user_id).preferences or {}).get(PREFERENCE_KEY) or {}
    return {"dismissed": bool(raw.get("dismissed")), "welcomed": bool(raw.get("welcomed")),
            "celebrated": [q for q in raw.get("celebrated") or [] if q in QUEST_IDS]}


def save_state(db: Session, user_id: int, *, dismissed: bool | None = None, welcomed: bool | None = None,
               celebrated: list[str] | None = None) -> None:
    """Merge into the guide's saved state. Celebrated quests only accumulate; unknown ids are ignored."""
    row = settings_for(db, user_id)
    state = guide_state(db, user_id)
    if dismissed is not None:
        state["dismissed"] = dismissed
    if welcomed is not None:
        state["welcomed"] = welcomed
    if celebrated:
        state["celebrated"] = sorted(set(state["celebrated"]) | (set(celebrated) & QUEST_IDS))
    row.preferences = {**(row.preferences or {}), PREFERENCE_KEY: state}
    db.commit()


def level_for(xp: int) -> dict[str, Any]:
    index = max(i for i, (floor, _) in enumerate(LEVELS) if xp >= floor)
    floor, title = LEVELS[index]
    ceiling = LEVELS[index + 1][0] if index + 1 < len(LEVELS) else None
    return {"number": index + 1, "title": title, "floor": floor, "next": ceiling,
            "next_title": LEVELS[index + 1][1] if ceiling is not None else None}


def progress(db: Session, user_id: int) -> dict[str, Any]:
    profile = _profile(db, user_id)
    quests, xp = [], 0
    for q in QUESTS:
        try:
            done, detail = q.check(db, user_id, profile)
        except Exception:  # a broken check must never break the guide
            done, detail = False, ""
        xp += q.xp if done else 0
        quests.append({"id": q.id, "chapter": q.chapter, "title": q.title, "summary": q.summary, "why": q.why,
                       "steps": list(q.steps), "xp": q.xp, "screen": q.screen, "section": q.section,
                       "optional": q.optional, "recommended": q.recommended, "snippet": q.snippet,
                       "links": [{"label": label, "url": url} for label, url in q.links],
                       "done": done, "detail": detail})
    required = [q for q in quests if not q["optional"]]
    next_quest = next((q["id"] for q in quests if not q["done"] and not q["optional"]),
                      next((q["id"] for q in quests if not q["done"]), None))
    return {"quests": quests, "xp": xp, "xp_total": sum(q.xp for q in QUESTS), "level": level_for(xp),
            "done": sum(q["done"] for q in required), "total": len(required),
            "complete": all(q["done"] for q in required), "next": next_quest, **guide_state(db, user_id)}
