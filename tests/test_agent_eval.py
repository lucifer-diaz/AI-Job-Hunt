"""Phase 7 evaluation suites and review gates (app.services.agent_eval, scripts/agent_eval.py).

Every suite runs the production code over its hand-labelled dataset and must PASS its gates; a doctored case must turn
the gate BLOCKED, so a gate can never pass by accident. Nothing here calls a model or a hosted service.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.agent_eval import GATES, SUITES, dataset_for, evaluate, report, suites
from app.services.agent_eval.datasets import AGENT_EVAL, load

ROOT = Path(__file__).resolve().parents[1]
IN_PROCESS = ("jd_parser", "qualification", "browser_actions", "confirmation", "human_corrections")


@pytest.mark.parametrize("name", IN_PROCESS)
def test_each_suite_passes_its_gates_on_its_dataset(name):
    result = SUITES[name](dataset_for(name), {})
    gate = evaluate(result)
    assert gate["status"] == "PASS", (gate["reason"], result.failed_cases)
    assert result.dataset["version"] >= 1 and len(result.dataset["sha256"]) == 64


def test_every_suite_has_a_gate_and_a_versioned_dataset():
    assert set(GATES) == set(SUITES)
    for name in SUITES:
        ds = dataset_for(name)
        assert isinstance(ds.version, int) and ds.sha256
    for path in AGENT_EVAL.glob("*.json"):
        data = json.loads(path.read_text())
        assert data["name"] == path.stem and isinstance(data["version"], int) and data["examples"]


def _doctored(name, mutate):
    ds = copy.deepcopy(dataset_for(name))
    mutate(ds)
    return evaluate(SUITES[name](ds, {}))


def test_a_false_confirmation_blocks_the_gate():
    def flip(ds):  # label an ambiguous page "confirmed": the verifier disagrees, as it must
        next(ex for ex in ds.examples if ex.key == "form-still-on-page").outputs["confirmed"] = True
    gate = _doctored("confirmation", flip)
    assert gate["status"] == "BLOCKED" and "accuracy" in gate["reason"]


def test_an_unsafe_browser_action_blocks_the_gate(monkeypatch):
    from app.services import browser_actions

    original = browser_actions.check

    def lenient(proposal, *a, **k):  # a regression that lets a submit click through
        if proposal.action == "click_control" and proposal.element_key == "k4":
            return browser_actions.Step("click", {"key": "k4"})
        return original(proposal, *a, **k)
    monkeypatch.setattr(browser_actions, "check", lenient)
    gate = evaluate(SUITES["browser_actions"](dataset_for("browser_actions"), {}))
    assert gate["status"] == "BLOCKED"
    assert next(c for c in gate["checks"] if c["metric"] == "unsafe_accepted")["value"] == 1


def test_a_hidden_job_the_person_wants_blocks_the_gate():
    def relabel(ds):
        next(ex for ex in ds.examples if ex.key == "domain-foreign-skill").outputs = {"keep": True}
    gate = _doctored("qualification", relabel)
    assert gate["status"] == "BLOCKED"
    assert next(c for c in gate["checks"] if c["metric"] == "false_hide")["value"] == 1


def test_a_fabricated_employer_quote_blocks_the_jd_parser_gate(monkeypatch):
    from app.services import job_description

    monkeypatch.setattr(job_description, "ground", lambda parsed, posting: parsed)  # grounding switched off
    gate = evaluate(SUITES["jd_parser"](dataset_for("jd_parser"), {}))
    assert gate["status"] == "BLOCKED"
    assert next(c for c in gate["checks"] if c["metric"] == "fabricated_explicit")["value"] >= 1


def test_a_rejection_counted_as_a_correction_blocks_the_gate(monkeypatch):
    from app.services.agent_eval import feedback

    original = feedback.rates

    def blaming(facts):  # a regression that reads rejections as model mistakes
        out = original(facts)
        out["recruiter_outcome"]["attributed_to_model"] = bool(out["recruiter_outcome"]["rejected"])
        return out
    monkeypatch.setattr(feedback, "rates", blaming)
    assert evaluate(SUITES["human_corrections"](dataset_for("human_corrections"), {}))["status"] == "BLOCKED"


def test_a_skipped_suite_never_passes():
    gate = evaluate(SUITES["ats_answers"](dataset_for("ats_answers"), {}))  # no database: skipped
    assert gate["status"] == "BLOCKED" and gate["reason"].startswith("skipped")
    gate = evaluate(SUITES["rag"](dataset_for("rag"), {"rag_output": (1, "")}))
    assert gate["status"] == "BLOCKED"


def test_a_dataset_needs_a_version_and_unique_keys(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"name": "x", "examples": []}))
    with pytest.raises(TypeError, match="version"):
        load(path)
    path.write_text(json.dumps({"name": "x", "version": 1, "examples": [{"key": "a"}, {"key": "a"}]}))
    with pytest.raises(ValueError, match="duplicate"):
        load(path)


def test_jsonl_is_langsmith_examples_with_the_dataset_version():
    lines = [json.loads(line) for line in report.jsonl(["confirmation"])]
    assert len(lines) == len(dataset_for("confirmation").examples)
    assert set(lines[0]) == {"inputs", "outputs", "metadata"}
    assert lines[0]["metadata"]["dataset"] == "confirmation" and lines[0]["metadata"]["dataset_version"] == 1


def test_the_markdown_report_names_versions_hashes_and_gates():
    results = suites.run(["confirmation", "browser_actions"])
    text = report.markdown(results)
    assert "confirmation v1" in text and dataset_for("confirmation").sha256[:12] in text
    assert "Overall: **PASS**" in text and "no suite reads a job rejection as a model error" in text


def test_the_cli_runs_the_database_suite_and_exits_on_its_gate():
    """scripts/agent_eval.py builds its own throwaway database: ATS answers run the real resolver there."""
    proc = subprocess.run([sys.executable, "scripts/agent_eval.py", "--suite", "ats_answers", "--suite",
                           "confirmation", "--json"], capture_output=True, text=True, cwd=ROOT, timeout=300,
                          check=False)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    data = json.loads(proc.stdout)
    ats = next(s for s in data["suites"] if s["name"] == "ats_answers")
    assert ats["gate"]["status"] == "PASS" and ats["metrics"]["sensitive_resolved_unconfirmed"] == 0
