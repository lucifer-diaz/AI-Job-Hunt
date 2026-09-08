"""Upgrade a seeded pre-Alembic database and verify retention and rollback.

This intentionally builds the legacy database at the baseline revision, rather
than using current ORM metadata, so the check detects migration-chain mistakes.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def alembic_config(url: str) -> Config:
    os.environ["DATABASE_URL"] = url
    # Settings may already have been imported by an embedding test process.
    from app.settings import get_settings

    get_settings.cache_clear()
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def verify() -> None:
    with tempfile.TemporaryDirectory() as directory:
        url = f"sqlite:///{directory}/legacy.db"
        config = alembic_config(url)
        command.upgrade(config, "0001_baseline")
        engine = create_engine(url)
        now = datetime(2025, 1, 1)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO candidate_profiles (id,name,email,phone,location,linkedin_url,github_url,headline,target_roles,preferred_locations,work_authorization,compensation_notes,master_resume,search_preferences,updated_at) VALUES (1,'Ada','ada@example.com','','','','','','[]','[]','','','legacy resume','{}',:now)"
                ),
                {"now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO jobs (id,source,source_ref,title,company,location,url,description,raw,discovered_at,fit_score,fit_analysis,status) VALUES (1,'manual','legacy-1','Engineer','Example','','','','{}',:now,90,'{}','approved')"
                ),
                {"now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO resume_variants (id,job_id,version,content,markdown,cover_letter,reviewer_score,reviewer_notes,created_at) VALUES (1,1,1,'{\"summary\":\"kept\"}','resume markdown','letter',95,'{}',:now)"
                ),
                {"now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO applications (id,job_id,status,approved_at,applied_at,followup_due_at,recruiter_name,recruiter_email,notes,created_at) VALUES (1,1,'approved',:now,NULL,NULL,'','','legacy notes',:now)"
                ),
                {"now": now},
            )
        command.upgrade(config, "head")
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT name FROM candidate_profiles WHERE id=1")).scalar_one()
                == "Ada"
            )
            assert (
                conn.execute(text("SELECT title FROM jobs WHERE id=1")).scalar_one() == "Engineer"
            )
            assert (
                conn.execute(text("SELECT notes FROM applications WHERE id=1")).scalar_one()
                == "legacy notes"
            )
            assert (
                conn.execute(
                    text("SELECT content FROM resume_documents WHERE candidate_profile_id=1")
                ).scalar_one()
                == "legacy resume"
            )
            assert (
                conn.execute(
                    text("SELECT markdown FROM approved_resume_versions WHERE application_id=1")
                ).scalar_one()
                == "resume markdown"
            )
            assert (
                conn.execute(
                    text("SELECT state FROM application_execution_states WHERE application_id=1")
                ).scalar_one()
                == "ready"
            )
        command.downgrade(config, "0001_baseline")
        tables = set(inspect(engine).get_table_names())
        assert "application_settings" not in tables and "ats_knowledge_records" not in tables
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT email FROM candidate_profiles WHERE id=1")).scalar_one()
                == "ada@example.com"
            )
            assert (
                conn.execute(text("SELECT company FROM jobs WHERE id=1")).scalar_one() == "Example"
            )
            assert (
                conn.execute(text("SELECT status FROM applications WHERE id=1")).scalar_one()
                == "approved"
            )
        command.downgrade(config, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    print(
        "Migration verification passed: legacy data retained; incremental and baseline downgrades passed."
    )


if __name__ == "__main__":
    verify()
