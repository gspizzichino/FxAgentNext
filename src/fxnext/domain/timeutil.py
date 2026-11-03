"""UTC time helpers. All internal instants are aware UTC datetimes or int ns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = timedelta(microseconds=1)


def to_ns(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("naive datetime rejected")
    return ((value - EPOCH) // _US) * 1000


def from_ns(ns: int) -> datetime:
    if ns % 1000:
        raise ValueError("sub-microsecond instants are not representable as datetime")
    return EPOCH + timedelta(microseconds=ns // 1000)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime rejected")
    return value.astimezone(UTC)
