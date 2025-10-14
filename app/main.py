# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api import routes_auth, routes_me, routes_league

import json

app = FastAPI(title=settings.APP_NAME)

# --- Strict CORS setup ---
def _parse_origins(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("["):
            # value came from .env as a JSON-ish list
            try:
                return json.loads(v)
            except Exception:
                pass
        # single origin string
        return [v] if v else []
    return []

ALLOWED_ORIGINS = _parse_origins(getattr(settings, "CORS_ORIGINS", []))

# Fail fast if empty in non-local envs
# (optional; comment out if you want the API callable from any origin-less client like curl)
# if settings.APP_ENV.lower() != "local" and not ALLOWED_ORIGINS:
#     raise RuntimeError("CORS_ORIGINS must be set in non-local environments")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,             # needed if your frontend ever sends cookies
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Requested-With",
        "X-User-Id",                    # if you keep using this in dev
    ],
    max_age=600,                        # cache preflight for 10 minutes
)

# Routers
app.include_router(routes_auth.router)
app.include_router(routes_me.router)
app.include_router(routes_league.router)

@app.get("/health")
def health():
    return {"ok": True, "env": settings.APP_ENV}
