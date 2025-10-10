from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl
from typing import List

class Settings(BaseSettings):
    APP_NAME: str = "YahooFantasyAPI"
    APP_ENV: str = "local"
    SECRET_KEY: str = "dev"   # default fallback

    CORS_ORIGINS: List[AnyHttpUrl] | List[str] = []

    DATABASE_URL: str = "sqlite:///./dev.db"

    YAHOO_CLIENT_ID: str = ""
    YAHOO_CLIENT_SECRET: str = ""
    YAHOO_REDIRECT_URI: str = "http://localhost:8000/auth/callback"
    YAHOO_AUTH_URL: str = "https://api.login.yahoo.com/oauth2/request_auth"
    YAHOO_TOKEN_URL: str = "https://api.login.yahoo.com/oauth2/get_token"
    YAHOO_API_BASE: str = "https://fantasysports.yahooapis.com/fantasy/v2"
    YAHOO_FAKE_MODE: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

# 👇 this is critical
settings = Settings()
