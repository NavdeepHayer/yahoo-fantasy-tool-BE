from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, Integer, DateTime, func

class Base(DeclarativeBase):
    pass

class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # Simple "dev user" identifier. Later, replace with real users table
    user_id: Mapped[str] = mapped_column(String(128), index=True)

    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON as str for debugging

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

class User(Base):
    __tablename__ = "users"
    # Yahoo GUID as the stable primary key
    guid: Mapped[str] = mapped_column(String(64), primary_key=True)
    nickname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), onupdate=func.now(), nullable=True)