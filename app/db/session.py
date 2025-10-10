from contextlib import contextmanager
from sqlalchemy.orm import Session
from app.db.engine import SessionLocal
from typing import Iterator

def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
