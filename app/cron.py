"""Scheduled jobs. `python -m app.cron` drafts due follow-ups; `python -m app.cron searches`
runs the saved search profile when its schedule is daily."""
import sys

from app.db import SessionLocal
from app.services.workflow import create_due_followups
from app.workspace import get_search_profile, run_search_profile


def main():
    db = SessionLocal()
    try:
        if "searches" in sys.argv[1:]:
            profile = get_search_profile(db)
            if profile.get("schedule") != "daily":
                print("started_searches=0 reason=manual_schedule")
                return
            runs = run_search_profile(db, profile)
            print(f"started_searches={len(runs)}")
            return
        count = create_due_followups(db)
        print(f"created_followups={count}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
