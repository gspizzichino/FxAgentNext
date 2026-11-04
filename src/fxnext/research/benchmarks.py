"""Null benchmarks (M2 spec §6). Same exits, risk unit, execution and costs as the baseline.

Each candidate entry instant has only two possible trades (LONG or SHORT), so both
are simulated once and every random repetition just picks among them; the
single-position policy is then applied per repetition.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from fxnext.domain.enums import Direction
from fxnext.research.execution import Fill


@dataclass(frozen=True, slots=True)
class EntryPoint:
    """An instant where a trade could start: as_of, BID close and ATR at the signal slot."""

    symbol: str
    as_of_ns: int
    expires_at_ns: int
    close: float
    atr: float
    month: int  # year * 12 + month - 1


@dataclass(frozen=True, slots=True)
class PairedFills:
    """Simulated LONG and SHORT outcome per entry point (r_net None = not counted)."""

    entry_ns: npt.NDArray[np.int64]
    exit_ns: npt.NDArray[np.int64]  # [n, 2]: column 0 LONG, 1 SHORT
    r: npt.NDArray[np.float64]  # [n, 2], NaN when not counted
    month: npt.NDArray[np.int64]

    @classmethod
    def of(cls, points: Sequence[EntryPoint], fills: Sequence[tuple[Fill, Fill]]) -> PairedFills:
        n = len(points)
        entry = np.zeros(n, np.int64)
        exit_ = np.zeros((n, 2), np.int64)
        r = np.full((n, 2), np.nan)
        for i, pair in enumerate(fills):
            for d, f in enumerate(pair):
                if f.entry_ns is not None:
                    entry[i] = f.entry_ns
                if f.counted:
                    assert f.exit_ns is not None and f.r_net is not None
                    exit_[i, d] = f.exit_ns
                    r[i, d] = f.r_net
        return cls(entry, exit_, r, np.array([p.month for p in points], np.int64))


def _policy_mean(
    entry: npt.NDArray[np.int64], exit_: npt.NDArray[np.int64], r: npt.NDArray[np.float64]
) -> float:
    total, count, busy = 0.0, 0, -1
    for i in np.argsort(entry, kind="stable").tolist():
        if np.isnan(r[i]) or int(entry[i]) <= busy:
            continue
        total += float(r[i])
        count += 1
        busy = int(exit_[i])
    return total / count if count else float("nan")


def fixed_direction(paired: PairedFills, direction: Direction) -> float:
    d = 0 if direction is Direction.LONG else 1
    return _policy_mean(paired.entry_ns, paired.exit_ns[:, d], paired.r[:, d])


def random_direction(paired: PairedFills, repetitions: int, seed: int) -> npt.NDArray[np.float64]:
    rng = np.random.Generator(np.random.PCG64(seed))
    n = paired.entry_ns.size
    out = np.empty(repetitions)
    rows = np.arange(n)
    for k in range(repetitions):
        d = rng.integers(0, 2, size=n)
        out[k] = _policy_mean(paired.entry_ns, paired.exit_ns[rows, d], paired.r[rows, d])
    return out


def random_time(
    pool: PairedFills,
    entries_per_month: dict[int, int],
    repetitions: int,
    seed: int,
) -> npt.NDArray[np.float64]:
    """Same number of entries per month as the baseline, at random eligible instants."""

    rng = np.random.Generator(np.random.PCG64(seed))
    by_month = {m: np.flatnonzero(pool.month == m) for m in sorted(entries_per_month)}
    out = np.empty(repetitions)
    for k in range(repetitions):
        picks: list[npt.NDArray[np.int64]] = []
        for m, count in sorted(entries_per_month.items()):
            eligible = by_month[m]
            if eligible.size:
                picks.append(rng.choice(eligible, size=min(count, eligible.size), replace=False))
        idx = np.sort(np.concatenate(picks)) if picks else np.zeros(0, np.int64)
        d = rng.integers(0, 2, size=idx.size)
        out[k] = _policy_mean(pool.entry_ns[idx], pool.exit_ns[idx, d], pool.r[idx, d])
    return out
