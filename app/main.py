from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.db.engine import engine
from app.db.models import Base
from app.api.routes_auth import router as auth_router
from app.api.routes_me import router as me_router
from app.api.routes_league import router as league_router

app = FastAPI(title=settings.APP_NAME)

# Create tables on startup (SQLite dev convenience)
Base.metadata.create_all(bind=engine)

# CORS (dev-friendly)
origins = [*settings.CORS_ORIGINS] if settings.CORS_ORIGINS else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"hello": "world"}

@app.get("/health")
def health():
    return {"ok": True, "env": settings.APP_ENV}



@app.on_event("startup")
def startup_event():
    Base.metadata.create_all(bind=engine)

# Routers
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(league_router)


