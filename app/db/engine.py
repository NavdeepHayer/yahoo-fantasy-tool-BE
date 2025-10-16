import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# load .env for local dev
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")  # prefer pooled URL for Neon/Render

connect_args = {}

engine = create_engine(
    DATABASE_URL or "sqlite:///./app.db",
    pool_pre_ping=True,
    pool_size=10,       # friendly to free Postgres tiers
    max_overflow=10,
    pool_timeout=5,
    echo=False,
    future=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    future=True,
)
