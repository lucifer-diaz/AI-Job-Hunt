from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Job Hunt OS"
    database_url: str = "sqlite:///./jobhunt.db"
    app_base_url: str = "http://localhost:8000"
    cron_secret: str = "change-me"

    # Accounts (app.auth). SECRET_KEY encrypts the API keys users save; keep it stable and private.
    secret_key: str | None = None
    allow_signup: bool = True
    session_days: int = 30
    # Secure cookie flag. Unset = on when the request arrives over HTTPS.
    session_cookie_secure: bool | None = None

    # LLM provider: openai | anthropic | openrouter | mock
    llm_provider: str = "openrouter"
    upload_dir: str = "./data/resumes"
    max_resume_upload_mb: int = 8

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
    followup_days: int = 5
    max_jobs_per_batch: int = 30

    # Workflow workers (app.services.work_queue)
    workflow_max_attempts: int = 3
    workflow_lease_seconds: int = 900
    workflow_retry_base_seconds: int = 60
    workflow_retry_max_seconds: int = 3600

    # In-app scheduler (app.scheduler). Intervals are in minutes; 0 disables that job.
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 30
    scheduler_lease_minutes: int = 30
    hunter_interval_minutes: int = 240
    apify_sync_interval_minutes: int = 5
    ranking_interval_minutes: int = 2
    compensation_interval_minutes: int = 5
    application_interval_minutes: int = 5
    followup_interval_minutes: int = 60
    inbox_interval_minutes: int = 5
    analytics_interval_minutes: int = 60
    cleanup_interval_minutes: int = 15
    ranking_batch_size: int = 20
    ranking_concurrency: int = 5
    compensation_batch_size: int = 50
    # Required header value (X-Admin-Token) for POST /api/admin/scheduler/{job}/run. Unset disables the triggers.
    admin_token: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
