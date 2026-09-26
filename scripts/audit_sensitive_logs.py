"""Find credential-shaped values already stored in telemetry and log tables. Prints counts only, never a value.

    python scripts/audit_sensitive_logs.py                # read-only report for DATABASE_URL
    python scripts/audit_sensitive_logs.py --json         # the same, machine-readable
    python scripts/audit_sensitive_logs.py --apply        # re-redact affected text in place (never deletes a row)

Detection is app.services.redaction itself: a value is flagged when redacting it would change it. That covers rows
written before a pattern existed (for example `user_api_key` inside LiteLLM metadata, a password inside a database
URL, or LLM_PROMPT_DEBUG text stored before debug text was redacted at write time).

--apply rewrites only the flagged columns with their redacted form, so audit records keep their shape, time and
meaning without the secret. Rows are never deleted; retention (app.services.scheduled_jobs) still ages them out.
If anything is found, rotate the credential: a secret that reached a log may also have reached a backup.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, select, update

import app.models  # noqa: F401  registers every table on Base.metadata
from app.db import Base, SessionLocal
from app.services.redaction import redact_text, redact_value

# Tables that hold telemetry, logs or free text a provider or an operator produced, and the columns to check.
# litellm_connections.api_key_enc is excluded: it is encrypted by design and never shown.
SCANNED = {
    "external_api_request_logs": ("endpoint_path", "error_message", "request_metadata"),
    "llm_request_logs": ("error_message", "debug_prompt", "debug_response"),
    "system_logs": ("message", "details"),
    "ai_config_audit": ("changes", "sync_result", "error"),
    "litellm_connections": ("base_url", "last_error", "last_sync_error"),
    "ai_optimization_tasks": ("error",),
    "job_ranking_tasks": ("last_error",),
    "tailor_tasks": ("error", "result"),
    "scheduler_jobs": ("last_error", "last_result"),
    "search_runs": ("error", "input_json", "search_params"),
    "conversation_actions": ("arguments_summary", "result_summary"),
}
BATCH = 500


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value, max(len(value), 1))
    if isinstance(value, (dict, list)):
        return redact_value(value)
    return value


def _changed(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (dict, list)):
        return json.dumps(_clean(value), sort_keys=True, default=str) != json.dumps(value, sort_keys=True, default=str)
    return _clean(value) != value


def audit(apply: bool = False) -> dict[str, dict[str, int]]:
    """{table: {column: rows with a credential-shaped value}}. With `apply`, those values are redacted in place."""
    found: dict[str, dict[str, int]] = {}
    with SessionLocal() as db:
        schema = inspect(db.get_bind())
        existing = set(schema.get_table_names())
        for name, columns in SCANNED.items():
            table = Base.metadata.tables.get(name)
            if table is None or name not in existing:
                continue  # an older database without this table
            in_db = {c["name"] for c in schema.get_columns(name)}
            present = [c for c in columns if c in table.c and c in in_db]
            key = next(iter(table.primary_key.columns))
            counts = dict.fromkeys(present, 0)
            last = None
            while present:
                query = select(key.label("_key"), *[table.c[c] for c in present]).order_by(key).limit(BATCH)
                rows = db.execute(query if last is None else query.where(key > last)).all()
                if not rows:
                    break
                for row in rows:
                    fixes = {c: _clean(getattr(row, c)) for c in present if _changed(getattr(row, c))}
                    for column in fixes:
                        counts[column] += 1
                    if apply and fixes:
                        db.execute(update(table).where(key == row._key).values(**fixes))
                last = rows[-1]._key
                if apply:
                    db.commit()
            found[name] = counts
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="re-redact flagged values in place (no deletion)")
    parser.add_argument("--json", action="store_true", help="print the counts as JSON")
    args = parser.parse_args()
    report = audit(apply=args.apply)
    total = sum(n for columns in report.values() for n in columns.values())
    if args.json:
        print(json.dumps({"total": total, "tables": report, "applied": args.apply}, indent=2))
        return 0
    for table, columns in report.items():
        flagged = {c: n for c, n in columns.items() if n}
        print(f"{table:28} " + (", ".join(f"{c}: {n}" for c, n in flagged.items()) if flagged else "clean"))
    print(f"\n{total} value(s) with credential-shaped content" + (" were redacted in place." if args.apply and total
                                                                  else "."))
    if total and not args.apply:
        print("Rotate the affected credentials, then run again with --apply to redact the stored copies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
