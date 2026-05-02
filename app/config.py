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
