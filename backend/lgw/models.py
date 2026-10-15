"""Users, API keys and forwarded request records."""

# ORM classes expose columns rather than methods.
# pylint: disable=too-few-public-methods

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    """Return the current time in UTC."""
    return datetime.now(UTC)


def ensure_utc(value):
    """Return ``value`` as a timezone-aware UTC datetime.

    SQLite drops timezone information, so values read back may be
    naive. Those values are treated as UTC.

    Parameters
    ----------
    value : datetime.datetime
        Datetime stored on a model instance.

    Returns
    -------
    datetime.datetime
        Timezone-aware UTC datetime.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class KeyStatus(enum.StrEnum):
    """Whether a gateway API key can authenticate requests."""

    ACTIVE = "active"
    DISABLED = "disabled"


class RequestStatus(enum.StrEnum):
    """Outcome of one forwarded request."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    UPSTREAM_INTERRUPTED = "upstream_interrupted"
    CLIENT_INTERRUPTED = "client_interrupted"
    UPSTREAM_ERROR = "upstream_error"
    GATEWAY_ERROR = "gateway_error"


class UsageSource(enum.StrEnum):
    """Where the stored token counts came from."""

    REPORTED = "reported"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class Base(DeclarativeBase):
    """Declarative base for gateway tables."""


class User(Base):
    """Person who owns gateway API keys.

    ``sub`` is also sent to DeepSeek as ``user_id``.
    """

    __tablename__ = "users"

    sub: Mapped[str] = mapped_column(String(512), primary_key=True)
    username: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ApiKey(Base):
    """Gateway API key. The raw key is never stored."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_sub: Mapped[str] = mapped_column(ForeignKey("users.sub"))
    name: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    key_hint: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32))


class Request(Base):
    """One upstream call made with a gateway API key."""

    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_started_at", "started_at"),
        Index("ix_requests_key_id_started_at", "key_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32))
    usage_source: Mapped[str] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column()
    output_tokens: Mapped[int] = mapped_column()
    cache_hit_tokens: Mapped[int] = mapped_column()
