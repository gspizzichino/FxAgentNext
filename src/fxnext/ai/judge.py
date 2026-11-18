"""The call to Claude, behind a small port so tests use a fake.

The ``anthropic`` package is imported lazily (optional dependency ``ai``). The
API key is read by the SDK from ``ANTHROPIC_API_KEY`` on the PC; this module
never sees it written anywhere.

No server-side fallback to another model is enabled on purpose: a judgement
written by a different model would mix two series. A refusal is a failed run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 8_000
# USD per million tokens, list price on 2026-11-18 (check before changing the model)
PRICES = {"claude-opus-5-5": (4.0, 20.0)}


@dataclass(frozen=True)
class RawAnswer:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str


class JudgeError(RuntimeError):
    """The run failed. ``answer`` is set when tokens were spent anyway (refusal, cut off)."""

    def __init__(self, message: str, answer: RawAnswer | None = None) -> None:
        super().__init__(message)
        self.answer = answer


class Judge(Protocol):
    model: str

    def ask(self, system: str, user: str, schema: dict[str, Any]) -> RawAnswer: ...


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def worst_case_usd(model: str, system: str, user: str) -> float:
    """Upper bound before the call: about 3 characters per token, full output budget."""

    return cost_usd(model, (len(system) + len(user)) // 3 + 1, MAX_TOKENS)


class AnthropicJudge:
    def __init__(self, model: str = MODEL, client: Any = None) -> None:
        if model not in PRICES:
            raise JudgeError(f"prezzo sconosciuto per {model}: aggiorna PRICES")
        self.model = model
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on the PC
                raise JudgeError("pacchetto anthropic non installato (uv sync --extra ai)") from exc
            client = anthropic.Anthropic(max_retries=2, timeout=300.0)
        self.client = client

    def ask(self, system: str, user: str, schema: dict[str, Any]) -> RawAnswer:
        try:
            res = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": EFFORT,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except Exception as exc:  # noqa: BLE001 - any SDK/network error is a failed run
            raise JudgeError(f"chiamata non riuscita: {type(exc).__name__}: {exc}") from None
        stop = str(getattr(res, "stop_reason", ""))
        usage = res.usage
        text = "".join(b.text for b in res.content if getattr(b, "type", "") == "text")
        answer = RawAnswer(
            text,
            str(getattr(res, "model", self.model)),
            int(usage.input_tokens),
            int(usage.output_tokens),
            stop,
        )
        if stop in ("refusal", "max_tokens"):
            raise JudgeError(f"risposta non utilizzabile (stop_reason={stop})", answer)
        return answer


__all__ = [
    "EFFORT",
    "MAX_TOKENS",
    "MODEL",
    "PRICES",
    "AnthropicJudge",
    "Judge",
    "JudgeError",
    "RawAnswer",
    "cost_usd",
    "worst_case_usd",
]
