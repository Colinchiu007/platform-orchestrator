"""Phase B: SQLAlchemy ORM models — auth tables (users, refresh_tokens, subscriptions).

These replace the aiosqlite raw-SQL tables with the same schema.
All models use auth schema — shared with trendscope on same PG instance.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _utcnow():
    """Return naive UTC datetime for DB columns defined as TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.utcnow()


class AuthUser(Base):
    """User accounts — corresponds to old SQLite 'users' table."""
    __tablename__ = "users"
    __table_args__ = {"schema": "auth"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, nullable=False, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True)
    password_hash = Column(String(128), nullable=False)
    subscription_type = Column(String(20), default="free", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    def __repr__(self):
        return f"<AuthUser {self.username} ({self.uuid})>"


class AuthRefreshToken(Base):
    """JWT refresh tokens — corresponds to old SQLite 'refresh_tokens' table."""
    __tablename__ = "refresh_tokens"
    __table_args__ = {"schema": "auth"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_jti = Column(String(36), unique=True, nullable=False, index=True)
    user_uuid = Column(String(36), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


class AuthSubscription(Base):
    """User subscription plans — corresponds to old SQLite 'subscriptions' table."""
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("user_uuid"),
        {"schema": "auth"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uuid = Column(String(36), unique=True, nullable=False, index=True)
    plan_type = Column(String(20), default="free", nullable=False)
    status = Column(String(20), default="active")
    start_date = Column(DateTime)
    end_date = Column(DateTime)
    auto_renew = Column(Boolean, default=False)
