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

    # LLM provider: litellm | openai | anthropic | openrouter | gemini | mock. `litellm` is gateway mode: every
    # worker's model comes from Settings → AI Models and every request goes through the LiteLLM proxy.
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
    # The same for the other stages. In LiteLLM gateway mode they seed the matching workers' model assignments
    # the first time an account's AI model configuration is created (app.services.ai_model_config).
    tailor_model: str | None = None
    review_model: str | None = None
    followup_model: str | None = None
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

    # LiteLLM gateway (Settings → AI Models). These are bootstrap values: an account's saved connection wins.
    # LITELLM_API_KEY is copied (encrypted) into the FIRST account only, like the provider keys above.
    litellm_base_url: str | None = None
    litellm_api_key: str | None = None
    # Used when a worker has no usable assignment yet (for example a tier nobody has mapped to a model).
    litellm_default_model: str | None = None
    # Seed values for the default tiers, as LiteLLM model names (aliases), e.g. tier-1-fast. Blank = unmapped.
    litellm_tier_fast: str | None = None
    litellm_tier_primary: str | None = None
    litellm_tier_deep: str | None = None
    litellm_auto_router_model: str | None = None
    # How Job Hunt OS applies the parts of its configuration that live inside LiteLLM (managed deployments and
    # the auto router): admin_api (needs STORE_MODEL_IN_DB=True on the proxy), config_file, or disabled.
    litellm_sync_mode: str = "disabled"
    # config_file mode: the one file Job Hunt OS owns. The proxy's own config must `include:` it; Job Hunt OS
    # never edits that main config. Operator-level on purpose: a user cannot point the app at another path.
    litellm_managed_config_path: str | None = None
    # Optional: the main proxy config, read (never written) to check that it includes the managed file.
    litellm_config_path: str | None = None
    # Optional command that restarts the proxy after the managed file changes (split like a shell, not run by
    # one). Without it a file change waits for a manual restart and shows as PENDING until verified.
    litellm_restart_command: str | None = None
    litellm_restart_wait_seconds: int = 60
    # The local Ollama server whose models the "Add local Ollama model" form lists. Operator-level: the app only
    # ever fetches this URL, never one a user types in.
    ollama_base_url: str = "http://localhost:11434"
    # Management calls (discovery, sync, verification); inference uses llm_request_timeout_seconds.
    litellm_admin_timeout_seconds: int = 15
    # How long a worker's resolved model configuration is cached in-process. Every applied change clears the
    # cache at once in this process; other processes pick it up within this many seconds.
    ai_config_cache_seconds: int = 30

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

    # Rate-limit backoff (app.services.backoff): after a provider says 429, its calls are held off for a while
    # instead of being retried straight into another rejection. Each account can adjust this in Settings.
    rate_limit_backoff_enabled: bool = True
    rate_limit_base_seconds: int = 30
    rate_limit_multiplier: float = 2.0
    rate_limit_max_seconds: int = 900
    # The longest a provider's own Retry-After is obeyed; beyond this the cap above applies instead.
    rate_limit_retry_after_max_seconds: int = 3600

    # External API monitoring (app.services.external_api). Every third-party call is recorded automatically;
    # raw rows are rolled into daily totals once they pass the retention, and operational log lines are deleted.
    external_api_telemetry_enabled: bool = True
    external_api_log_retention_days: int = 90
    system_log_retention_days: int = 30
    # Off by design: bodies can hold a candidate's resume or an employer's posting. When it is on, extracts are
    # still redacted and capped at external_api_payload_limit bytes.
    external_api_debug_payloads: bool = False
    external_api_payload_limit: int = 2000
    # A call slower than this is logged as a warning in Monitoring → Logs, so slowdowns are visible without a query.
    external_api_slow_call_ms: int = 10_000
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
    # Imports the auto router's decisions (tier and cause) from LiteLLM's spend logs into Monitoring.
    litellm_telemetry_interval_minutes: int = 10
    # A posting with no real description cannot be ranked honestly: the model would be scoring a job title.
    # Postings shorter than this (and with no parsed structure) wait instead, and are ranked if a description
    # arrives later. Several boards — Naukri and Bayt in particular — return listings without one.
    ranking_min_description_chars: int = 200
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
    # Applying from Claude Desktop: the most jobs one list_jobs_for_application page returns, and where
    # get_job_packet writes the approved resume files for an ATS upload (one folder per account and job).
    mcp_application_page_limit: int = 50
    mcp_packet_dir: str = "./data/packets"
    # The token `python -m app.mcp` authenticates with. The process environment wins over .env.
    jobhunt_mcp_token: str | None = None
    # Required header value (X-Admin-Token) for POST /api/admin/scheduler/{job}/run. Unset disables the triggers.
    admin_token: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
