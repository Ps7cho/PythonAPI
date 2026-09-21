from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    app_name: str = "Game Event API"
    database_url: str
    max_characters_per_account: int = Field(default=20, ge=1)
    public_client_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    discord_client_id: str | None = None
    discord_client_secret: str | None = None
    discord_redirect_uri: str = "http://127.0.0.1:8000/api/auth/discord/callback"
    discord_login_redirect_uri: str = "http://127.0.0.1:8000/api/auth/discord/login/callback"
    discord_frontend_redirect_uri: str = "http://127.0.0.1:5173/index.html"

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
