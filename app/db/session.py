# app/db/session.py
from typing import Iterator
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from app.db.engine import SessionLocal, engine

def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        # if you only ever read, this commit is a no-op; if you write in some routes, it commits
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    finally:
        try:
            db.close()
        except OperationalError:
            # underlying socket already dead; dispose pool to force fresh conns next time
            try:
                engine.dispose()
            except Exception:
                pass
        except Exception:
            try:
                engine.dispose()
            except Exception:
                pass
