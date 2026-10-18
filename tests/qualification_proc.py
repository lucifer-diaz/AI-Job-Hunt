"""A qualification graph worker in its own process, for the restart tests (tests/test_qualification_graph.py).

    python -m tests.qualification_proc <database url> <schema> <user id> <point> <how> <marker file>

It claims the account's due qualification task and runs it until the node hook `point`. Then:
  * `exit`: write the marker and exit at once (os._exit). The node's effect and business write are committed; the
    step's checkpoint is not.
  * `hang`: write the marker and wait to be killed (SIGKILL).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> None:
    url, schema, user_id, point, how, marker = sys.argv[1:7]
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import AppSession
    from app.services import qualification

    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = sessionmaker(bind=engine, class_=AppSession, autoflush=False)

    def hold(at: str) -> None:
        if at != point:
            return
        Path(marker).write_text(at)
        if how == "exit":
            os._exit(1)
        while True:
            time.sleep(1)

    with sessions() as db:
        claims = [c for c in qualification.claim(db, worker_id=f"proc:{os.getpid()}", limit=5)
                  if c.user_id == int(user_id)]
    if not claims:
        sys.exit(3)
    outcome = qualification.process(sessions, claims[0], hold=hold)
    Path(marker).write_text(f"finished:{outcome}")


if __name__ == "__main__":
    main()
