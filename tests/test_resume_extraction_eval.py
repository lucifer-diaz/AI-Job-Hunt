"""The resume_extraction evaluation (app.services.agent_eval.resume_extraction): its fixtures reproduce the PDF
ligature bug, and its metrics count what they claim to — a doctored pipeline must turn the gate BLOCKED."""
from __future__ import annotations

import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest

from app.services.agent_eval import GATES, SUITES, dataset_for, evaluate
from app.services.agent_eval import resume_extraction as rx

ROOT = Path(__file__).resolve().parents[1]


def _layer(name: str) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO((rx.DOCUMENTS / name).read_bytes())).pages)


# ───────────── fixtures ─────────────

def test_the_committed_documents_match_the_dataset():
    proc = subprocess.run([sys.executable, "scripts/build_resume_fixtures.py", "--check"], capture_output=True,
                          text=True, cwd=ROOT, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_broken_pdfs_reproduce_the_ligature_bug():
    clean, broken = _layer("ben-two_column-clean.pdf"), _layer("ben-two_column-broken.pdf")
    assert "Kafka" in clean and "Kaga" not in clean
    assert "Kaga" in broken and "li4ed" in broken and "hCps://" in broken


def test_the_actualtext_pdfs_carry_the_right_text_beside_the_broken_mapping():
    data = (rx.DOCUMENTS / "ana-single-actualtext.pdf").read_bytes()
    from pypdf import PdfReader

    contents = b"".join(p.get_contents().get_data() for p in PdfReader(BytesIO(data)).pages)
    assert b"/ActualText (fk)" in contents and b"/ActualText (ti)" in contents
    assert "Kaga" in _layer("ana-single-actualtext.pdf"), "a reader that ignores ActualText still sees the bug"


def test_ground_truth_terms_cover_the_cases_the_spec_names():
    resumes = rx.load_resumes()
    words = " ".join(" ".join(rx.document_lines(r)) for r in resumes.values())
    for term in ("Kafka", "transformation", "optimization", "certification", "affiliate", "workflow", "Software",
                 "Python", "Hybrid", "Analytics", "Gateway"):
        assert term.lower() in words.lower(), term
    unknown = [i["name"] for r in resumes.values() for g in r["skills"] for i in g["items"]
               if i.get("recognized") is False]
    assert len(unknown) == 4


# ───────────── metrics ─────────────

def test_tokens_drop_bullets_bars_and_outer_punctuation():
    assert rx.tokens("• Built Kafka, dbt | 2021 – Present (remote).") == ["Built", "Kafka", "dbt", "2021",
                                                                          "Present", "remote"]


def test_compare_tokens_pairs_a_misreading_with_its_word():
    out = rx.compare_tokens(["Kafka", "streams"], ["Kaga", "streams"])
    assert out["wrong"] == ["Kafka→Kaga"] and out["errors"] == 2 and out["chars"] == 12


def test_a_repair_that_breaks_a_valid_word_is_counted():
    assert rx.altered(["Python", "Kafka"], ["Python", "Kaga"], ["Pythoti", "Kafka"]) == ["Python"]


def test_corrections_reward_fixes_punish_wrong_words_and_ignore_partial_repairs():
    expected = ["Kafka", "certification", "Python"]
    assert rx.corrections(expected, ["Kaga"], ["Kafka"])[:2] == (1, 1)
    made, right, wrong = rx.corrections(expected, ["Kaga"], ["Saga"])  # a real word, but not the right one
    assert (made, right) == (1, 0) and wrong == ["Kaga→Saga"]
    assert rx.corrections(expected, ["cerYﬁcaYon"], ["cerYficaYon"])[:2] == (0, 0)  # still broken: neither
    assert rx.corrections(expected, ["Python"], ["Pythoti"])[:2] == (1, 0)


def test_structure_scoring_separates_invented_misfiled_and_dropped_values():
    resume = rx.load_resumes()["chloe"]
    text = "\n".join(rx.document_lines(resume))
    data = {"summary": resume["summary"] + " Go, Kubernetes", "skills": [{"name": "Rust"}], "experience": [],
            "education": [], "certifications": [], "projects": []}
    s = rx.score_structure(resume, data, text)
    assert s["hallucinated"] == ["skills: Rust"], "a word in neither the resume nor its text is invented"
    assert "skills: Kubernetes" in s["misfiled"] and "skills: Rust" not in s["misfiled"]
    assert s["unrecognized_dropped"] == ["Tessellate DSL"]
    misread = {**data, "skills": [{"name": "Kaga"}]}
    assert rx.score_structure(resume, misread, text + " Kaga")["hallucinated"] == [], "misread, not invented"


# ───────────── the suite ─────────────

def test_the_suite_scores_the_current_pipeline_on_every_gated_metric():
    result = SUITES["resume_extraction"](dataset_for("resume_extraction"), {})
    assert result.metrics["pipeline"] == "current" and result.metrics["cases"] == len(dataset_for(
        "resume_extraction").examples)
    for gate in GATES["resume_extraction"]:
        assert result.metrics[gate.metric] is not None, gate.metric
    assert {"cer_clean", "cer_broken", "cer_actualtext", "cer_text"} <= set(result.metrics)


def test_a_pipeline_that_rewrites_valid_words_is_blocked(monkeypatch):
    current = rx.PIPELINES["current"]

    def careless(text: str) -> str:  # applies "y → ti" everywhere: the corruption the spec warns about
        return current.repair(text).replace("y", "ti")

    monkeypatch.setitem(rx.PIPELINES, "careless", rx.Pipeline(current.read, careless, current.structure))
    result = SUITES["resume_extraction"](dataset_for("resume_extraction"), {"resume_pipeline": "careless"})
    gate = evaluate(result)
    assert result.metrics["valid_words_altered"] > 0
    assert gate["status"] == "BLOCKED" and "valid_words_altered" in gate["reason"]


def test_a_pipeline_that_invents_a_skill_is_blocked(monkeypatch):
    current = rx.PIPELINES["current"]

    def inventive(text: str) -> dict:
        data = current.structure(text)
        return {**data, "skills": [*data.get("skills", []), {"name": "Haskell", "category": ""}]}

    monkeypatch.setitem(rx.PIPELINES, "inventive", rx.Pipeline(current.read, current.repair, inventive))
    result = SUITES["resume_extraction"](dataset_for("resume_extraction"), {"resume_pipeline": "inventive"})
    assert result.metrics["hallucinated_values"] == 16  # one per document
    assert "hallucinated_values" in evaluate(result)["reason"]


@pytest.mark.parametrize("flag", [[], ["--with-real-resumes"]])
def test_the_cli_scores_the_suite_and_reports_it_blocked_until_the_targets_are_met(flag):
    proc = subprocess.run([sys.executable, "scripts/agent_eval.py", "--suite", "resume_extraction", *flag],
                          capture_output=True, text=True, cwd=ROOT, timeout=300, check=False)
    assert "resume_extraction" in proc.stdout and proc.returncode in (0, 1), proc.stderr[-2000:]


def test_v2_reads_the_actualtext_pdfs_without_errors_and_keeps_the_hard_gates():
    ds = dataset_for("resume_extraction")
    current = SUITES["resume_extraction"](ds, {}).metrics
    v2 = SUITES["resume_extraction"](ds, {"resume_pipeline": "v2"}).metrics
    assert v2["cer_actualtext"] == 0.0 < current["cer_actualtext"]
    assert v2["cer"] < current["cer"] and v2["bullets_intact"] > current["bullets_intact"]
    assert v2["field_recall"] >= current["field_recall"] and v2["field_precision"] >= current["field_precision"]
    assert v2["valid_words_altered"] == 0 and v2["hallucinated_values"] == 0
    assert v2["quality_routing"] == 1.0 and current["quality_routing"] is None


def test_ground_truth_can_state_a_role_header_as_the_document_writes_it():
    role = {"title": "Lead Engineer", "company": "Zendrive", "location": "Remote, US", "dates": "Jun 2024 - Oct 2025",
            "bullets": []}
    assert rx.role_lines(role) == ["Lead Engineer | Zendrive | Remote, US | Jun 2024 - Oct 2025"]
    assert rx.role_lines({**role, "header": ["Zendrive", "Lead Engineer"]}) == ["Zendrive", "Lead Engineer"]
    edu = {"degree": "B.Tech", "field_of_study": "CS", "institution": "KGEC", "dates": "2010 - 2014"}
    assert rx.section_lines({"education": [edu]}, "education") == ["B.Tech, CS | KGEC | 2010 - 2014"]


def test_a_normalised_dash_or_a_value_cut_at_its_limit_is_not_invented():
    resume = rx.load_resumes()["ana"]
    text = "\n".join(rx.document_lines(resume))
    dash = {"summary": "", "skills": [], "experience": [{"job_title": "x", "company": "", "responsibilities": [
        "Led the transformation of nightly batch jobs into real-time workflows, cutting reporting latency from 6 hours "
        "to 4 minutes."]}], "education": [], "certifications": [], "projects": []}
    assert rx.score_structure(resume, dash, text.replace("–", "—"))["hallucinated"] == ["roles: x"]
    long = ("Data platform engineer with nine years of experience building streaming pipelines, workflow automation and "
            "analytics platforms. Led the digital transformation of batch reporting into real-time Kafka services with a "
            "focus on reliability and cost optimization. Data platform engineer with nine years of experience build")[:300]
    cut = {**dash, "experience": [], "projects": [{"name": long}]}
    assert len(long) == 300 and rx.score_structure(resume, cut, text)["hallucinated"] == []
