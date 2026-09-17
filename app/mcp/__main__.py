"""Command line for the Claude Desktop MCP server.

    python -m app.mcp                                   serve over stdio (Claude Desktop starts this)
    python -m app.mcp create-token --email you@example.com [--name "Claude Desktop"]

Standard output is the MCP channel, so logs and errors go to standard error.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def configured_token(env_file: str | Path = ".env") -> str | None:
    """JOBHUNT_MCP_TOKEN from the environment the MCP client passed, else from .env.

    Clients such as the MCP Inspector start servers with a minimal environment, so .env must work on its own.
    """
    from app.settings import Settings

    return os.environ.get("JOBHUNT_MCP_TOKEN") or Settings(_env_file=env_file).jobhunt_mcp_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.mcp", description="Job Hunt OS MCP server for Claude Desktop")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Serve MCP over stdio (default)")
    create = sub.add_parser("create-token", help="Create an MCP token for an account and print it once")
    create.add_argument("--email", required=True)
    create.add_argument("--name", default="Claude Desktop")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Claude Desktop starts servers from an arbitrary directory: resolve .env and relative paths from the repository.
    os.chdir(REPO_ROOT)
    from sqlalchemy import func, select
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from app.db import SessionLocal
    from app.models import User
    from app.services.mcp_tokens import InvalidToken, create_token, resolve_token

    try:
        with SessionLocal() as db:
            if args.command == "create-token":
                user = db.scalar(select(User).where(func.lower(User.email) == args.email.strip().lower()))
                if user is None:
                    print(f"No account with email {args.email}", file=sys.stderr)
                    return 1
                _, token = create_token(db, args.name, user_id=user.id)
                print(token)
                return 0
            user_id = resolve_token(db, configured_token())
    except InvalidToken as exc:
        print(f"Job Hunt OS MCP: authentication failed: {exc}", file=sys.stderr)
        return 2
    except (OperationalError, ProgrammingError) as exc:
        print("Job Hunt OS MCP: the database is unreachable or not migrated. Start the database and run Job Hunt OS "
              f"once (or `python -m app.db_migrations`). Details: {exc.__class__.__name__}", file=sys.stderr)
        return 3

    from app.mcp.server import build_server

    logging.getLogger("jobhunt.mcp").info("Serving Job Hunt OS MCP tools for account %s", user_id)
    build_server(user_id).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
