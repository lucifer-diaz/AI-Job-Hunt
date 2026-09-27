"""The Ranker's contract accepts what models harmlessly vary on, and enforces in code the rules the prompt states.

Each case is a shape that was rejected in production (Monitoring → LLM requests, ranker, 7 days)."""
import pytest
from pydantic import ValidationError

from app.prompts.contracts import RankingResult
from app.services import ranker
from app.services.llm import unwrap

SCORES = {name: 60 for name in ("overall_fit_score", "candidate_fit_score", "resume_fit_score", "seniority_match_score",
                                "skills_match_score", "leadership_match_score", "domain_match_score",
                                "location_match_score")}


def answer(**changes):
    base = {"job_id": 553, **SCORES, "confidence": 0.8, "qualification_recommendation": "QUALIFY",
            "reasoning_summary": "Strong platform background.", "requirement_assessments": [
                {"requirement": "Python", "requirement_id": "req_001", "level": "REQUIRED", "status": "MATCH",
                 "supported": "Built Django services", "evidence_ids": [1]}]}
    return {**base, **changes}


def assessment(**fields):
    return {"requirement": "PRDs", "requirement_id": "req_008", "level": "REQUIRED", "status": "UNKNOWN",
            "evidence_ids": [], **fields}


@pytest.mark.parametrize("value, expected", [(None, ""), ("N/A", ""), (["no PRDs", "no user stories"],
                                                                        "no PRDs; no user stories")])
def test_empty_or_listed_supported_and_unsupported_are_accepted(value, expected):
    """58 + 13 + 9 + 4 rejections: "Input should be a valid string" for supported / unsupported."""
    result = RankingResult.model_validate(answer(requirement_assessments=[assessment(supported=value,
                                                                                     unsupported=value)]))
    item = result.requirement_assessments[0]
    assert item.supported == expected and item.unsupported == expected


def test_a_numeric_requirement_id_is_the_same_id():
    result = RankingResult.model_validate(answer(requirement_assessments=[assessment(requirement_id=8)]))
    assert result.requirement_assessments[0].requirement_id == "8"


def test_a_missing_job_id_is_filled_in_but_a_different_one_is_still_refused():
    """32 rejections: "job_id: Field required". The application knows which job it asked about."""
    body = answer()
    del body["job_id"]
    output, _ = ranker.validate_output(body, 553, {1})
    assert output.job_id == 553
    with pytest.raises(ranker.RankingSchemaError, match="for job 1, not job 553"):
        ranker.validate_output(answer(job_id=1), 553, {1})


def test_unknown_is_never_fatal_and_only_missing_fatal_requirements_are_fatal_gaps():
    """The prompt's rule, applied by code: a model marking UNKNOWN requirements fatal archived jobs by itself."""
    result = RankingResult.model_validate(answer(
        requirement_assessments=[assessment(status="UNKNOWN", fatal=True),
                                 assessment(requirement="Work authorization in India", status="MISSING", fatal=True)],
        fatal_gaps=["PRDs", "Product discovery"]))
    assert [a.fatal for a in result.requirement_assessments] == [False, True]
    assert result.fatal_gaps == ["Work authorization in India"], "derived from the assessments, not the model's list"


def test_an_answer_wrapped_in_one_outer_key_is_unwrapped_only_when_the_inside_is_valid():
    """43 rejections had every field "required": the answer was not at the top level."""
    wrapped = {"fit_analysis": answer()}
    assert unwrap(RankingResult, wrapped) == answer()
    assert unwrap(RankingResult, answer()) == answer(), "a valid answer is left alone"
    broken = {"fit_analysis": {"score": 3}}
    assert unwrap(RankingResult, broken) is broken, "an invalid inside is not guessed at"
    with pytest.raises(ValidationError):
        RankingResult.model_validate(broken)


def test_an_invalid_recommendation_is_still_rejected():
    with pytest.raises(ValidationError):
        RankingResult.model_validate(answer(qualification_recommendation="MAYBE"))


def test_the_gateway_hands_the_unwrapped_answer_to_the_ranker(monkeypatch):
    from app.db import SessionLocal
    from app.services.llm import LLMCall, LLMClient
    from tests.conftest import make_user
    from tests.test_monitoring import ranking_prompt

    user_id = make_user(SessionLocal)
    real = LLMClient._mock

    def wrapped(self, task, user_text):
        out = real(self, task, user_text)
        return {"fit_analysis": out} if "FIT_ANALYSIS" in user_text.split("\n", 1)[0] else out
    monkeypatch.setattr(LLMClient, "_mock", wrapped)
    client = LLMClient(overrides={"llm_provider": "mock"}, keys={}, user_id=user_id)
    result = client.generate_prompt(ranking_prompt(7), LLMCall(validate=lambda data: ranker.validate_output(data, 7, set())))
    output, _ = result.validated
    assert output.job_id == 7 and "fit_analysis" not in result.data
