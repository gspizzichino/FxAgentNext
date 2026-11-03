"""Base model: frozen, strict, no extra fields, UTC-only datetimes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict

from fxnext.domain.frozen import freeze_json
from fxnext.domain.timeutil import ensure_utc


class Model(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True
    )
    # Field names excluded from the content hash (e.g. the hash field itself).
    hash_exclude: ClassVar[frozenset[str]] = frozenset()


UtcDatetime = Annotated[AwareDatetime, AfterValidator(ensure_utc)]
FrozenJson = Annotated[Any, BeforeValidator(freeze_json)]

__all__ = ["FrozenJson", "Model", "UtcDatetime", "datetime"]
