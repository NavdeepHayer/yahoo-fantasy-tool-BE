# app/main.py
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api import routes_auth, routes_me, routes_league
import json, re
from app.api.routes_debug import router as debug_router
from app.middleware.cache_log import CacheHeaderLogMiddleware

app = FastAPI(title=settings.APP_NAME)
app.add_middleware(CacheHeaderLogMiddleware)

def _parse_origins(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("["):
            try:
                return json.loads(v)
            except Exception:
                pass
        return [v] if v else []
    return []

# Prefer explicit whitelist in local
ALLOWED_ORIGINS = _parse_origins(getattr(settings, "CORS_ORIGINS", []))
print("CORS allow_origins =", ALLOWED_ORIGINS)  # keep while debugging

# 1) Starlette CORS — use regex to match localhost/127.0.0.1:5173 precisely
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,                 # fine for prod (mynbaassistant.com)
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1):5173$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
)

# 2) Safety net: override wildcard headers set by a proxy (e.g., ngrok)
@app.middleware("http")
async def _cors_override(request: Request, call_next):
    resp = await call_next(request)
    origin = request.headers.get("origin") or ""
    if re.match(r"^https?://(localhost|127\.0\.0\.1):5173$", origin):
        # If some upstream set '*', replace it with the exact origin so cookies are allowed
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers.setdefault("Vary", "Origin")
    return resp

# Routers
app.include_router(routes_auth.router)
app.include_router(routes_me.router)
app.include_router(routes_league.router)
app.include_router(debug_router)


@app.get("/health")
def health():
    return {"ok": True, "env": settings.APP_ENV}


