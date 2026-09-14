"""Isolate the test suite from a developer's .env before any app module is imported.

Settings are read once and cached, and .env may point at a real database, real API keys and
basic-auth credentials. Tests always use TEST_DATABASE_URL (or throwaway SQLite), the mock LLM,
no Apify token and no login.
"""
import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_suite.db")
os.environ["LLM_PROVIDER"] = "mock"
for key in ("APP_USERNAME", "APP_PASSWORD", "APIFY_API_TOKEN", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[key] = ""
