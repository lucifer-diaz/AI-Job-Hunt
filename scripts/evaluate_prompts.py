"""Evaluate the production prompts on the golden fixtures with real models, to compare models or prompt versions.

    python scripts/evaluate_prompts.py --user-id 1                               # the account's configured models
    python scripts/evaluate_prompts.py --user-id 1 --model tier-2 --model tier-3 # compare LiteLLM models
    python scripts/evaluate_prompts.py --user-id 1 --worker tailor --repeat 3 --out results/v5.json
    python scripts/evaluate_prompts.py --compare results/v4.json results/v5.json # two prompt versions (two runs)

Only the fictional golden fixtures (tests/fixtures/prompt_golden) are sent, never an account's data. Every request
goes through the production LLM layer, so it is recorded in Monitoring like any other (workflow type `eval`) and is
checked against the worker's output contract.

Per case it measures: schema success (and the failure reason, e.g. POSSIBLE_CROSS_WORKER_OUTPUT), evidence validity
(every cited id exists in the fixture), prohibited claims (requirements the candidate lacks, invented facts),
expected classifications (e.g. PARTIAL_MATCH), latency and tokens. To compare prompt versions, run once per
checkout and `--compare` the two result files.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services import model_resolution
from app.services.ai_workers import worker_for_prompt
from app.services.llm import LLMCall, LLMClient, LLMError
from tests import golden


def _same(got: Any, expected: Any) -> bool:
    """Lists compare as sets of items (order is not a quality signal); anything else compares as a value."""
    if isinstance(expected, list):
        return sorted(map(str, got or [])) == sorted(map(str, expected))
    return got == expected


def _expectations(fixture: dict[str, Any], validated: Any) -> list[str]:
    failures = []
    for field, expected in (fixture.get("expect") or {}).items():
        if field.endswith("_contain"):
            if not any(expected in item for item in getattr(validated, field.removesuffix("_contain"), []) or []):
                failures.append(f"{field}: {expected!r} missing")
        elif not _same(getattr(validated, field, None), expected):
            failures.append(f"{field}: expected {expected}")
    return failures


def run_case(user_id: int, name: str, model: str | None) -> dict[str, Any]:
    fixture = golden.load(name)
    prompt = golden.build(fixture)
    worker = worker_for_prompt(prompt.name)
    out: dict[str, Any] = {"case": name, "worker": worker, "prompt_version": prompt.version, "model": model}
    with SessionLocal() as db:
        db.info["user_id"] = user_id
        client = LLMClient.for_user(db, user_id)
        call = LLMCall(workflow_type="eval")
        if model:  # the worker's configuration with only its model swapped, so every other setting is the same
            call = LLMCall(workflow_type="eval", worker_type=worker,
                           resolved=replace(model_resolution.get(user_id, worker), model=model))
        started = time.monotonic()
        try:
            result = client.generate_prompt(prompt, call)
        except LLMError as exc:
            out.update(schema_ok=False, failure=getattr(exc, "reason", type(exc).__name__), error=str(exc)[:300],
                       latency_ms=int((time.monotonic() - started) * 1000))
            return out
    answer = result.data
    out.update(schema_ok=True, latency_ms=int((time.monotonic() - started) * 1000), answered_by=result.model,
               input_tokens=result.usage.input_tokens if result.usage else None,
               output_tokens=result.usage.output_tokens if result.usage else None,
               invalid_evidence=sorted(golden.cited_evidence(answer) - golden.known_evidence()),
               prohibited_claims=golden.prohibited_claims(fixture, answer),
               expectation_failures=_expectations(fixture, result.validated))
    out["passed"] = not (out["invalid_evidence"] or out["prohibited_claims"] or out["expectation_failures"])
    return out


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_key: dict[tuple, list[dict[str, Any]]] = {}
    for item in results:
        by_key.setdefault((item["model"] or "configured", item["worker"], item["prompt_version"]), []).append(item)
    rows = []
    for (model, worker, version), items in sorted(by_key.items()):
        ok = [i for i in items if i.get("schema_ok")]
        latencies = [i["latency_ms"] for i in items if i.get("latency_ms") is not None]
        rows.append({"model": model, "worker": worker, "prompt_version": version, "runs": len(items),
                     "schema_success": round(len(ok) / len(items), 3),
                     "passed": round(sum(bool(i.get("passed")) for i in items) / len(items), 3),
                     "unsupported_claims": sum(len(i.get("prohibited_claims") or []) for i in items),
                     "invalid_evidence": sum(len(i.get("invalid_evidence") or []) for i in items),
                     "failures": sorted({i["failure"] for i in items if i.get("failure")}),
                     "median_latency_ms": int(statistics.median(latencies)) if latencies else None,
                     "output_tokens": sum(i.get("output_tokens") or 0 for i in ok)})
    return {"rows": rows}


def print_table(summary: dict[str, Any]) -> None:
    header = ("model", "worker", "prompt_version", "runs", "schema_success", "passed", "unsupported_claims",
              "invalid_evidence", "median_latency_ms", "output_tokens", "failures")
    print(" | ".join(header))
    for row in summary["rows"]:
        print(" | ".join(str(row[key]) for key in header))


def compare(first: Path, second: Path) -> None:
    a, b = (json.loads(p.read_text())["summary"]["rows"] for p in (first, second))
    index = {(r["model"], r["worker"]): r for r in a}
    for row in b:
        before = index.get((row["model"], row["worker"]))
        if before is None:
            continue
        print(f"{row['worker']:18} {row['model']:24} {before['prompt_version']} → {row['prompt_version']}: "
              f"schema {before['schema_success']} → {row['schema_success']}, passed {before['passed']} → "
              f"{row['passed']}, unsupported claims {before['unsupported_claims']} → {row['unsupported_claims']}, "
              f"median latency {before['median_latency_ms']} → {row['median_latency_ms']} ms")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--user-id", type=int, help="the account whose AI configuration runs the fixtures")
    parser.add_argument("--model", action="append", help="a LiteLLM model to compare (repeatable)")
    parser.add_argument("--worker", action="append", help="golden case names to run (default: all)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", type=Path, help="write results as JSON")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare)
        return 0
    if args.user_id is None:
        parser.error("--user-id is required to run the fixtures")
    cases = args.worker or golden.names()
    results = [run_case(args.user_id, case, model) for model in (args.model or [None]) for case in cases
               for _ in range(max(1, args.repeat))]
    summary = summarise(results)
    print_table(summary)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
