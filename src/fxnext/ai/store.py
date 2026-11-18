"""Append-only archive of shadow AI runs: ``<root>/runs/<YYYY>/<YYYY-MM-DD>.jsonl``.

One line per run, successful or not. A line is never rewritten: a failed day
stays recorded as failed, and a newer model or prompt writes a series of its own.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STORE_VERSION = "ai-ombra-store-v1"


class AiStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def append(self, record: dict[str, Any], at: datetime) -> Path:
        rec = {"store_version": STORE_VERSION, **record}
        day = at.astimezone(UTC)
        path = self.root / "runs" / f"{day:%Y}" / f"{day:%Y-%m-%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return path

    def records(self) -> Iterator[dict[str, Any]]:
        """Every run, oldest first. Unreadable lines are skipped."""

        folder = self.root / "runs"
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*/*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec

    def month_spend_usd(self, now: datetime) -> float:
        month = now.astimezone(UTC).strftime("%Y-%m")
        return sum(
            float(r.get("cost_usd") or 0.0)
            for r in self.records()
            if str(r.get("written_at", "")).startswith(month)
        )

    def last_views(self, prompt_sha: str) -> dict[str, dict[str, Any]]:
        """Latest successful judgement per currency, for this prompt's series only."""

        out: dict[str, dict[str, Any]] = {}
        for r in self.records():
            if r.get("status") != "ok" or r.get("prompt_sha") != prompt_sha:
                continue
            for v in r.get("judgements", []):
                out[str(v.get("currency"))] = {**v, "as_of": r.get("as_of")}
        return out

    def handled_triggers(self) -> set[str]:
        """Decision texts that already have an event run (successful or failed)."""

        return {
            str(r["trigger_sha256"])
            for r in self.records()
            if r.get("kind") == "event" and r.get("trigger_sha256")
        }

    def has_daily(self, day: str) -> bool:
        return any(
            r.get("kind") == "daily"
            and r.get("status") == "ok"
            and str(r.get("as_of", "")).startswith(day)
            for r in self.records()
        )


__all__ = ["STORE_VERSION", "AiStore"]
