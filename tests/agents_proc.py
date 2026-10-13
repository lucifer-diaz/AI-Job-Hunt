"""A pilot graph worker in its own process, for the restart tests (tests/test_agents_postgres.py).

    python -m tests.agents_proc <database url> <schema> <thread id> <user id> <mode> <marker file>

mode `hang_at_compute`: run until the `compute` node, write the marker, then wait to be killed (SIGKILL).
mode `die_after_effect`: run until the effect is committed, write the marker, then exit at once (os._exit), before
LangGraph writes the step's checkpoint.
mode `until_interrupt`: run normally (a review run stops at its approval interrupt) and exit.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> None:
    url, schema, thread_pk, user_id, mode, marker = sys.argv[1:7]
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.agents import runtime
    from app.db import AppSession

    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = sessionmaker(bind=engine, class_=AppSession, autoflush=False)

    def hold(point: str) -> None:
        if mode == "hang_at_compute" and point == "compute":
            Path(marker).write_text("compute")
            while True:
                time.sleep(1)
        if mode == "die_after_effect" and point == "finalize:after_effect":
            Path(marker).write_text("effect")
            os._exit(1)

    with sessions() as db:
        db.info["user_id"] = int(user_id)
        run = runtime.claim(db, int(thread_pk), int(user_id), ("QUEUED",), f"proc:{os.getpid()}")
    if run is None:
        sys.exit(3)
    outcome = runtime.execute(sessions, run, hold=hold)
    Path(marker).write_text(outcome)


if __name__ == "__main__":
    main()
