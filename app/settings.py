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

    # Direct Gemini (Google AI Studio / Gemini API key)
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"

    # OpenRouter is OpenAI-compatible. `openrouter/free` automatically routes
    # among currently available free models that can satisfy the request.
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openrouter/free"
    openrouter_model_rank: str | None = None
    # Provider-independent model for the rank stage (any provider). A model saved in Settings still wins.
    rank_model: str | None = None
    openrouter_model_tailor: str | None = None
    openrouter_model_review: str | None = None
    openrouter_model_followup: str | None = None
    openrouter_fallback_models: str = ""
    openrouter_require_free: bool = True
    openrouter_max_tokens: int = 5000
    openrouter_site_url: str | None = None
    openrouter_app_name: str = "Job Hunt OS"
    # Every provider call: request timeout and SDK-level retries (the workers retry on their own too).
    llm_request_timeout_seconds: int = 120
    llm_max_retries: int = 1

    # LLM monitoring (app.services.llm_telemetry). Telemetry stores metadata and token counts, never prompt text,
    # unless LLM_PROMPT_DEBUG is on. Raw request rows older than the retention are rolled up into daily totals.
    llm_telemetry_enabled: bool = True
    llm_telemetry_retention_days: int = 90
    llm_prompt_debug: bool = False
    # Model prices and context windows (app.services.model_catalog). Blank uses app/model_catalog.json.
    model_catalog_file: str | None = None

    # Google ADK / Vertex AI Agent Engine. Authentication and project/location
    # are read by the Google SDK from standard GOOGLE_* environment variables.
    gemini_agent_model: str = "gemini-2.5-flash"

    apify_api_token: str | None = None
    apify_actor_id: str = "openclawai/job-board-scraper"
    apify_dataset_limit: int = 5000

    # Hunter actors (app.services.apify_adapters). Each has its own adapter; IDs are never hard-coded elsewhere.
    apify_primary_job_actor: str = "openclawai/job-board-scraper"
    apify_linkedin_actor: str = "thirdwatch/linkedin-jobs-scraper"
    apify_indeed_actor: str = "crawlerbros/indeed-jobs-scraper"
    apify_glassdoor_actor: str = "crawlerbros/glassdoor-jobs-scraper"
    apify_naukri_actor: str = "crawlerbros/naukri-scraper"
    # When a multi-board run fails, search its boards again with their dedicated actors (once).
    apify_auto_fallback: bool = True
    apify_request_timeout_seconds: int = 30
    hunter_max_runs_per_search: int = 30
    apify_sync_batch_size: int = 25

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
    # AI Optimization Advisor (app.services.ai_advisor): processes queued and scheduled analyses.
    ai_optimization_interval_minutes: int = 5
    ranking_batch_size: int = 20
    ranking_concurrency: int = 5
    # Job Ranker (app.services.ranker)
    min_ranking_confidence: float = 0.6
    ranking_max_attempts: int = 3
    ranking_lease_seconds: int = 900
    ranking_retry_base_seconds: int = 60
    ranking_retry_max_seconds: int = 3600
    # How long a task waits before trying again when the account cannot be ranked yet (missing key or resume).
    ranking_wait_seconds: int = 900
    ranking_enqueue_batch_size: int = 200
    ranking_max_evidence: int = 40
    # Structured job descriptions (app.services.job_description)
    jd_parse_enabled: bool = True
    jd_parse_interval_minutes: int = 3
    jd_parse_batch_size: int = 10
    jd_parse_concurrency: int = 3
    jd_parse_enqueue_batch_size: int = 100
    jd_parse_lease_seconds: int = 600
    jd_parse_max_attempts: int = 3
    jd_parse_retry_seconds: int = 120
    # Re-rank qualified jobs once their description has been parsed. Off by default: parsing a backlog would
    # otherwise start a round of ranking calls.
    jd_rerank_after_parse: bool = False
    # Re-rank qualified jobs automatically when the profile, the active resume or the ranking prompt changes.
    ranking_auto_rerank: bool = True
    compensation_batch_size: int = 50
    # Background tailoring worker (app.services.tailor_tasks)
    tailor_worker_enabled: bool = True
    tailor_concurrency: int = 2
    tailor_lease_seconds: int = 900
    # Claude Desktop MCP server (app.mcp): the most jobs one context request or bulk save may cover.
    mcp_bulk_tailor_limit: int = 10
    mcp_bulk_review_limit: int = 20
    # The token `python -m app.mcp` authenticates with. The process environment wins over .env.
    jobhunt_mcp_token: str | None = None
    # Required header value (X-Admin-Token) for POST /api/admin/scheduler/{job}/run. Unset disables the triggers.
    admin_token: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
