from app.db import SessionLocal
from app.services.workflow import create_due_followups


def main():
    db = SessionLocal()
    try:
        count = create_due_followups(db)
        print(f"created_followups={count}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
