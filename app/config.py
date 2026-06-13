from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_service_key: str

    # Kimi (OpenAI-compatible)
    kimi_api_key: str
    kimi_base_url: str = "https://api.moonshot.ai/v1"
    kimi_model: str = "kimi-k2.6"  # Moonshot's latest model

    # DeepSeek (fallback + consensus)
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-coder"
    kimi_input_price_per_1m_tokens: float | None = 0.60
    kimi_output_price_per_1m_tokens: float | None = 2.50
    deepseek_input_price_per_1m_tokens: float | None = None
    deepseek_output_price_per_1m_tokens: float | None = None

    # Generic fallback model (OpenAI-compatible — provider-agnostic)
    fallback_enabled: bool = False
    fallback_api_key: str | None = None
    fallback_base_url: str | None = None
    fallback_model: str | None = None
    fallback_input_price_per_1m_tokens: float | None = None
    fallback_output_price_per_1m_tokens: float | None = None

    # GitHub
    github_client_id: str
    github_client_secret: str
    github_webhook_secret: str
    github_app_id: str | None = None
    github_app_slug: str | None = None
    github_app_private_key: str | None = None

    # JWT
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 168
    token_encryption_key: str | None = None
    oauth_state_required: bool = False
    oauth_state_expiry_minutes: int = 15

    # Webhooks / automation
    webhook_rate_limit_max: int = 60
    webhook_rate_limit_window_seconds: int = 3600
    fix_branch_prefix: str = "prash/fix-run-"

    # URLs
    frontend_url: str = "http://localhost:3000"
    public_backend_url: str = "http://localhost:8000"

    # Slack (optional — internal ops alerts, not user-facing)
    slack_webhook_url: str | None = None

    # Email (Resend — optional, used for weekly health reports)
    resend_api_key: str | None = None
    report_email_from: str = "Drufiy <reports@drufiy.com>"

    # Internal cron secret (used by Cloud Scheduler to authenticate internal endpoints)
    internal_cron_secret: str | None = None

    # Environment
    env: str = "development"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
