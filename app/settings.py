from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Job Hunt OS"
    database_url: str = "sqlite:///./jobhunt.db"
    app_base_url: str = "http://localhost:8000"
    cron_secret: str = "change-me"
    app_username: str | None = None
    app_password: str | None = None

    # LLM provider: openai | anthropic | openrouter | mock
    llm_provider: str = "openai"

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # OpenRouter is OpenAI-compatible. `openrouter/free` automatically routes
    # among currently available free models that can satisfy the request.
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openrouter/free"
    openrouter_model_rank: str | None = None
    openrouter_model_tailor: str | None = None
    openrouter_model_review: str | None = None
    openrouter_model_followup: str | None = None
    openrouter_fallback_models: str = ""
    openrouter_require_free: bool = True
    openrouter_max_tokens: int = 5000
    openrouter_site_url: str | None = None
    openrouter_app_name: str = "Job Hunt OS"

    # Google ADK / Vertex AI Agent Engine. Authentication and project/location
    # are read by the Google SDK from standard GOOGLE_* environment variables.
    gemini_agent_model: str = "gemini-2.5-flash"

    apify_api_token: str | None = None
    apify_actor_id: str = "openclawai/job-board-scraper"
    apify_dataset_limit: int = 5000

    min_fit_score: int = 72
    # Set to 0 to require a completed review without enforcing a numeric cutoff.
    min_review_score: float = 0
    followup_days: int = 5
    max_jobs_per_batch: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
