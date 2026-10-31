"""The evaluation report: Markdown for a pull request, JSON for a machine, JSONL for a manual LangSmith import."""
from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from typing import Any

from app.services.agent_eval.gates import evaluate
from app.services.agent_eval.suites import SuiteResult, dataset_for


def as_dict(results: list[SuiteResult]) -> dict[str, Any]:
    gates = [evaluate(r) for r in results]
    return {"generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "python": platform.python_version(),
            "status": "PASS" if all(g["status"] == "PASS" for g in gates) else "BLOCKED",
            "suites": [{"name": r.name, "dataset": r.dataset, "metrics": r.metrics, "skipped": r.skipped,
                        "gate": g, "failed_cases": r.failed_cases} for r, g in zip(results, gates, strict=True)]}


def _value(v: Any) -> str:
    return "—" if v is None else f"{v:.4g}" if isinstance(v, float) else str(v)


def markdown(results: list[SuiteResult]) -> str:
    data = as_dict(results)
    lines = ["# Agent evaluation report", "",
             f"Overall: **{data['status']}** · generated {data['generated_at']} · Python {data['python']}", "",
             ("Reproduce: `python scripts/agent_eval.py --report docs/evals/phase7-report.md` (mock providers, a "
              "throwaway SQLite database, LangSmith export off)."), "",
             "| Suite | Gate | Dataset | Cases | Key metrics |", "| --- | --- | --- | --- | --- |"]
    for s in data["suites"]:
        d = s["dataset"]
        metrics = ", ".join(f"{k} {_value(v)}" for k, v in s["metrics"].items() if k != "cases") or s["skipped"] or ""
        lines.append(f"| {s['name']} | {s['gate']['status']} | {d['name']} v{d['version']} `{d['sha256'][:12]}` | "
                     f"{s['metrics'].get('cases', '—')} | {metrics} |")
    lines += ["", "## Gates", ""]
    for s in data["suites"]:
        lines.append(f"### {s['name']} — {s['gate']['status']}")
        for c in s["gate"]["checks"]:
            lines.append(f"- {'✅' if c['passed'] else '❌'} `{c['metric']}` = {_value(c['value'])} "
                         f"(needs {c['op']} {c['threshold']}): {c['why']}")
        if s["gate"]["status"] == "BLOCKED" and not s["gate"]["checks"]:
            lines.append(f"- ❌ {s['gate']['reason']}")
        for case in s["failed_cases"]:
            lines.append(f"- failing case `{case['key']}`: {json.dumps(case.get('outputs') or case.get('comment'))}")
        lines.append("")
    lines += [("Model quality and the recruiter's decision are reported separately: no suite reads a job rejection "
               "as a model error."), ""]
    return "\n".join(lines)


def jsonl(names: list[str]) -> list[str]:
    out = []
    for name in names:
        ds = dataset_for(name)
        out += [json.dumps(ex.langsmith(ds.name, ds.version)) for ex in ds.examples]
    return out
