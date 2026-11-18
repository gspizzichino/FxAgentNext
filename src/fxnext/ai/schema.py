"""Closed schema of an AI judgement (proposal m7, section 4).

The model answers with ``ModelAnswer`` only. Fields the model must not decide
(time, currency set, tone change, source hashes) are filled in by the code.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CURRENCIES = ("EUR", "GBP", "JPY", "USD")
BANK_OF = {"USD": "fed", "EUR": "ecb", "GBP": "boe", "JPY": "boj"}
RISK_LEVELS = ("basso", "medio", "alto")
Kind = Literal["daily", "event"]


class CurrencyView(BaseModel):
    """What the model writes for one currency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: Literal["EUR", "GBP", "JPY", "USD"]
    tone: int = Field(ge=-2, le=2)
    surprise: int | None = Field(default=None, ge=-2, le=2)
    event_risk_24h: Literal["basso", "medio", "alto"]
    bias_5d: int = Field(ge=-1, le=1)
    confidence: float = Field(ge=0.0, le=1.0)
    sources: tuple[str, ...]
    rationale: str = Field(max_length=700)

    @field_validator("rationale")
    @classmethod
    def _short(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("rationale vuota")
        return v


class ModelAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    judgements: tuple[CurrencyView, ...]


def _int_enum(lo: int, hi: int) -> dict[str, Any]:
    return {"type": "integer", "enum": list(range(lo, hi + 1))}


def answer_json_schema() -> dict[str, Any]:
    """JSON schema sent to the API (structured outputs: no numeric bounds, enums instead)."""

    view = {
        "type": "object",
        "properties": {
            "currency": {"type": "string", "enum": list(CURRENCIES)},
            "tone": _int_enum(-2, 2),
            "surprise": {"anyOf": [_int_enum(-2, 2), {"type": "null"}]},
            "event_risk_24h": {"type": "string", "enum": list(RISK_LEVELS)},
            "bias_5d": _int_enum(-1, 1),
            "confidence": {"type": "number"},
            "sources": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": [
            "currency",
            "tone",
            "surprise",
            "event_risk_24h",
            "bias_5d",
            "confidence",
            "sources",
            "rationale",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"judgements": {"type": "array", "items": view}},
        "required": ["judgements"],
        "additionalProperties": False,
    }


def check_answer(answer: ModelAnswer, kind: Kind, expected: tuple[str, ...], ids: set[str]) -> None:
    """Rules the JSON schema cannot express. Raises ValueError."""

    got = tuple(sorted(v.currency for v in answer.judgements))
    if got != tuple(sorted(expected)):
        raise ValueError(f"valute attese {sorted(expected)}, ricevute {list(got)}")
    for v in answer.judgements:
        if kind == "daily" and v.surprise is not None:
            raise ValueError(f"{v.currency}: surprise vale solo dopo una decisione")
        if kind == "event" and v.surprise is None:
            raise ValueError(f"{v.currency}: surprise mancante dopo una decisione")
        unknown = set(v.sources) - ids
        if unknown:
            raise ValueError(f"{v.currency}: fonti sconosciute {sorted(unknown)}")


__all__ = [
    "BANK_OF",
    "CURRENCIES",
    "CurrencyView",
    "Kind",
    "ModelAnswer",
    "answer_json_schema",
    "check_answer",
]
