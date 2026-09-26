"""Golden fixtures per worker: the prompt carries the right facts, the contract accepts the right answer and rejects
wrong ones with the right diagnosis. Nothing here depends on exact prose."""
import pytest
from pydantic import ValidationError

from app.prompts import OUTPUT_CONTRACTS
from app.prompts.contracts import diagnose
from app.services.ai_workers import WORKERS, worker_for_prompt
from tests import golden

CASES = golden.names()


def test_every_worker_with_a_prompt_has_a_golden_fixture():
    covered = {golden.load(name)["worker"] for name in CASES}
    assert {"RESUME_PARSER", "JD_PARSER", "RANKER", "TAILOR", "REVIEWER", "APPLICATION_ANSWER", "INBOX_CLASSIFIER",
            "FOLLOWUP"} <= covered
    for name in CASES:
        fixture = golden.load(name)
        assert worker_for_prompt(fixture["prompt"]) == fixture["worker"] and fixture["worker"] in WORKERS, name


@pytest.mark.parametrize("name", CASES)
def test_the_prompt_carries_the_right_facts_and_nothing_it_should_not(name):
    fixture = golden.load(name)
    prompt = golden.build(fixture)
    text = f"{prompt.system}\n{prompt.user}"
    for needed in fixture["must_include"]:
        assert needed in text, (name, needed)
    for banned in fixture["must_exclude"]:
        assert banned not in prompt.user, (name, banned)
    assert prompt.output_schema is OUTPUT_CONTRACTS[fixture["prompt"]]


@pytest.mark.parametrize("name", CASES)
def test_the_valid_answer_passes_its_contract_and_the_truth_checks(name):
    fixture = golden.load(name)
    contract = OUTPUT_CONTRACTS[fixture["prompt"]]
    result = contract.model_validate(fixture["valid_output"])
    assert golden.prohibited_claims(fixture, fixture["valid_output"]) == []
    assert golden.cited_evidence(fixture["valid_output"]) <= golden.known_evidence()
    for field, expected in (fixture.get("expect") or {}).items():
        if field.endswith("_contain"):
            assert any(expected in item for item in getattr(result, field.removesuffix("_contain"))), field
        else:
            assert getattr(result, field) == expected, field


@pytest.mark.parametrize("name", CASES)
def test_wrong_answers_are_rejected_with_the_right_diagnosis(name):
    fixture = golden.load(name)
    contract = OUTPUT_CONTRACTS[fixture["prompt"]]
    for case in fixture["invalid_outputs"]:
        with pytest.raises(ValidationError):
            contract.model_validate(case["output"])
        assert diagnose(contract, case["output"])[0] == case["reason"], (name, case["name"])


def test_a_hallucinated_requirement_is_caught_by_the_prohibited_claims():
    tailor = golden.load("tailor")
    invented = {**tailor["valid_output"], "role_updates": [{"role_index": 0, "bullets": [
        {"text": "Owned AWS deployments and PHP services", "evidence_ids": [24]}]}]}
    assert golden.prohibited_claims(tailor, invented) == ["PHP", "owned AWS deployments"]


def test_the_evaluation_script_scores_every_case_through_the_real_gateway(monkeypatch):
    import importlib.util

    import app.main  # noqa: F401  migrates the test database, as every API test does
    from app.settings import get_settings
    from tests.conftest import ensure_test_user

    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    spec = importlib.util.spec_from_file_location("evaluate_prompts", golden.FIXTURES.parents[2] / "scripts" /
                                                  "evaluate_prompts.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    results = [script.run_case(ensure_test_user().id, name, None) for name in CASES]
    by_case = {r["case"]: r for r in results}
    for name in ("ranker", "tailor", "reviewer", "resume_parser", "jd_parser", "followup"):
        assert by_case[name]["schema_ok"], name
    assert by_case["tailor"]["prohibited_claims"] == [] and by_case["tailor"]["invalid_evidence"] == []
    # The mock has no inbox answer: the contract failure is measured and reported, not raised.
    assert by_case["inbox"] == {**by_case["inbox"], "schema_ok": False, "failure": "WRONG_OUTPUT_SCHEMA"}
    rows = script.summarise(results)["rows"]
    assert {row["worker"] for row in rows} >= {"RANKER", "TAILOR", "REVIEWER"}
