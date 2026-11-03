"""Canonical encoding and content hashes, schema ``fxnext-hash-v1`` (ADR-0013)."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from fxnext.domain.timeutil import to_ns

HASH_SCHEMA = "fxnext-hash-v1"
_PREFIX = HASH_SCHEMA.encode("ascii") + b"\x00"


def canonical_value(value: Any) -> Any:
    """Transform a value into plain JSON with lossless, platform-independent encoding."""

    from fxnext.domain.series import BarSeries  # local import: avoid cycle

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN/inf cannot be hashed")
        return "f:" + (0.0 if value == 0.0 else value).hex()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be hashed")
        return "d:" + format(value.normalize(), "f")
    if isinstance(value, datetime):
        return "t:" + str(to_ns(value))
    if isinstance(value, date):
        return "D:" + value.isoformat()
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bytes):
        return "b:" + hashlib.sha256(value).hexdigest()
    if isinstance(value, BarSeries):
        return value.canonical()
    if isinstance(value, BaseModel):
        exclude: frozenset[str] = getattr(type(value), "hash_exclude", frozenset())
        return {
            name: canonical_value(getattr(value, name))
            for name in type(value).model_fields
            if name not in exclude
        }
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            ckey = canonical_value(key)
            if not isinstance(ckey, str):
                ckey = json.dumps(ckey, separators=(",", ":"))
            out[ckey] = canonical_value(item)
        return out
    if isinstance(value, tuple | list):
        return [canonical_value(item) for item in value]
    if isinstance(value, frozenset | set):
        items = [canonical_value(item) for item in value]
        return sorted(items, key=lambda item: canonical_bytes_of(item))
    raise TypeError(f"cannot hash value of type {type(value).__name__}")


def canonical_bytes_of(canon: Any) -> bytes:
    return json.dumps(
        canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def canonical_bytes(value: Any) -> bytes:
    return canonical_bytes_of(canonical_value(value))


def content_hash(type_tag: str, value: Any) -> str:
    """sha256(b"fxnext-hash-v1\\0" + tag + b"\\0" + canon(value))."""

    if not type_tag or "\x00" in type_tag:
        raise ValueError("invalid type tag")
    digest = hashlib.sha256(_PREFIX + type_tag.encode("ascii") + b"\x00")
    digest.update(canonical_bytes(value))
    return digest.hexdigest()


def chain_start(run_fingerprint: str) -> str:
    return content_hash("RunOutput/1", run_fingerprint)


def chain_step(head: str, request_id: str, evaluation_hash: str) -> str:
    return hashlib.sha256(
        bytes.fromhex(head) + bytes.fromhex(request_id) + bytes.fromhex(evaluation_hash)
    ).hexdigest()
