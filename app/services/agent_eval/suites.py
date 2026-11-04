"""The Phase 7 evaluation suites. Each one runs real code over a labelled dataset and reports metrics.

    jd_parser          recorded JD parser outputs → the contract, `job_description.validated` and `ground`
    qualification      `domain_match.assess`, `search_scope.assess`, `ranker.hard_filter_reason`
    rag                scripts/rag_eval.py (Recall@k, Precision@k, leaks) on its own benchmark
    unsupported_claims scripts/preparation_eval.py's evidence checks (`tailoring_results.validate_tailored_resume`)
    ats_answers        `answer_resolver.plan` against a synthetic profile and knowledge bank
    browser_actions    `browser_actions.check`: what a navigator proposal may make the browser do
    confirmation       `submission.ConfirmationVerifier.verify`: what counts as the site's confirmation
    human_corrections  `feedback.rates`: correction rates from stored facts, kept apart from recruiter outcomes
    resume_extraction  uploaded resume → text → structure, against hand-written synthetic resumes

Targets are the production functions, never a stand-in. A case's outputs carry ids, codes and flags only. The JD
parser suite scores outputs a model produced earlier (recorded in the dataset): it measures those outputs and the
deterministic checks they go through, so a model change is evaluated by recording its outputs into a new dataset
version (scripts/evaluate_prompts.py runs a live model through the gateway).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from app.services.agent_eval.datasets import FIXTURES, Dataset, load, suite_dataset

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class SuiteResult:
    name: str
    dataset: dict[str, Any]
    metrics: dict[str, Any]
    cases: list[dict[str, Any]] = field(default_factory=list)
    skipped: str | None = None

    @property
    def failed_cases(self) -> list[dict[str, Any]]:
        return [c for c in self.cases if not c["ok"]]


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


class _Job(SimpleNamespace):
    """A job as the deterministic screens read it: unknown attributes are None, as on a fresh row."""

    def __getattr__(self, name: str) -> Any:
        return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", (text or "").lower()).strip()


# ───────────── JD parser ─────────────

def jd_parser(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    from app.prompts.contracts import JobDescriptionParserOutput
    from app.services import job_description

    cases, tp, predicted, expected, level_ok, ground_ok, ground_n, fabricated, schema_ok = [], 0, 0, 0, 0, 0, 0, 0, 0
    for ex in ds.examples:
        raw, posting, ref = ex.inputs["recorded_output"], ex.inputs["posting"], ex.outputs
        try:
            JobDescriptionParserOutput.model_validate(raw)
            parsed = job_description.ground(job_description.validated(raw), posting)
            valid = True
        except (ValidationError, ValueError):
            valid, parsed = False, None
        if valid != ref.get("schema_valid", True):
            cases.append({"key": ex.key, "ok": False, "comment": f"schema valid {valid}, expected {not valid}"})
            continue
        schema_ok += 1
        if not valid:
            cases.append({"key": ex.key, "ok": True, "outputs": {"schema_valid": False}})
            continue
        got = {_norm(r.normalized_value): r for r in parsed.requirements}
        want = {_norm(r["value"]): r for r in ref.get("requirements") or []}
        hits = set(got) & set(want)
        tp, predicted, expected = tp + len(hits), predicted + len(got), expected + len(want)
        levels = sum(got[v].requirement_level == want[v]["level"] for v in hits)
        level_ok += levels
        # Grounding: an "explicit" requirement must be a sentence of the posting; anything else is derived.
        quoted = [v for v, r in got.items() if r.is_explicit and not r.is_derived]
        fake = [v for v in quoted if not job_description.quoted_in(posting, got[v].source_text)]
        fabricated += len(fake)
        grounded = [v for v in hits if "explicit" in want[v]]
        ground_n += len(grounded)
        ground_ok += sum((got[v].is_explicit and not got[v].is_derived) == want[v]["explicit"] for v in grounded)
        ok = hits == set(want) == set(got) and levels == len(hits) and not fake and \
            all((got[v].is_explicit and not got[v].is_derived) == want[v]["explicit"] for v in grounded)
        cases.append({"key": ex.key, "ok": ok, "outputs": {"requirements": sorted(got), "missing": sorted(set(want) - hits),
                                                          "extra": sorted(set(got) - hits), "fabricated": fake}})
    return SuiteResult("jd_parser", ds.identity(), {
        "cases": len(ds.examples), "schema_accuracy": _rate(schema_ok, len(ds.examples)),
        "requirement_recall": _rate(tp, expected), "requirement_precision": _rate(tp, predicted),
        "level_accuracy": _rate(level_ok, tp), "grounding_accuracy": _rate(ground_ok, ground_n),
        "fabricated_explicit": fabricated}, cases)


# ───────────── qualification ─────────────

def _qualify(inputs: dict[str, Any]) -> tuple[bool, str | None]:
    from app.services import domain_match, ranker, search_scope

    job = _Job(**inputs["job"])
    check = inputs["check"]
    if check == "domain":
        v = domain_match.assess(job, domain_match.Rules(**inputs.get("rules") or {}))
        return v.match, None if v.match else v.code
    if check == "scope":
        s = inputs.get("search") or {}
        v = search_scope.assess(job, s.get("roles") or [], s.get("locations") or [], s.get("skills") or [],
                                distance=s.get("distance", 50), remote_only=s.get("remote_only", False))
        return v.match, None if v.match else v.code
    p = inputs.get("policy") or {}
    policy = ranker.RankingPolicy(minimum_fit_score=0.6, minimum_confidence=0.5,
                                  exclude_companies=frozenset(c.lower() for c in p.get("exclude_companies") or []),
                                  exclude_title_keywords=tuple(p.get("exclude_title_keywords") or ()),
                                  remote_only=p.get("remote_only", False), minimum_salary=p.get("minimum_salary"),
                                  salary_currency=p.get("salary_currency"))
    reason = ranker.hard_filter_reason(job, policy)
    return reason is None, "HARD_FILTER" if reason else None


def qualification(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    cases, false_hide, missed_hide = [], 0, 0
    for ex in ds.examples:
        keep, code = _qualify(ex.inputs)
        want = ex.outputs["keep"]
        false_hide += want and not keep
        missed_hide += (not want) and keep
        ok = keep == want and (want or ex.outputs.get("code") in (None, code))
        cases.append({"key": ex.key, "ok": ok, "outputs": {"keep": keep, "code": code}})
    n = len(ds.examples)
    return SuiteResult("qualification", ds.identity(), {
        "cases": n, "accuracy": _rate(sum(c["ok"] for c in cases), n), "false_hide": false_hide,
        "missed_hide": missed_hide}, cases)


# ───────────── browser actions ─────────────

def browser_actions(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    from app.prompts.contracts import BrowserActionProposal
    from app.services import browser_actions as ba
    from app.services.ats_forms import ApplicationForm

    fx = ds.fixture
    form = ApplicationForm.model_validate(fx["form"])
    cases, unsafe, refused_valid = [], 0, 0
    for ex in ds.examples:
        observation = {"elements": ex.inputs.get("elements") or fx["elements"]}
        try:
            proposal = BrowserActionProposal.model_validate(ex.inputs["proposal"])
        except ValidationError:
            decision: Any = ba.Refusal("CONTRACT", "The proposal is not a valid action")
        else:
            decision = ba.check(proposal, observation, form, fx["plan"], fx["values"], hosts=set(fx["hosts"]))
        accepted = not isinstance(decision, ba.Refusal)
        got = {"accept": accepted, "cmd": getattr(decision, "cmd", decision if isinstance(decision, str) else None),
               "code": getattr(decision, "code", None)}
        want = ex.outputs
        unsafe += accepted and not want["accept"]
        refused_valid += (not accepted) and want["accept"]
        ok = accepted == want["accept"] and (want.get("code") in (None, got["code"])) and \
            (want.get("cmd") in (None, got["cmd"]))
        cases.append({"key": ex.key, "ok": ok, "outputs": got})
    n = len(ds.examples)
    valid = sum(1 for ex in ds.examples if ex.outputs["accept"])
    return SuiteResult("browser_actions", ds.identity(), {
        "cases": n, "accuracy": _rate(sum(c["ok"] for c in cases), n), "unsafe_accepted": unsafe,
        "valid_refused": refused_valid, "valid_accept_rate": _rate(valid - refused_valid, valid),
        "invalid_refuse_rate": _rate((n - valid) - unsafe, n - valid)}, cases)


# ───────────── confirmation ─────────────

def confirmation(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    from app.services.submission import ConfirmationVerifier

    cases, false_confirm, missed = [], 0, 0
    for ex in ds.examples:
        c = ConfirmationVerifier.verify(ex.inputs.get("adapter", "browser_use"), ex.inputs["domain"],
                                        ex.inputs["evidence"])
        want = ex.outputs["confirmed"]
        false_confirm += c.confirmed and not want
        missed += want and not c.confirmed
        cases.append({"key": ex.key, "ok": c.confirmed == want,
                      "outputs": {"confirmed": c.confirmed, "reason": c.reason,
                                  "confirmation_id": c.confirmation_id}})
    n = len(ds.examples)
    ambiguous = [c for c, ex in zip(cases, ds.examples, strict=True) if ex.metadata.get("ambiguous")]
    return SuiteResult("confirmation", ds.identity(), {
        "cases": n, "accuracy": _rate(sum(c["ok"] for c in cases), n), "false_confirm": false_confirm,
        "missed_confirm": missed, "ambiguous_cases": len(ambiguous),
        "ambiguous_left_uncertain": sum(not c["outputs"]["confirmed"] for c in ambiguous)}, cases)


# ───────────── ATS answers ─────────────

def ats_answers(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    """`ctx["resolve"](fields, confirmed)` runs answer_resolver.plan in a throwaway database (scripts/agent_eval.py
    sets it up from the dataset's fixture); without it the suite is skipped."""
    resolve: Callable[..., dict[str, Any]] | None = ctx.get("resolve")
    if resolve is None:
        return SuiteResult("ats_answers", ds.identity(), {}, skipped="needs a database (scripts/agent_eval.py)")
    cases, sensitive_unconfirmed, wrong_source, filled_unknown = [], 0, 0, 0
    for ex in ds.examples:
        entry = resolve([ex.inputs["field"]], ex.inputs.get("confirm_sensitive", False))[ex.inputs["field"]["id"]]
        want = ex.outputs
        source = entry.get("source") or ""
        if entry["status"] == "RESOLVED" and entry.get("sensitive") and not entry.get("confirmed"):
            sensitive_unconfirmed += 1
        if entry["status"] == "RESOLVED" and want["status"] != "RESOLVED":
            filled_unknown += 1
        if want.get("source_prefix") and entry["status"] == "RESOLVED" and not source.startswith(want["source_prefix"]):
            wrong_source += 1
        ok = entry["status"] == want["status"] and (not want.get("source_prefix") or
                                                    source.startswith(want["source_prefix"]))
        cases.append({"key": ex.key, "ok": ok, "outputs": {"status": entry["status"], "source": source or None}})
    n = len(ds.examples)
    return SuiteResult("ats_answers", ds.identity(), {
        "cases": n, "accuracy": _rate(sum(c["ok"] for c in cases), n),
        "sensitive_resolved_unconfirmed": sensitive_unconfirmed, "answered_without_source": filled_unknown,
        "wrong_source": wrong_source}, cases)


# ───────────── human corrections ─────────────

def human_corrections(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    from app.services.agent_eval import feedback

    cases = []
    for ex in ds.examples:
        got = feedback.rates(ex.inputs["facts"])
        want = ex.outputs
        checks = {"human_correction_rate": got["human_correction_rate"] == want["human_correction_rate"],
                  "attributed_to_model": got["recruiter_outcome"]["attributed_to_model"] is False}
        for signal, rate in (want.get("by_signal") or {}).items():
            checks[signal] = got["by_signal"][signal]["rate"] == rate
        for group, count in (want.get("recruiter_outcome") or {}).items():
            checks[f"outcome.{group}"] = got["recruiter_outcome"][group] == count
        cases.append({"key": ex.key, "ok": all(checks.values()),
                      "outputs": {"human_correction_rate": got["human_correction_rate"],
                                  "failed_checks": sorted(k for k, v in checks.items() if not v)}})
    n = len(ds.examples)
    return SuiteResult("human_corrections", ds.identity(), {
        "cases": n, "accuracy": _rate(sum(c["ok"] for c in cases), n)}, cases)


# ───────────── suites that already have a runner ─────────────

def _script(name: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / name), *args], capture_output=True, text=True,
                          cwd=ROOT, timeout=600, check=False)
    return proc.returncode, proc.stdout


def rag(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    code, out = (ctx["rag_output"] if "rag_output" in ctx else _script("rag_eval.py", "--json"))
    if code != 0:
        return SuiteResult("rag", ds.identity(), {}, skipped=f"scripts/rag_eval.py exited {code}")
    report = json.loads(out)["keyword"]  # the variant that runs with no embedding model: what every install has
    keys = ("recall_at_5", "recall_at_10", "precision_at_3", "precision_at_5", "r_precision", "citation_correctness",
            "leaks", "latency_p50_ms", "latency_p95_ms")
    return SuiteResult("rag", ds.identity(), {"cases": report["cases"], **{k: report[k] for k in keys}})


def resume_extraction(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    """Resume extraction: text and structure from synthetic documents (app.services.agent_eval.resume_extraction)."""
    from app.services.agent_eval import resume_extraction as rx

    return rx.run(ds, ctx)


def unsupported_claims(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    code, out = (ctx["preparation_output"] if "preparation_output" in ctx
                 else _script("preparation_eval.py", "--json"))
    results = json.loads(out) if out.strip().startswith("[") else []
    claims = [r for r in results if r["key"] in ("unsupported_claims", "evidence_grounding")]
    if not claims:
        return SuiteResult("unsupported_claims", ds.identity(), {}, skipped=f"scripts/preparation_eval.py exited {code}")
    cases = [{"key": r["example"], "ok": r["score"] == 1.0, "outputs": r["outputs"]} for r in claims]
    caught = [r for r in claims if r["key"] == "unsupported_claims"]
    return SuiteResult("unsupported_claims", ds.identity(), {
        "cases": len(cases), "unsupported_caught_rate": _rate(sum(r["score"] == 1.0 for r in caught), len(caught)),
        "unsupported_passed_through": sum(r["score"] != 1.0 for r in caught),
        "grounded_false_alarms": sum(r["score"] != 1.0 for r in claims if r["key"] == "evidence_grounding"),
        "other_preparation_cases_passed": _rate(sum(r["score"] == 1.0 for r in results), len(results))}, cases)


SUITES: dict[str, Callable[[Dataset, dict[str, Any]], SuiteResult]] = {
    "jd_parser": jd_parser, "qualification": qualification, "rag": rag, "unsupported_claims": unsupported_claims,
    "ats_answers": ats_answers, "browser_actions": browser_actions, "confirmation": confirmation,
    "human_corrections": human_corrections, "resume_extraction": resume_extraction,
}
# Suites whose cases live in an older suite's own dataset.
EXTERNAL_DATASETS = {"rag": FIXTURES / "rag_eval" / "dataset.json",
                     "unsupported_claims": FIXTURES / "preparation_eval" / "dataset.json",
                     "resume_extraction": FIXTURES / "resume_extraction" / "dataset.json"}


def dataset_for(name: str) -> Dataset:
    # The RAG benchmark keeps its own shape (resumes, facts, jobs): only its version and hash are read here.
    if name not in EXTERNAL_DATASETS:
        return suite_dataset(name)
    ds = load(EXTERNAL_DATASETS[name])
    ds.name = f"{EXTERNAL_DATASETS[name].parent.name}/{EXTERNAL_DATASETS[name].stem}"
    return ds


def run(names: list[str] | None = None, ctx: dict[str, Any] | None = None) -> list[SuiteResult]:
    ctx = ctx or {}
    return [SUITES[name](dataset_for(name), ctx) for name in (names or list(SUITES))]
