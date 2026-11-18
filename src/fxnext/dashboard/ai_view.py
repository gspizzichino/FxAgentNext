"""Read the shadow AI archive for the "Contesto AI" panel (read only).

Shows the latest judgement per currency, the last runs and this month's
spending. Nothing here is passed to the engine (ADR-0018).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Rome")
ARROW = {-1: "↓ più debole", 0: "→ nessuna direzione", 1: "↑ più forte"}
BANK = {"USD": "Fed", "EUR": "BCE", "GBP": "BoE", "JPY": "BoJ"}
TONE = {-2: "molto colomba", -1: "colomba", 0: "neutrale", 1: "falco", 2: "molto falco"}
SURPRISE = {
    -2: "molto più colomba delle attese",
    -1: "più colomba delle attese",
    0: "in linea con le attese",
    1: "più falco delle attese",
    2: "molto più falco delle attese",
}
RISK_PILL = {"basso": "p-ok", "medio": "p-wait", "alto": "p-bad"}


def _confidence(c: float) -> str:
    return "bassa" if c < 0.34 else "media" if c < 0.67 else "alta"


def _change(d: Any) -> str:
    if d is None:
        return ""
    if d == 0:
        return "invariato"
    return f"{'più falco' if d > 0 else 'più colomba'} di {abs(int(d))} rispetto a prima"


def _runs(root: Path, max_files: int = 62) -> list[dict[str, Any]]:
    folder = root / "runs"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*/*.jsonl"))[-max_files:]:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _local(iso: Any) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")
    except ValueError:
        return "–"


def ai_context(root: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(tz=UTC)
    runs = _runs(root)
    latest: dict[str, dict[str, Any]] = {}
    for r in runs:
        if r.get("status") != "ok":
            continue
        for v in r.get("judgements", []):
            ccy = str(v.get("currency"))
            tone = int(v.get("tone", 0))
            surprise = v.get("surprise")
            latest[ccy] = {
                **v,
                "bank": BANK.get(ccy, ""),
                "tone_label": f"{TONE.get(tone, '–')} ({tone:+d})",
                "change_label": _change(v.get("tone_change")),
                "surprise_label": (
                    ""
                    if surprise is None
                    else f"{SURPRISE.get(int(surprise), '–')} ({int(surprise):+d})"
                ),
                "confidence_label": f"{_confidence(float(v.get('confidence', 0)))} "
                f"({float(v.get('confidence', 0)):.1f})",
                "risk_pill": RISK_PILL.get(str(v.get("event_risk_24h")), "p-na"),
                "at": _local(r.get("as_of")),
                "kind": "dopo una decisione"
                if r.get("kind") == "event"
                else "giudizio del mattino",
                "arrow": ARROW.get(int(v.get("bias_5d", 0)), "–"),
            }
    month = now.astimezone(UTC).strftime("%Y-%m")
    spent = sum(
        float(r.get("cost_usd") or 0.0)
        for r in runs
        if str(r.get("written_at", "")).startswith(month)
    )
    recent = [
        {
            "at": _local(r.get("as_of")),
            "kind": r.get("kind"),
            "status": r.get("status"),
            "error": r.get("error", ""),
            "cost": float(r.get("cost_usd") or 0.0),
        }
        for r in reversed(runs[-8:])
    ]
    return {
        "views": [latest[c] for c in ("EUR", "GBP", "JPY", "USD") if c in latest],
        "recent": recent,
        "spent": spent,
        "any": bool(runs),
    }


__all__ = ["ai_context"]
