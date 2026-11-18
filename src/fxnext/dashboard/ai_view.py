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
ARROW = {-1: "↓ più debole", 0: "→ nessuna idea", 1: "↑ più forte"}


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
            latest[str(v.get("currency"))] = {
                **v,
                "at": _local(r.get("as_of")),
                "kind": "dopo decisione" if r.get("kind") == "event" else "mattino",
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
