"""Google ADK multi-agent definition discovered as ``job_hunt_agent``."""

from google.adk.agents import Agent

from app.settings import get_settings
from job_hunt_agent import prompts
from job_hunt_agent.tools import (
    get_application_context,
    get_job_context,
    get_resume_review_context,
    save_fit_analysis,
    save_follow_up_draft,
    save_resume_review,
    save_tailored_resume,
)

MODEL = get_settings().gemini_agent_model

job_fit_ranker = Agent(name="job_fit_ranker", model=MODEL,
    description="Scores one stored job against verified candidate evidence.",
    instruction=prompts.RANKER_INSTRUCTION, tools=[get_job_context, save_fit_analysis])

resume_tailor = Agent(name="resume_tailor", model=MODEL,
    description="Creates an evidence-grounded resume and cover-letter draft for a ranked job.",
    instruction=prompts.TAILOR_INSTRUCTION, tools=[get_job_context, save_tailored_resume])

resume_reviewer = Agent(name="resume_reviewer", model=MODEL,
    description="Independently reviews one tailored resume against its job.",
    instruction=prompts.REVIEWER_INSTRUCTION,
    tools=[get_resume_review_context, save_resume_review])

follow_up_writer = Agent(name="follow_up_writer", model=MODEL,
    description="Drafts, but never sends, one follow-up for an applied application.",
    instruction=prompts.FOLLOW_UP_INSTRUCTION,
    tools=[get_application_context, save_follow_up_draft])

root_agent = Agent(name="job_hunt_coordinator", model=MODEL,
    description="Coordinates the human-in-the-loop Job Hunt OS specialist agents.",
    instruction=prompts.ROOT_AGENT_INSTRUCTION,
    sub_agents=[job_fit_ranker, resume_tailor, resume_reviewer, follow_up_writer])
