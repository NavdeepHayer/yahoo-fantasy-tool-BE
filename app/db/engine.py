import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Load .env for local dev
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")  # e.g. postgresql+psycopg2://user:pass@host/dbname

# Add SSL + TCP keepalive args only for Postgres (not SQLite)
connect_args = {}
if DATABASE_URL and DATABASE_URL.startswith("postgresql"):
    connect_args = {
        "sslmode": "require",      # ensure SSL stays active on hosted DBs (Neon, Render, etc.)
        "keepalives": 1,
        "keepalives_idle": 30,     # seconds before starting keepalives
        "keepalives_interval": 10, # seconds between keepalives
        "keepalives_count": 5,     # number of failed keepalives before drop
    }

engine = create_engine(
    DATABASE_URL or "sqlite:///./app.db",
    pool_pre_ping=True,     # automatically tests and replaces stale conns
    pool_recycle=300,       # recycles every 5 min to beat provider idle timeout
    pool_size=10,           # fine for Neon free tier
    max_overflow=10,
    pool_timeout=10,
    echo=False,
    future=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
    future=True,
)
