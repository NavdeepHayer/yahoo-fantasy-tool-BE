# app/core/config.py
from __future__ import annotations

import json
import base64
from typing import List, Literal, Optional

from pydantic import Field, field_validator, ValidationInfo
from pydantic_settings import BaseSettings, SettingsConfigDict


EnvType = Literal["local", "dev", "staging", "prod"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    APP_NAME: str = "YahooFantasyAPI"
    APP_ENV: EnvType = "local"
    SECRET_KEY: str = Field(default="change_me_dev_only", description="Used for session signing")
    ENCRYPTION_KEY: str  # required; Fernet key

    # CORS
    CORS_ORIGINS: str | List[str] = Field(
        default='["http://localhost:5173","http://127.0.0.1:5173"]',
        description='JSON list or comma-separated origins',
    )

    # DB
    DATABASE_URL: Optional[str] = None

    FRONTEND_URL_LOCAL: str = "http://localhost:5173"
    FRONTEND_URL_REMOTE: str = "https://fantasy.navdeephayer.com"
    API_URL_LOCAL: str = "http://127.0.0.1:8001"
    API_URL_REMOTE: str = "https://api.mynbaassistant.com"

    @property
    def frontend_url(self) -> str:
        return self.FRONTEND_URL_REMOTE if self.APP_ENV != "local" else self.FRONTEND_URL_LOCAL

    @property
    def api_url(self) -> str:
        return self.API_URL_REMOTE if self.APP_ENV != "local" else self.API_URL_LOCAL

    # Yahoo OAuth
    YAHOO_CLIENT_ID: Optional[str] = None
    YAHOO_CLIENT_SECRET: Optional[str] = None
    YAHOO_REDIRECT_URI: Optional[str] = None
    YAHOO_AUTH_URL: str = "https://api.login.yahoo.com/oauth2/request_auth"
    YAHOO_TOKEN_URL: str = "https://api.login.yahoo.com/oauth2/get_token"
    YAHOO_API_BASE: str = "https://fantasysports.yahooapis.com/fantasy/v2"

    # Dev toggle
    YAHOO_FAKE_MODE: bool = False

    # Derived / convenience flags
    @property
    def IS_LOCAL(self) -> bool:
        return self.APP_ENV == "local"

    @property
    def COOKIE_SECURE(self) -> bool:
        # Secure cookies in any non-local environment
        return not self.IS_LOCAL

    # ---------- Validators ----------

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        # Fernet requires 32-byte urlsafe base64-encoded key
        try:
            # allow quoted values from .env
            raw = v.strip().strip('"').strip("'")
            decoded = base64.urlsafe_b64decode(raw + "===")  # tolerate missing padding
            if len(decoded) != 32:
                raise ValueError
            return raw
        except Exception as _:
            raise ValueError(
                "ENCRYPTION_KEY must be a 32-byte urlsafe base64-encoded Fernet key "
                "(generate with: from cryptography.fernet import Fernet; print(Fernet.generate_key().decode()))"
            )

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _parse_cors(cls, v):
        # Accept JSON list or comma-separated string
        if isinstance(v, list):
            return v
        s = str(v).strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        # fallback: comma-separated
        return [p.strip() for p in s.split(",") if p.strip()]

    # ---------- Runtime validations ----------

    def validate_at_startup(self) -> None:
        """Fail fast with clear messages for misconfigurations."""
        problems: list[str] = []

        # Require DB in all envs (you use Neon in dev/staging/prod)
        if not self.DATABASE_URL:
            problems.append("DATABASE_URL is required.")

        # Yahoo credentials required outside local (where you may use FAKE_MODE)
        if not self.IS_LOCAL:
            if not self.YAHOO_CLIENT_ID:
                problems.append("YAHOO_CLIENT_ID is required in non-local env.")
            if not self.YAHOO_CLIENT_SECRET:
                problems.append("YAHOO_CLIENT_SECRET is required in non-local env.")
            if not self.YAHOO_REDIRECT_URI:
                problems.append("YAHOO_REDIRECT_URI is required in non-local env.")

        # CORS must not be empty outside local
        if not self.IS_LOCAL and not self.CORS_ORIGINS:
            problems.append("CORS_ORIGINS must contain at least one allowed origin in non-local env.")

        if problems:
            # Collapse to one helpful error line
            raise RuntimeError("Config validation failed: " + " ".join(problems))


settings = Settings()
