"""Pre-registration, temporal split and sealed holdout (M0 spec §14, M2 spec §8-§9).

The splits are fixed in code, not in the experiment file, so an experiment can
choose a split but cannot move its boundaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fxnext.domain.model import Model
from fxnext.research.execution import CostModel, ExecParams

PROTOCOL_VERSION = "protocol-v1"

SPLITS: dict[str, tuple[datetime, datetime]] = {
    "development": (datetime(2015, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
    "validation": (datetime(2021, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC)),
    "holdout": (datetime(2024, 1, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
}
SplitName = Literal["development", "validation", "holdout"]


class ProtocolError(RuntimeError):
    pass


class Criteria(Model):
    """Approved 2026-11-03 (M2 spec §9, E6). Fixed before any result."""

    min_independent_trades: int = 150
    ci_level: float = 0.95
    cost_stress_multiplier: float = 1.5
    benchmark_percentile: float = 95.0
    worst_year_r: float = -10.0
    holdout_max_drawdown_ratio: float = 1.5


class SymbolData(Model):
    symbol: str
    m15_bid_dataset: str
    path: str


class Preregistration(Model):
    experiment_id: str
    hypothesis: str
    split: SplitName
    holdout: bool = False
    strategy_id: str = "baseline_donchian_breakout"
    strategy_version: str = "1"
    data: tuple[SymbolData, ...]
    execution: ExecParams = ExecParams()
    costs: CostModel = CostModel()
    cost_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0)
    benchmark_repetitions: int = 1000
    benchmark_seed: int = 20261103
    bootstrap_samples: int = 10000
    bootstrap_seed: int = 20261104
    criteria: Criteria = Criteria()
    # Reference metrics needed by the holdout criteria (from the validation report).
    validation_max_drawdown_r: float | None = None


def load_preregistration(path: Path) -> tuple[Preregistration, str]:
    """Parse a TOML pre-registration. Returns the model and the sha256 of the file bytes."""

    raw = Path(path).read_bytes()
    # JSON mode: strict typing, but TOML arrays are accepted for tuple fields
    prereg = Preregistration.model_validate_json(json.dumps(tomllib.loads(raw.decode("utf-8"))))
    if prereg.split == "holdout" and not prereg.holdout:
        raise ProtocolError("the holdout split requires `holdout = true` in the pre-registration")
    if prereg.holdout and prereg.split != "holdout":
        raise ProtocolError("`holdout = true` is only valid with split = 'holdout'")
    return prereg, hashlib.sha256(raw).hexdigest()


def split_bounds(name: str) -> tuple[datetime, datetime]:
    if name not in SPLITS:
        raise ProtocolError(f"unknown split {name}")
    return SPLITS[name]


def check_development_only(start: datetime, end: datetime) -> None:
    """T12.4: the parameter grid may only look at the development split."""

    d0, d1 = SPLITS["development"]
    if start < d0 or end > d1:
        raise ProtocolError("the sensitivity grid is restricted to the development split")


class HoldoutLog:
    """Append-only log of holdout openings (``holdout_log.jsonl``)."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "holdout_log.jsonl"

    def entries(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text("utf-8").splitlines() if x.strip()]

    def open(
        self, prereg: Preregistration, prereg_sha256: str, *, reopen: bool, now: datetime
    ) -> None:
        previous = [e for e in self.entries() if e["experiment_id"] == prereg.experiment_id]
        if previous and not reopen:
            raise ProtocolError(
                f"holdout already opened for {prereg.experiment_id}; "
                "a new opening needs --reopen-holdout and is recorded"
            )
        record = {
            "experiment_id": prereg.experiment_id,
            "prereg_sha256": prereg_sha256,
            "opened_at": now.isoformat(),
            "opening": len(previous) + 1,
            "reopen": bool(previous),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
