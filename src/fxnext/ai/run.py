"""One shadow AI run: build the context, ask the model, check, append, notify.

Whatever happens, one line is appended to the store: ``ok``, ``failed`` (the day
stays empty and says why) or ``skipped`` (spending cap). Nothing here can reach
the demo engine: import-linter forbids ``fxnext.execution`` and ``fxnext.ops``
(apart from the Telegram helper) from depending on this package and vice versa.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from fxnext.ai.context import Trigger, collect_sources, pending_triggers, user_message
from fxnext.ai.judge import Judge, JudgeError, RawAnswer, cost_usd, worst_case_usd
from fxnext.ai.prompt import PROMPT_SHA, PROMPT_VERSION, SYSTEM_PROMPT
from fxnext.ai.schema import CURRENCIES, Kind, ModelAnswer, answer_json_schema, check_answer
from fxnext.ai.store import AiStore

LOCAL_TZ = ZoneInfo("Europe/Rome")
CAP_USD = 10.0
EVENT_WINDOW = timedelta(hours=36)
ARROW = {-1: "↓", 0: "→", 1: "↑"}

Notify = Callable[[str], None]


@dataclass(frozen=True)
class RunResult:
    status: str  # ok | failed | skipped
    record: dict[str, Any]
    message: str


def _base(kind: Kind, as_of: datetime, judge: Judge) -> dict[str, Any]:
    return {
        "run_id": uuid.uuid4().hex,
        "kind": kind,
        "as_of": as_of.astimezone(UTC).isoformat(),
        "model": judge.model,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha": PROMPT_SHA,
    }


def run_once(
    kind: Kind,
    news_root: Path,
    store: AiStore,
    judge: Judge,
    now: Callable[[], datetime],
    currencies: tuple[str, ...] = CURRENCIES,
    trigger: Trigger | None = None,
    cap_usd: float = CAP_USD,
) -> RunResult:
    as_of = now()
    rec = _base(kind, as_of, judge)
    if trigger is not None:
        rec["trigger_sha256"] = trigger.sha256
        rec["trigger_title"] = trigger.title
    sources = collect_sources(news_root, as_of)
    rec["sources"] = [
        {"id": s.id, "label": s.label, "received_at": s.received_at, "sha256": s.sha256}
        for s in sources
    ]
    previous = store.last_views(PROMPT_SHA)
    user = user_message(kind, as_of, currencies, sources, previous, trigger)
    spent = store.month_spend_usd(as_of)
    worst = worst_case_usd(judge.model, SYSTEM_PROMPT, user)

    def finish(status: str, message: str, answer: RawAnswer | None = None) -> RunResult:
        rec["status"] = status
        if status != "ok":
            rec["error"] = message
        rec["cost_usd"] = 0.0
        if answer is not None:
            rec["input_tokens"] = answer.input_tokens
            rec["output_tokens"] = answer.output_tokens
            rec["served_model"] = answer.model
            rec["cost_usd"] = round(
                cost_usd(judge.model, answer.input_tokens, answer.output_tokens), 6
            )
        rec["written_at"] = now().astimezone(UTC).isoformat()
        store.append(rec, as_of)
        return RunResult(status, rec, message)

    if spent + worst > cap_usd:
        return finish(
            "skipped",
            f"tetto di spesa: spesi {spent:.2f} $ questo mese, la chiamata può costare fino a "
            f"{worst:.2f} $ (tetto {cap_usd:.2f} $)",
        )
    try:
        answer = judge.ask(SYSTEM_PROMPT, user, answer_json_schema())
    except JudgeError as exc:
        return finish("failed", str(exc), exc.answer)
    try:
        parsed = ModelAnswer.model_validate_json(answer.text)
        check_answer(parsed, kind, currencies, {s.id for s in sources})
    except (ValidationError, ValueError) as exc:
        return finish("failed", f"risposta fuori schema: {exc}"[:600], answer)
    sha_of = {s.id: s.sha256 for s in sources}
    views = []
    for v in sorted(parsed.judgements, key=lambda x: x.currency):
        prev = previous.get(v.currency)
        row = v.model_dump()
        row["sources"] = list(v.sources)
        row["sources_sha256"] = [sha_of[i] for i in v.sources]
        row["tone_change"] = None if prev is None else v.tone - int(prev.get("tone", 0))
        views.append(row)
    rec["judgements"] = views
    return finish("ok", "", answer)


def run_daily(
    news_root: Path,
    store: AiStore,
    judge: Judge,
    now: Callable[[], datetime],
    notify: Notify | None = None,
    cap_usd: float = CAP_USD,
    force: bool = False,
) -> RunResult | None:
    """The morning judgement, Monday to Friday, once a day (``force`` to repeat it)."""

    local = now().astimezone(LOCAL_TZ)
    if not force and (local.weekday() >= 5 or store.has_daily(f"{now().astimezone(UTC):%Y-%m-%d}")):
        return None
    result = run_once("daily", news_root, store, judge, now, cap_usd=cap_usd)
    if notify is not None:
        notify(digest_text(result))
    return result


def run_events(
    news_root: Path,
    store: AiStore,
    judge: Judge,
    now: Callable[[], datetime],
    notify: Notify | None = None,
    cap_usd: float = CAP_USD,
) -> list[RunResult]:
    """One event judgement for each new rate decision text in the archive."""

    results = []
    for trig in pending_triggers(news_root, now(), store.handled_triggers(), EVENT_WINDOW):
        res = run_once(
            "event", news_root, store, judge, now, (trig.currency,), trig, cap_usd=cap_usd
        )
        results.append(res)
        if notify is not None:
            text = event_alert_text(res, trig)
            if text:
                notify(text)
    return results


def digest_text(result: RunResult) -> str:
    rec = result.record
    if result.status != "ok":
        return f"AI in ombra: giudizio del mattino non scritto ({result.status}). {result.message}"
    lines = ["AI in ombra (solo osservazione, il motore non la usa):"]
    for v in rec.get("judgements", []):
        lines.append(
            f"{v['currency']}: tono {v['tone']:+d}, 5 giorni {ARROW[v['bias_5d']]}, "
            f"rischio 24h {v['event_risk_24h']}, fiducia {v['confidence']:.1f}"
        )
    return "\n".join(lines)


def event_alert_text(result: RunResult, trig: Trigger) -> str | None:
    """Alert only for a strong surprise (±2) or a failed event run."""

    if result.status != "ok":
        return f"AI in ombra: giudizio dopo {trig.bank.upper()} non scritto. {result.message}"
    for v in result.record.get("judgements", []):
        if abs(v.get("surprise") or 0) == 2:
            return (
                f"AI in ombra: sorpresa {v['surprise']:+d} su {v['currency']} dopo "
                f"{trig.bank.upper()} ({trig.title}). {v['rationale']}"
            )
    return None


__all__ = [
    "CAP_USD",
    "RunResult",
    "digest_text",
    "event_alert_text",
    "run_daily",
    "run_events",
    "run_once",
]
