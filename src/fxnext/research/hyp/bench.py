"""Benchmarks for the hypotheses, with one position per symbol and the max-of-K correction.

Unlike research-v1 (one baseline), each repetition applies the single-position
policy per symbol and pools the R of the three pairs. Common rule 6: for every
repetition the maximum expectancy over the K variants is taken, and the best real
variant is compared with the 95th percentile of that maximum (a simple version of
White's Reality Check).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from fxnext.domain.timeutil import from_ns
from fxnext.research.execution import Fill


@dataclass(frozen=True, slots=True)
class Paired:
    """LONG (column 0) and SHORT (column 1) outcomes per instant, one symbol."""

    entry_ns: npt.NDArray[np.int64]
    exit_ns: npt.NDArray[np.int64]
    r: npt.NDArray[np.float64]  # NaN = not counted
    month: npt.NDArray[np.int64]

    @classmethod
    def of(cls, fills: Sequence[tuple[Fill, Fill]], available_ns: Sequence[int]) -> Paired:
        n = len(fills)
        entry = np.array(available_ns, np.int64) if n else np.zeros(0, np.int64)
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
        month = np.array([_month(int(t)) for t in available_ns], np.int64)
        return cls(entry, exit_, r, month)


def _month(ns: int) -> int:
    d = from_ns(ns)
    return d.year * 12 + d.month - 1


def month_of(ns: int) -> int:
    return _month(ns)


def _policy(
    entry: npt.NDArray[np.int64], exit_: npt.NDArray[np.int64], r: npt.NDArray[np.float64]
) -> tuple[float, int]:
    total, count, busy = 0.0, 0, -1
    for i in np.argsort(entry, kind="stable").tolist():
        if np.isnan(r[i]) or int(entry[i]) <= busy:
            continue
        total += float(r[i])
        count += 1
        busy = int(exit_[i])
    return total, count


def fixed_direction(per_symbol: Sequence[Paired], column: int) -> float | None:
    tot, cnt = 0.0, 0
    for p in per_symbol:
        s, c = _policy(p.entry_ns, p.exit_ns[:, column], p.r[:, column])
        tot, cnt = tot + s, cnt + c
    return tot / cnt if cnt else None


def random_direction(
    per_symbol: Sequence[Paired], repetitions: int, seed: int
) -> npt.NDArray[np.float64]:
    rng = np.random.Generator(np.random.PCG64(seed))
    out = np.full(repetitions, np.nan)
    for k in range(repetitions):
        tot, cnt = 0.0, 0
        for p in per_symbol:
            n = p.entry_ns.size
            d = rng.integers(0, 2, size=n)
            rows = np.arange(n)
            s, c = _policy(p.entry_ns, p.exit_ns[rows, d], p.r[rows, d])
            tot, cnt = tot + s, cnt + c
        if cnt:
            out[k] = tot / cnt
    return out


def random_time(
    pools: Sequence[Paired],
    entries_per_month: Sequence[dict[int, int]],
    repetitions: int,
    seed: int,
) -> npt.NDArray[np.float64]:
    """Per symbol, as many entries per month as the candidates, at random eligible instants."""

    rng = np.random.Generator(np.random.PCG64(seed))
    by_month = [
        {m: np.flatnonzero(pool.month == m) for m in sorted(epm)}
        for pool, epm in zip(pools, entries_per_month, strict=True)
    ]
    out = np.full(repetitions, np.nan)
    for k in range(repetitions):
        tot, cnt = 0.0, 0
        for pool, epm, months in zip(pools, entries_per_month, by_month, strict=True):
            picks = [
                rng.choice(months[m], size=min(c, months[m].size), replace=False)
                for m, c in sorted(epm.items())
                if months[m].size
            ]
            idx = np.sort(np.concatenate(picks)) if picks else np.zeros(0, np.int64)
            d = rng.integers(0, 2, size=idx.size)
            s, c = _policy(pool.entry_ns[idx], pool.exit_ns[idx, d], pool.r[idx, d])
            tot, cnt = tot + s, cnt + c
        if cnt:
            out[k] = tot / cnt
    return out


def max_of_k(distributions: Sequence[npt.NDArray[np.float64]]) -> npt.NDArray[np.float64]:
    """Per repetition, the maximum over the variants (same seeds for every variant)."""

    stack = np.vstack(distributions)
    with np.errstate(invalid="ignore"):
        all_nan = np.isnan(stack).all(axis=0)
        out = np.where(
            all_nan, np.nan, np.nanmax(np.where(np.isnan(stack), -np.inf, stack), axis=0)
        )
    return out.astype(np.float64)
