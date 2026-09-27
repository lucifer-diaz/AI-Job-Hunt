"""Output contracts: the typed JSON each machine-facing worker must return.

A prompt names its contract (`Prompt.output_schema`). The LLM gateway (app.services.llm) then, for every provider:

* sends the contract as a JSON Schema when the model is known to support structured output (otherwise JSON mode, or
  nothing), and records which enforcement the request had;
* validates every answer against the contract before a service sees it. Valid JSON is not enough: contracts forbid
  fields they do not define, so an answer in another worker's shape (a Tailor answer carrying `candidate_fit_score`)
  fails, and is classified as POSSIBLE_CROSS_WORKER_OUTPUT rather than quietly accepted;
* retries once with a short schema correction, then fails. Missing fields are never inferred.

Contracts are deliberately non-overlapping: the Ranker's fields (scores, matches, gaps) exist only on RankingResult,
the Tailor's (a patch of the source resume) only on TailoringResult, the Reviewer's (claims found, a recommendation)
only on ReviewResult. The system prompts list fields with `describe_fields`, generated from these models, so a
prompt and its contract cannot drift apart.

Everything here is provider-independent: pydantic models and plain JSON Schema, no SDK.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

# Routing signals a model may add for quality escalation (app.services.model_resolution.AMBIGUITY_FIELDS). They are
# read from the raw answer by the gateway and are never part of a worker's result.
SIGNAL_FIELDS = frozenset({"ambiguous", "needs_escalation", "fatal_requirement_uncertain"})
MATCH_STATUSES = ("MATCH", "PARTIAL_MATCH", "MISSING", "UNKNOWN")
REQUIREMENT_LEVELS = ("REQUIRED", "PREFERRED", "INFORMATIONAL", "UNKNOWN")
QUALIFICATION_RECOMMENDATIONS = ("QUALIFY", "ARCHIVE")
REVIEW_RECOMMENDATIONS = ("PASS", "RETAILOR", "SKIP")
APPLICATION_DECISIONS = ("ANSWER", "UNKNOWN", "NEEDS_INPUT", "SKIP_APPLICATION")
INBOX_CATEGORIES = ("RECRUITER_RESPONSE", "INTERVIEW_INVITATION", "ASSESSMENT", "REJECTION", "OFFER",
                    "REQUEST_FOR_INFORMATION", "AUTOMATED_CONFIRMATION", "UNKNOWN")
RECOMMENDATION_CATEGORIES = ("COST", "LATENCY", "CONTEXT", "MODEL", "PROMPT", "RELIABILITY", "ARCHITECTURE",
                             "EXTERNAL_API")
RECOMMENDATION_PRIORITIES = ("HIGH", "MEDIUM", "LOW")
ADVISOR_STAGES = ("RANKER", "TAILOR", "REVIEWER", "RESUME_PARSER", "APPLICATION", "FOLLOW_UP", "INBOX",
                  "COMPANY_INSIGHTS", "ADVISOR", "OTHER", "ALL")


def _ids(values: Any) -> Any:
    """Evidence ids arrive as 12, "12" or "E12"; anything else is left for validation to reject."""
    if not isinstance(values, list):
        return values
    out = []
    for value in values:
        if isinstance(value, str) and re.fullmatch(r"\s*[Ee]?\d+\s*", value):
            value = int(value.strip().lstrip("Ee"))
        out.append(value)
    return list(dict.fromkeys(out)) if all(isinstance(v, int) for v in out) else out


def _upper(value: Any) -> Any:
    return value.strip().upper().replace(" ", "_").replace("-", "_") if isinstance(value, str) else value


def _phrase(value: Any) -> Any:
    """A short free-text field as models actually send it: null or "N/A" is empty, a list of phrases is one phrase.
    Anything else is left for validation to reject."""
    if value is None:
        return ""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        value = "; ".join(v.strip() for v in value if v.strip())
    if isinstance(value, str) and value.strip().upper().strip(".") in ("N/A", "NA", "NONE", "-"):
        return ""
    return value


def _text_id(value: Any) -> Any:
    """An id copied from the input: 7 and "7" are the same id."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _clean_strings(values: Any) -> Any:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()] if isinstance(values, list) else values


EvidenceIds = Annotated[list[int], BeforeValidator(_ids), Field(default_factory=list, max_length=20)]
Score = Annotated[float, Field(ge=0, le=100)]
Confidence = Annotated[float, Field(ge=0, le=1)]
Phrases = Annotated[list[str], BeforeValidator(_clean_strings), Field(default_factory=list, max_length=40)]


class Contract(BaseModel):
    """A worker's output. Unknown fields are an error, never ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    worker: ClassVar[str] = ""


# ───────────── Ranker ─────────────

class RequirementAssessment(Contract):
    requirement: str = Field(min_length=1, max_length=300, description="the requirement as the posting states it")
    requirement_id: Annotated[str | None, BeforeValidator(_text_id)] = Field(
        default=None, max_length=60, description="its id from JOB REQUIREMENTS, or null")
    level: Annotated[Literal["REQUIRED", "PREFERRED", "INFORMATIONAL", "UNKNOWN"], BeforeValidator(_upper)] = Field(
        default="UNKNOWN", description="copied from JOB REQUIREMENTS, never reclassified")
    status: Annotated[Literal["MATCH", "PARTIAL_MATCH", "MISSING", "UNKNOWN"], BeforeValidator(_upper)] = Field(
        description="MATCH fully supported; PARTIAL_MATCH part supported; MISSING the facts show it is absent; "
                    "UNKNOWN the facts cannot tell")
    supported: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=400, description="what the candidate's facts do support")
    unsupported: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=400, description="what they do not support")
    evidence_ids: EvidenceIds = Field(description="VERIFIED EVIDENCE ids behind `supported`")
    fatal: bool = Field(default=False, description="true only for an explicit hard requirement that is MISSING")

    @model_validator(mode="after")
    def _only_missing_is_fatal(self) -> RequirementAssessment:
        """The rule the prompt states, applied by code: only a MISSING requirement can be fatal (unknown lowers
        confidence instead). A model's fatal flag on anything else would archive a job by itself."""
        if self.status != "MISSING":
            self.fatal = False
        return self


class RankingResult(Contract):
    worker: ClassVar[str] = "RANKER"

    # The application knows which job it asked about (app.services.ranker.validate_output fills a missing one); a
    # different id is still rejected there.
    job_id: int | None = Field(default=None, description="the JOB ID given in the input")
    overall_fit_score: Score
    confidence: Confidence = Field(description="how complete the posting and the candidate's facts are")
    candidate_fit_score: Score = Field(description="does the person fit, from all their facts")
    resume_fit_score: Score = Field(description="does the ACTIVE RESUME, as written, show that fit")
    seniority_match_score: Score
    skills_match_score: Score
    leadership_match_score: Score
    domain_match_score: Score
    location_match_score: Score = Field(description="location, remote policy and work authorization")
    requirement_assessments: list[RequirementAssessment] = Field(
        default_factory=list, max_length=60, description="one entry per requirement you assessed")
    strong_matches: Phrases = Field(description="derived from MATCH assessments when omitted")
    partial_matches: Phrases = Field(description="derived from PARTIAL_MATCH assessments when omitted")
    missing_requirements: Phrases = Field(description="derived from MISSING assessments when omitted")
    fatal_gaps: Phrases = Field(description="explicit hard requirements the facts show are not met")
    resume_gaps: Phrases = Field(description="supported facts the resume does not show")
    relevant_evidence_ids: EvidenceIds = Field(description="only ids that appear in VERIFIED EVIDENCE")
    recommended_resume_focus: Phrases = Field(description="what the resume should lead with")
    qualification_recommendation: Annotated[str, BeforeValidator(_upper)] = Field(
        description=f"one of {', '.join(QUALIFICATION_RECOMMENDATIONS)}")
    reasoning_summary: str = Field(min_length=1, max_length=2000, description="two or three sentences")

    @field_validator("qualification_recommendation")
    @classmethod
    def _recommendation(cls, value: str) -> str:
        if value not in QUALIFICATION_RECOMMENDATIONS:
            raise ValueError(f"must be one of {', '.join(QUALIFICATION_RECOMMENDATIONS)}")
        return value

    @model_validator(mode="after")
    def _derive_lists(self) -> RankingResult:
        """The match lists are a projection of the assessments. A list the model already wrote is kept."""
        by_status: dict[str, list[RequirementAssessment]] = {s: [] for s in MATCH_STATUSES}
        for item in self.requirement_assessments:
            by_status[item.status].append(item)
        if not self.strong_matches:
            self.strong_matches = [a.requirement for a in by_status["MATCH"]]
        if not self.partial_matches:
            self.partial_matches = [
                "; ".join(filter(None, [a.requirement, f"supported: {a.supported}" if a.supported else "",
                                        f"not shown: {a.unsupported}" if a.unsupported else ""]))
                for a in by_status["PARTIAL_MATCH"]]
        if not self.missing_requirements:
            self.missing_requirements = [a.requirement for a in by_status["MISSING"] if not a.fatal]
        if self.requirement_assessments or not self.fatal_gaps:
            # Fatal gaps decide archiving (app.services.ranker.qualify), so with assessments they are what the
            # assessments show — MISSING and fatal — never a list the model wrote alongside them.
            self.fatal_gaps = [a.requirement for a in by_status["MISSING"] if a.fatal]
        return self


# ───────────── Tailor ─────────────

class Claim(Contract):
    """One generated factual unit and the evidence that supports it."""

    text: str = Field(min_length=1, max_length=600)
    evidence_ids: EvidenceIds = Field(description="ids of the source bullets or RELEVANT EVIDENCE it rests on")


class RoleUpdate(Contract):
    role_index: int = Field(ge=0, description="a role_index from SOURCE RESUME")
    bullets: list[Claim] = Field(min_length=1, max_length=8, description="1-5 bullets, most relevant first")


class TailoringResult(Contract):
    """A patch applied to the source resume by a deterministic renderer; never a whole resume."""

    worker: ClassVar[str] = "TAILOR"

    summary: Claim | None = Field(default=None, description="null keeps the source summary")
    skills_order: Phrases = Field(description="skills copied from SOURCE RESUME, most relevant first")
    role_updates: list[RoleUpdate] = Field(default_factory=list, max_length=20,
                                           description="rewritten bullets for SOURCE RESUME roles")
    highlights: list[Claim] = Field(default_factory=list, max_length=30,
                                    description="only when SOURCE RESUME has no roles")
    selected_evidence_ids: EvidenceIds = Field(description="every evidence id used anywhere above")

    @model_validator(mode="after")
    def _not_empty(self) -> TailoringResult:
        if self.summary is None and not self.skills_order and not self.role_updates and not self.highlights:
            raise ValueError("a tailoring result must change at least one of summary, skills_order, role_updates "
                             "or highlights")
        return self

    def cited_ids(self) -> set[int]:
        units = ([self.summary] if self.summary else []) + self.highlights + [
            b for role in self.role_updates for b in role.bullets]
        return {i for unit in units for i in unit.evidence_ids} | set(self.selected_evidence_ids)


# ───────────── Reviewer ─────────────

class ReviewResult(Contract):
    worker: ClassVar[str] = "REVIEWER"

    review_score: Score
    confidence: Confidence
    unsupported_claims: Phrases = Field(description="claims no VERIFIED fact or evidence supports")
    overstated_claims: Phrases = Field(description="claims that go beyond what their evidence says")
    unaddressed_requirements: Phrases = Field(description="REQUIRED requirements the resume does not address")
    risks: Phrases = Field(description="policy, consistency or presentation concerns")
    strengths: Phrases = Field(description="what the resume does well for this job")
    recommendation: Annotated[Literal["PASS", "RETAILOR", "SKIP"], BeforeValidator(_upper)] = Field(
        description="advice only: the application decides the job's state")
    summary: str = Field(default="", max_length=2000, description="one or two sentences")


# ───────────── writing workers ─────────────

class CoverLetterResult(Contract):
    worker: ClassVar[str] = "COVER_LETTER"

    cover_letter: str = Field(min_length=1, max_length=8000, description='the whole letter, "\\n\\n" between paragraphs')
    selected_evidence_ids: EvidenceIds = Field(description="the VERIFIED EVIDENCE ids the letter relies on")


class FollowUpResult(Contract):
    worker: ClassVar[str] = "FOLLOWUP"

    draft: str = Field(min_length=1, max_length=4000, description="the message, ready for the candidate to edit")
    evidence_ids: EvidenceIds = Field(description="VERIFIED EVIDENCE ids the draft mentions, if any")


class ApplicationAnswerResult(Contract):
    worker: ClassVar[str] = "APPLICATION_ANSWER"

    decision: Annotated[Literal["ANSWER", "UNKNOWN", "NEEDS_INPUT", "SKIP_APPLICATION"], BeforeValidator(_upper)] = \
        Field(description=f"one of {', '.join(APPLICATION_DECISIONS)}")
    answer: str | None = Field(default=None, max_length=4000, description="null unless decision is ANSWER")
    sources: Annotated[list[Literal["profile", "policy", "knowledge", "resume", "evidence"]],
                       BeforeValidator(lambda v: [s.strip().lower() for s in v] if isinstance(v, list) else v)] = \
        Field(default_factory=list, description="which of profile, policy, knowledge, resume, evidence it came from")
    evidence_ids: EvidenceIds
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _answer_only_when_answering(self) -> ApplicationAnswerResult:
        if self.decision == "ANSWER" and not (self.answer or "").strip():
            raise ValueError("decision ANSWER needs an answer")
        if self.decision != "ANSWER" and self.answer:
            raise ValueError("answer must be null unless decision is ANSWER")
        return self


class InboxClassificationResult(Contract):
    worker: ClassVar[str] = "INBOX_CLASSIFIER"

    category: Annotated[str, BeforeValidator(_upper)] = Field(description="one of the categories above")
    confidence: Confidence
    application_id: int | None = Field(default=None, description="a KNOWN APPLICATIONS id, or null")
    company: str | None = Field(default=None, max_length=300)
    role: str | None = Field(default=None, max_length=300)
    summary: str = Field(min_length=1, max_length=500, description="one sentence")
    action_required: bool
    deadline: str | None = Field(default=None, max_length=40, description="ISO 8601 date or datetime, or null")

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value not in INBOX_CATEGORIES:
            raise ValueError(f"must be one of {', '.join(INBOX_CATEGORIES)}")
        return value


# ───────────── extraction workers ─────────────

class ResumeParserOutput(Contract):
    worker: ClassVar[str] = "RESUME_PARSER"

    structured_resume: dict[str, Any]  # validated in depth by app.services.structured_resume.from_parser_output


class ExtractedClaim(Contract):
    claim: str = Field(min_length=1, max_length=2000, description="the achievement, as the resume states it")
    company: str = Field(default="", max_length=200, description="the employer the resume places it under")
    role: str = Field(default="", max_length=200, description="the job title it sits under")
    skills: Phrases = Field(description="skills the claim itself names")


class EvidenceExtractionResult(Contract):
    worker: ClassVar[str] = "EVIDENCE_EXTRACTION"

    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=80)


class ParsedRequirement(Contract):
    """One requirement in the shape the prompt shows (REQUIREMENT_TEMPLATE). Typed here, not left as a dict, so that
    an answer like {"skill": "..."} fails the contract and gets the gateway's schema correction instead of passing
    through to the service and failing there on every attempt. Unknown type and level values are normalised by the
    service (app.services.job_description.RequirementIn), so a model is not failed over vocabulary."""

    id: str | None = Field(default=None, max_length=60)
    type: Annotated[str, BeforeValidator(_upper)] = Field(default="OTHER", max_length=25)
    normalized_value: str = Field(min_length=1, max_length=300, description="the requirement in a few words")
    requirement_level: Annotated[str, BeforeValidator(_upper)] = Field(default="UNKNOWN", max_length=15)
    source_text: str = Field(default="", max_length=4000)
    source_section: str | None = Field(default=None, max_length=120)
    confidence: Confidence = 0.5
    is_explicit: bool = True
    is_derived: bool = False


class JobDescriptionParserOutput(Contract):
    worker: ClassVar[str] = "JD_PARSER"

    # The structured document is validated in depth (and grounded in the posting's own text) by
    # app.services.job_description.validated/ground.
    structured_description: dict[str, Any]
    requirements: list[ParsedRequirement]  # no length cap: the service keeps the first 200 of a long list


# ───────────── AI Optimization Advisor ─────────────

class Recommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=300)
    category: Annotated[str, BeforeValidator(_upper)]
    priority: Annotated[str, BeforeValidator(_upper)]
    finding: str
    evidence: str
    recommended_change: str
    expected_benefit: str
    estimated_cost_saving: str
    risk: str
    affected_stage: Annotated[str, BeforeValidator(_upper)]

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value not in RECOMMENDATION_CATEGORIES:
            raise ValueError(f"must be one of {', '.join(RECOMMENDATION_CATEGORIES)}")
        return value

    @field_validator("priority")
    @classmethod
    def _priority(cls, value: str) -> str:
        if value not in RECOMMENDATION_PRIORITIES:
            raise ValueError(f"must be one of {', '.join(RECOMMENDATION_PRIORITIES)}")
        return value

    @field_validator("affected_stage")
    @classmethod
    def _stage(cls, value: str) -> str:
        if value not in ADVISOR_STAGES:
            raise ValueError(f"must be one of {', '.join(ADVISOR_STAGES)}")
        return value


class OptimizationReport(Contract):
    worker: ClassVar[str] = "AI_OPTIMIZATION"

    summary: str = Field(min_length=1)
    estimated_savings: str = "unknown"
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=10)


CONTRACTS: tuple[type[Contract], ...] = (
    RankingResult, TailoringResult, ReviewResult, CoverLetterResult, FollowUpResult, ApplicationAnswerResult,
    InboxClassificationResult, ResumeParserOutput, EvidenceExtractionResult, JobDescriptionParserOutput,
    OptimizationReport)
# Field names older prompts (or a confused model) use for another worker's output, beyond the contracts' own fields.
LEGACY_FIELDS = {
    "RANKER": {"score", "fit_score", "gaps", "must_have_missing", "keywords", "application_strategy", "matches"},
    "TAILOR": {"experience", "experience_bullets", "skills", "experience_sections", "resume_bullets", "bullets"},
    "REVIEWER": {"reject_reasons", "improvements"},
}
# Fields too generic to point at one worker (several contracts use them).
_SHARED = {"summary", "confidence", "selected_evidence_ids", "evidence_ids", "missing_requirements"}


def owned_fields() -> dict[str, set[str]]:
    """Each worker's distinctive output fields."""
    fields: dict[str, set[str]] = {}
    for contract in CONTRACTS:
        fields.setdefault(contract.worker, set()).update(contract.model_fields)
    for worker, legacy in LEGACY_FIELDS.items():
        fields.setdefault(worker, set()).update(legacy)
    return {worker: names - _SHARED for worker, names in fields.items()}


def diagnose(contract: type[Contract], data: Any) -> tuple[str, str | None]:
    """Why an answer failed its contract: (WRONG_OUTPUT_SCHEMA | POSSIBLE_CROSS_WORKER_OUTPUT, the likely worker)."""
    if not isinstance(data, dict):
        return "WRONG_OUTPUT_SCHEMA", None
    extras = set(data) - set(contract.model_fields) - SIGNAL_FIELDS
    best, overlap = None, 0
    for worker, names in owned_fields().items():
        if worker == contract.worker:
            continue
        hits = len(extras & names)
        if hits > overlap:
            best, overlap = worker, hits
    return ("POSSIBLE_CROSS_WORKER_OUTPUT", best) if best else ("WRONG_OUTPUT_SCHEMA", None)


# ───────────── rendering the contract for prompts and providers ─────────────

def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _resolve(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        return {k: _resolve(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_resolve(v, defs) for v in node]
    return node


def resolved_schema(contract: type[BaseModel]) -> dict[str, Any]:
    schema = contract.model_json_schema()
    return _resolve(schema, schema.get("$defs", {}))


def _type_label(node: dict[str, Any]) -> str:
    if "enum" in node:
        return "one of " + ", ".join(str(v) for v in node["enum"])
    if "anyOf" in node:
        options = [o for o in node["anyOf"] if o.get("type") != "null"]
        nullable = len(options) < len(node["anyOf"])
        label = " or ".join(_type_label(o) for o in options)
        return f"{label} or null" if nullable else label
    kind = node.get("type")
    if kind == "array":
        item = _type_label(node.get("items") or {})
        return f"array of {item}s" if item in ("string", "int") else f"array of {item}"
    if kind == "object":
        props = node.get("properties")
        return "{" + ", ".join(props) + "}" if props else "object"
    if kind == "number" and "minimum" in node and "maximum" in node:
        return f"number {node['minimum']:g}-{node['maximum']:g}"
    return {"integer": "int", "string": "string", "boolean": "boolean", "number": "number"}.get(kind, "value")


def describe_fields(contract: type[BaseModel], *, skip: tuple[str, ...] = ()) -> str:
    """The field list for a system prompt, generated from the contract so the two always agree."""
    schema = resolved_schema(contract)
    lines = []
    for name, node in schema.get("properties", {}).items():
        if name in skip:
            continue
        description = node.get("description") or ""
        lines.append(f"  {name} ({_type_label(node)})" + (f": {description}" if description else ""))
    return "Fields:\n" + ",\n".join(lines) + "."


_KEEP = {"type", "properties", "items", "enum", "required", "anyOf", "additionalProperties"}


def _strict(node: Any) -> tuple[Any, bool]:
    """A copy limited to widely supported keywords, every object closed and fully required. The second value is False
    when some object is free-form (a provider's strict mode cannot express it)."""
    if isinstance(node, list):
        parts = [_strict(v) for v in node]
        return [p for p, _ in parts], all(ok for _, ok in parts)
    if not isinstance(node, dict):
        return node, True
    out, ok = {}, True
    for key, value in node.items():
        if key not in _KEEP or key == "additionalProperties":
            continue
        if key == "properties":
            parts = {name: _strict(child) for name, child in value.items()}
            out[key] = {name: part for name, (part, _) in parts.items()}
            ok = ok and all(child_ok for _, child_ok in parts.values())
        else:
            out[key], child_ok = _strict(value)
            ok = ok and child_ok
    if out.get("type") == "object":
        if "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        else:
            ok = False
    return out, ok


def response_format(contract: type[BaseModel]) -> dict[str, Any]:
    """An OpenAI-style `json_schema` response format (LiteLLM translates it for other providers)."""
    schema, strict = _strict(resolved_schema(contract))
    return {"type": "json_schema", "json_schema": {"name": contract.__name__, "schema": schema, "strict": strict}}
