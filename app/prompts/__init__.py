"""Every production LLM prompt lives in this package.

Each prompt module has a builder that returns a provider-independent `Prompt` (trusted instructions in
`system`, dynamic data in `user`) and a version constant that services store with the result where the
data model allows. Services must not define long prompts inline.
"""
from app.prompts.agents import AGENT_PROMPT_VERSION
from app.prompts.ai_optimization import AI_OPTIMIZATION_PROMPT_VERSION, build_ai_optimization_prompt
from app.prompts.application import (
                                       APPLICATION_QUESTION_PROMPT_VERSION,
                                       build_application_question_prompt,
)
from app.prompts.common import Prompt
from app.prompts.cover_letter import COVER_LETTER_PROMPT_VERSION, build_cover_letter_prompt
from app.prompts.followup import FOLLOWUP_PROMPT_VERSION, build_followup_prompt
from app.prompts.inbox import (
                                       INBOX_CATEGORIES,
                                       INBOX_PROMPT_VERSION,
                                       build_inbox_classification_prompt,
)
from app.prompts.mcp_review import MCP_REVIEW_PROMPT_VERSION
from app.prompts.mcp_tailoring import MCP_TAILORING_PROMPT_VERSION
from app.prompts.ranking import RANKING_PROMPT_VERSION, build_ranking_prompt
from app.prompts.resume_parser import (
                                       EVIDENCE_EXTRACTION_PROMPT_VERSION,
                                       RESUME_PARSER_PROMPT_VERSION,
                                       build_evidence_extraction_prompt,
                                       build_resume_parser_prompt,
)
from app.prompts.review import REVIEW_PROMPT_VERSION, build_review_prompt
from app.prompts.tailoring import TAILOR_PROMPT_VERSION, build_tailoring_prompt

PROMPT_VERSIONS = {
    "ranking": RANKING_PROMPT_VERSION,
    "tailoring": TAILOR_PROMPT_VERSION,
    "cover_letter": COVER_LETTER_PROMPT_VERSION,
    "review": REVIEW_PROMPT_VERSION,
    "followup": FOLLOWUP_PROMPT_VERSION,
    "resume_parser": RESUME_PARSER_PROMPT_VERSION,
    "evidence_extraction": EVIDENCE_EXTRACTION_PROMPT_VERSION,
    "application_question": APPLICATION_QUESTION_PROMPT_VERSION,
    "inbox": INBOX_PROMPT_VERSION,
    "agents": AGENT_PROMPT_VERSION,
    "ai_optimization": AI_OPTIMIZATION_PROMPT_VERSION,
    "mcp_tailoring": MCP_TAILORING_PROMPT_VERSION,
    "mcp_review": MCP_REVIEW_PROMPT_VERSION,
}

__all__ = [
                                       "build_ai_optimization_prompt",
                                       "INBOX_CATEGORIES",
                                       "PROMPT_VERSIONS",
                                       "Prompt",
                                       "build_application_question_prompt",
                                       "build_cover_letter_prompt",
                                       "build_evidence_extraction_prompt",
                                       "build_followup_prompt",
                                       "build_inbox_classification_prompt",
                                       "build_ranking_prompt",
                                       "build_resume_parser_prompt",
                                       "build_review_prompt",
                                       "build_tailoring_prompt",
]
