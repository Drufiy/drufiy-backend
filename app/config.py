from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_service_key: str

    # Kimi / Moonshot (OpenAI-compatible)
    # Direct Moonshot: KIMI_BASE_URL=https://api.moonshot.cn/v1  KIMI_MODEL=kimi-k2-5
    # OpenRouter:      KIMI_BASE_URL=https://openrouter.ai/api/v1  KIMI_MODEL=moonshotai/kimi-k2.6
    kimi_api_key: str
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "kimi-k2-5"

    # Fallback model
    anthropic_api_key: str | None = None

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

    # Environment
    env: str = "development"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
