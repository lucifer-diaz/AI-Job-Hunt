"""Evaluations for resume preparation and Job Insights (Phase 4), in LangSmith's dataset and evaluator shapes.

A case is a LangSmith Example: `inputs`, reference `outputs` and `metadata` (`metadata.evaluator` names the check). A
target turns the inputs into run outputs by calling the code the graphs use, never a stand-in: the evidence checks
(`tailoring_results.validate_tailored_resume`), the graph's lineage, overrule and loop rules
(`app.agents.preparation`), the keyword coverage recorded on every version (`workflow.supported_keyword_coverage`), the
provenance check (`job_insights.provenance`) and the compensation rules (`compensation.compensation_insight`). An
evaluator compares outputs with the reference and returns LangSmith's `{"key", "score", "comment"}`.

Ground truth is written by a person in tests/fixtures/preparation_eval/dataset.json, never by a model, and every case
runs on mock providers. Nothing here sends anything anywhere: `scripts/preparation_eval.py --jsonl` writes the cases
for a manual LangSmith import; export goes only through app.agents.tracing_boundary, and this module adds no path.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATASET = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "preparation_eval" / "dataset.json"
EVALUATORS = ("evidence_grounding", "unsupported_claims", "resume_version", "keyword_relevance",
              "reviewer_disagreement", "revision_termination", "company_provenance", "conflicting_salary",
              "reported_vs_generated")


@dataclass
class Example:
    """A LangSmith Example."""

    key: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def evaluator(self) -> str:
        return self.metadata["evaluator"]

    def langsmith(self) -> dict[str, Any]:
        return {"inputs": self.inputs, "outputs": self.outputs, "metadata": {**self.metadata, "key": self.key}}


def load(path: Path = DATASET) -> list[Example]:
    data = json.loads(path.read_text())
    return [Example(c["key"], c["inputs"], c["outputs"], c.get("metadata") or {}) for c in data["examples"]]


def _result(key: str, ok: bool, comment: str = "") -> dict[str, Any]:
    return {"key": key, "score": 1.0 if ok else 0.0, "comment": comment}


# ───────────── evaluators: (outputs, reference) → {"key", "score", "comment"} ─────────────

def evidence_grounding(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """Every unit that survives cites evidence that exists and supports it: no grounding issue on a grounded case."""
    found = set(outputs["codes"]) & set(reference["grounding_codes"])
    return _result("evidence_grounding", not found, f"grounding issues: {sorted(found)}" if found else "")


def unsupported_claims(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """An invented skill or metric is caught, with the code the reference names."""
    missing = set(reference["expected_codes"]) - set(outputs["codes"])
    return _result("unsupported_claims", not missing, f"not caught: {sorted(missing)}" if missing else "")


def resume_version(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    return _result("resume_version", outputs["lineage_ok"] == reference["lineage_ok"])


def keyword_relevance(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """The coverage recorded for a version names exactly the supported posting terms it still leaves out."""
    ok = sorted(outputs["missing_supported"]) == sorted(reference["missing_supported"]) and \
        outputs["score"] == reference["score"]
    return _result("keyword_relevance", ok, json.dumps(outputs) if not ok else "")


def reviewer_disagreement(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    ok = (outputs["decision"], outputs["disagreement"]) == (reference["decision"], reference["disagreement"])
    return _result("reviewer_disagreement", ok, json.dumps(outputs) if not ok else "")


def revision_termination(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """The loop ends within the bound, and a packet that never passed waits for a person (never PASS)."""
    ok = outputs["passes"] == reference["passes"] and outputs["final"] == reference["final"] and \
        outputs["passes"] <= reference["max_revisions"] + 1
    return _result("revision_termination", ok, json.dumps(outputs) if not ok else "")


def company_provenance(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    ok = sorted(outputs["rules"]) == sorted(reference["rules"])
    return _result("company_provenance", ok, json.dumps(outputs) if not ok else "")


def conflicting_salary(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """Employer pay stays the employer's; estimates that disagree are listed, never averaged into a midpoint."""
    ok = outputs["status"] == reference["status"] and outputs["employer_low"] == reference["employer_low"] and \
        outputs["alternatives"] >= reference["min_alternatives"] and outputs["market_midpoint"] is None
    return _result("conflicting_salary", ok, json.dumps(outputs) if not ok else "")


def reported_vs_generated(outputs: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    ok = sorted(outputs["rules"]) == sorted(reference["rules"]) and \
        outputs["kept_reported"] == reference["kept_reported"] and \
        outputs["kept_generated"] == reference["kept_generated"]
    return _result("reported_vs_generated", ok, json.dumps(outputs) if not ok else "")


SCORERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "evidence_grounding": evidence_grounding, "unsupported_claims": unsupported_claims,
    "resume_version": resume_version, "keyword_relevance": keyword_relevance,
    "reviewer_disagreement": reviewer_disagreement, "revision_termination": revision_termination,
    "company_provenance": company_provenance, "conflicting_salary": conflicting_salary,
    "reported_vs_generated": reported_vs_generated,
}


# ───────────── targets: inputs → outputs, through the code the graphs run ─────────────

def target_lineage(inputs: dict[str, Any]) -> dict[str, Any]:
    from app.agents.preparation import lineage_matches

    return {"lineage_ok": lineage_matches(inputs["content"], inputs.get("resume_id"), inputs.get("ranking_id"))}


def target_keywords(inputs: dict[str, Any]) -> dict[str, Any]:
    from app.services.workflow import supported_keyword_coverage

    coverage = supported_keyword_coverage(inputs["targets"], inputs["markdown"])
    return {"missing_supported": coverage["missing_supported"], "score": coverage["score"]}


def target_overrule(inputs: dict[str, Any]) -> dict[str, Any]:
    from app.agents.preparation import overrule

    decision, disagreement = overrule(inputs["review_decision"], inputs["recommendation"], inputs["fact_issues"],
                                      inputs["lineage_ok"])
    return {"decision": decision, "disagreement": disagreement}


def target_loop(inputs: dict[str, Any]) -> dict[str, Any]:
    """Walk the loop with a scripted decision per pass (the last repeats), as DECIDE would."""
    from app.agents.preparation import NEEDS_HUMAN, next_step

    decisions, limit = inputs["decisions"], inputs["max_revisions"]
    revision, passes = 0, 0
    while True:
        passes += 1
        decision = decisions[min(passes - 1, len(decisions) - 1)]
        route, at_limit = next_step(decision, revision, limit)
        if route != "tailor":
            return {"passes": passes, "final": NEEDS_HUMAN if at_limit else decision}
        revision += 1
        if passes > 50:  # a loop that does not end is the failure this evaluates
            return {"passes": passes, "final": "UNBOUNDED"}


def target_provenance(inputs: dict[str, Any]) -> dict[str, Any]:
    from app.services.job_insights import provenance

    kept, report = provenance.enforce(inputs["sections"], inputs["observations"])
    return {"rules": [v["rule"] for v in report.violations],
            "kept_reported": len(kept.get("reported_questions") or []),
            "kept_generated": len(kept.get("generated_questions") or [])}


def target_salary(inputs: dict[str, Any]) -> dict[str, Any]:
    from app.services import compensation

    comp = compensation.compensation_insight(inputs["observations"], role_title=inputs.get("role", ""),
                                             company=inputs.get("company", ""), location=inputs.get("location", ""))
    employer = comp.get("employer_posted_range") or {}
    market = comp.get("market_range") or {}
    alternatives = len((comp.get("conflict") or {}).get("alternatives") or [])
    return {"status": comp.get("status"), "employer_low": employer.get("low"), "alternatives": alternatives,
            "market_midpoint": market.get("midpoint") if comp.get("status") == "CONFLICTING_ESTIMATES" else None}


def target_claims(inputs: dict[str, Any], *, validate: Callable[[dict[str, Any]], list[str]]) -> dict[str, Any]:
    """The deterministic evidence checks over the case's tailored units. `validate` runs them in a database the
    caller set up (the account's verified evidence)."""
    return {"codes": validate(inputs)}


TARGETS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "resume_version": target_lineage, "keyword_relevance": target_keywords,
    "reviewer_disagreement": target_overrule, "revision_termination": target_loop,
    "company_provenance": target_provenance, "conflicting_salary": target_salary,
    "reported_vs_generated": target_provenance,
}


def run(examples: list[Example], *, claims: Callable[[dict[str, Any]], list[str]] | None = None
        ) -> list[dict[str, Any]]:
    """Every example through its target and evaluator. `claims` runs the evidence checks (it needs a database);
    without it the two evidence evaluators are skipped."""
    results = []
    for example in examples:
        name = example.evaluator
        if name in ("evidence_grounding", "unsupported_claims"):
            if claims is None:
                continue
            outputs = target_claims(example.inputs, validate=claims)
        else:
            outputs = TARGETS[name](example.inputs)
        results.append({"example": example.key, **SCORERS[name](outputs, example.outputs), "outputs": outputs})
    return results


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_key: dict[str, list[float]] = {}
    for r in results:
        by_key.setdefault(r["key"], []).append(r["score"])
    return {"examples": len(results), "passed": sum(1 for r in results if r["score"] == 1.0),
            "by_evaluator": {k: {"examples": len(v), "mean": round(sum(v) / len(v), 3)} for k, v in by_key.items()}}
