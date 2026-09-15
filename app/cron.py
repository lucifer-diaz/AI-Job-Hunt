"""Run one scheduled job once, outside the web process: `python -m app.cron [job]`.

The in-app scheduler normally does this. Cron runs use the same job code and the same database lease,
so they never overlap an app instance running that job. With no argument it drafts due follow-ups;
`searches` is kept as an alias for the hunter.
"""
import asyncio
import json
import sys

from app.scheduler import build_scheduler

ALIASES = {"searches": "hunter"}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    name = ALIASES.get(args[0], args[0]).replace("-", "_") if args else "followups"
    scheduler = build_scheduler()
    if name not in scheduler.specs:
        print(f"Unknown job '{name}'. Choose from: {', '.join(scheduler.specs)}")
        return 2
    outcome = asyncio.run(scheduler.run_job(name, trigger="cron"))
    print(json.dumps(outcome.public(), default=str))
    return 1 if outcome.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
