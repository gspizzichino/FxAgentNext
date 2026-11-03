"""Metrics in R (M0 spec §14.4, M2 spec §7). Pure functions of R sequences."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import numpy.typing as npt

_WEEK_NS = 7 * 86_400 * 1_000_000_000
# Weeks start on Sunday 00:00 UTC, so a whole FX trading week falls in one block.
_WEEK_ORIGIN_NS = int(datetime(2000, 1, 2, tzinfo=UTC).timestamp()) * 1_000_000_000
_DAY_NS = 86_400 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class Summary:
    trades: int
    independent: int
    expectancy: float | None
    ci_low: float | None
    ci_high: float | None
    total_r: float
    win_rate: float | None
    profit_factor: float | None
    max_drawdown_r: float
    max_drawdown_days: float
    trades_per_month: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def max_drawdown(r: npt.NDArray[np.float64], exit_ns: npt.NDArray[np.int64]) -> tuple[float, float]:
    """Largest peak-to-trough fall of cumulative R, and longest time under water (days)."""

    if r.size == 0:
        return 0.0, 0.0
    equity = np.concatenate([[0.0], np.cumsum(r)])
    peaks = np.maximum.accumulate(equity)
    dd = float((peaks - equity).max())
    times = np.concatenate([[exit_ns[0]], exit_ns])
    longest = 0
    peak_time = int(times[0])
    for k in range(1, equity.size):
        if equity[k] >= peaks[k - 1]:
            peak_time = int(times[k])
        else:
            longest = max(longest, int(times[k]) - peak_time)
    return dd, longest / _DAY_NS


def independent_count(entry_ns: npt.NDArray[np.int64], exit_ns: npt.NDArray[np.int64]) -> int:
    """Greedy count of trades not overlapping a previously counted one (M0 §14.3)."""

    order = np.argsort(entry_ns, kind="stable")
    count, busy = 0, None
    for i in order.tolist():
        if busy is None or int(entry_ns[i]) > busy:
            count += 1
            busy = int(exit_ns[i])
    return count


def week_keys(entry_ns: npt.NDArray[np.int64]) -> npt.NDArray[np.int64]:
    return (entry_ns - _WEEK_ORIGIN_NS) // _WEEK_NS


def block_bootstrap_ci(
    r: npt.NDArray[np.float64],
    entry_ns: npt.NDArray[np.int64],
    *,
    samples: int,
    seed: int,
    level: float = 0.95,
) -> tuple[float, float] | None:
    """Percentile CI of the mean R, resampling whole weeks with replacement."""

    if r.size < 2:
        return None
    _, inverse = np.unique(week_keys(entry_ns), return_inverse=True)
    sums = np.bincount(inverse, weights=r)
    counts = np.bincount(inverse).astype(np.float64)
    if sums.size < 2:
        return None
    rng = np.random.Generator(np.random.PCG64(seed))
    means = np.empty(samples, dtype=np.float64)
    chunk = 1000
    for s in range(0, samples, chunk):
        n = min(chunk, samples - s)
        idx = rng.integers(0, sums.size, size=(n, sums.size))
        means[s : s + n] = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(means, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def summarize(
    r: npt.NDArray[np.float64],
    entry_ns: npt.NDArray[np.int64],
    exit_ns: npt.NDArray[np.int64],
    *,
    months: float | None,
    bootstrap_samples: int,
    seed: int,
) -> Summary:
    order = np.argsort(entry_ns, kind="stable")
    r, entry_ns, exit_ns = r[order], entry_ns[order], exit_ns[order]
    n = int(r.size)
    wins, losses = r[r > 0].sum(), r[r < 0].sum()
    ci = block_bootstrap_ci(r, entry_ns, samples=bootstrap_samples, seed=seed)
    dd, dd_days = max_drawdown(r, exit_ns)
    return Summary(
        trades=n,
        independent=independent_count(entry_ns, exit_ns),
        expectancy=float(r.mean()) if n else None,
        ci_low=None if ci is None else ci[0],
        ci_high=None if ci is None else ci[1],
        total_r=float(r.sum()),
        win_rate=float((r > 0).mean()) if n else None,
        profit_factor=float(wins / -losses) if losses < 0 else None,
        max_drawdown_r=dd,
        max_drawdown_days=dd_days,
        trades_per_month=(n / months) if months else None,
    )


def percentile_of(value: float, distribution: npt.NDArray[np.float64]) -> float:
    """Share (0-100) of the distribution strictly below ``value``; ties count half."""

    if distribution.size == 0:
        return float("nan")
    below = np.count_nonzero(distribution < value)
    ties = np.count_nonzero(distribution == value)
    return 100.0 * (below + 0.5 * ties) / distribution.size


def by_year(r: npt.NDArray[np.float64], entry_ns: npt.NDArray[np.int64]) -> dict[int, float]:
    years = np.array(
        [datetime.fromtimestamp(int(t) / 1e9, UTC).year for t in entry_ns.tolist()], dtype=np.int64
    )
    return {int(y): float(r[years == y].sum()) for y in np.unique(years).tolist()}
