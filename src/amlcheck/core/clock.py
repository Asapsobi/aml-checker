"""Time in one form everywhere: timezone-aware UTC, stored as ISO-8601 to the second with a Z."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def from_timestamp(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)
