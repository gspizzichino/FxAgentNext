"""Deeply immutable containers (ADR-0012)."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

K = TypeVar("K")
V = TypeVar("V")

_SCALARS = (type(None), bool, int, float, str, Decimal)


class FrozenMap(Mapping[K, V], Generic[K, V]):
    """Immutable, hashable mapping iterated in sorted key order.

    The constructor copies its input; no reference to the internal dict escapes.
    Values must themselves be immutable (checked by ``is_immutable``).
    """

    __slots__ = ("_data", "_hash")
    _data: dict[K, V]
    _hash: int | None

    def __init__(self, data: Mapping[K, V] | None = None) -> None:
        items = dict(data or {})
        for value in items.values():
            if not is_immutable(value):
                raise TypeError(f"FrozenMap value is mutable: {type(value).__name__}")
        try:
            ordered = dict(sorted(items.items(), key=lambda kv: kv[0]))  # type: ignore[arg-type,return-value]
        except TypeError as error:
            raise TypeError("FrozenMap keys must be mutually comparable") from error
        object.__setattr__(self, "_data", ordered)
        object.__setattr__(self, "_hash", None)

    def __getitem__(self, key: K) -> V:
        return self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        cached: int | None = self._hash
        if cached is None:
            cached = hash(tuple(self._data.items()))
            object.__setattr__(self, "_hash", cached)
        return cached

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenMap):
            return bool(self._data == other._data)
        if isinstance(other, Mapping):
            return bool(self._data == dict(other))
        return NotImplemented

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("FrozenMap is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("FrozenMap is immutable")

    def __repr__(self) -> str:
        return f"FrozenMap({self._data!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenMap, (dict(self._data),))

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            lambda value: value if isinstance(value, FrozenMap) else FrozenMap(value)
        )


def is_immutable(value: object) -> bool:
    """Shallow-recursive immutability check used for container values."""

    from fxnext.domain.series import BarSeries  # local import: avoid cycle

    if isinstance(value, _SCALARS):
        return True
    if isinstance(value, tuple | frozenset):
        return all(is_immutable(item) for item in value)
    if isinstance(value, FrozenMap | BarSeries):
        return True
    # Enum members, datetimes and frozen pydantic models are immutable values.
    from datetime import date, datetime, timedelta
    from enum import Enum

    from pydantic import BaseModel

    if isinstance(value, Enum | datetime | date | timedelta):
        return True
    return bool(isinstance(value, BaseModel) and value.model_config.get("frozen"))


def freeze_json(value: Any) -> Any:
    """Convert JSON-like data into FrozenJson: dict -> FrozenMap, list -> tuple."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("FrozenJson rejects NaN and infinities")
        return value
    if isinstance(value, FrozenMap):
        return FrozenMap({_json_key(k): freeze_json(v) for k, v in value.items()})
    if isinstance(value, Mapping):
        return FrozenMap({_json_key(k): freeze_json(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    raise ValueError(f"Not a JSON value: {type(value).__name__}")


def _json_key(key: object) -> str:
    if not isinstance(key, str):
        raise ValueError("FrozenJson keys must be str")
    return key
