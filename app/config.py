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
    kimi_input_price_per_1m_tokens: float | None = None
    kimi_output_price_per_1m_tokens: float | None = None
    deepseek_input_price_per_1m_tokens: float | None = None
    deepseek_output_price_per_1m_tokens: float | None = None

    # GitHub
    github_client_id: str
    github_client_secret: str
    github_webhook_secret: str

    # JWT
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 168

    # URLs
    frontend_url: str = "http://localhost:3000"
    public_backend_url: str = "http://localhost:8000"

    # Email (Resend — optional, used for weekly health reports)
    resend_api_key: str | None = None
    report_email_from: str = "Drufiy <reports@drufiy.com>"

    # Internal cron secret (used by Cloud Scheduler to authenticate internal endpoints)
    internal_cron_secret: str | None = None

    # Environment
    env: str = "development"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
