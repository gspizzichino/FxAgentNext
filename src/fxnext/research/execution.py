"""Execution simulator ``exec-model-v1`` and cost model ``cost-model-v1`` (M2 spec §3-§5).

Pure function of (trade, price path, parameters). Every price it produces is
MODELLED. The candidate levels are never modified (contracts §12): a LONG exits
on the BID, a SHORT on the ASK, as on MT5. The ``SIGNAL`` stream instead enters
at ``entry_ref`` and follows the BID for both directions, without costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Literal

import numpy as np
import numpy.typing as npt

from fxnext.data.path import MinuteStatus, PricePath
from fxnext.domain.calendar import FxCalendar
from fxnext.domain.contracts import Candidate
from fxnext.domain.enums import Direction, Timeframe
from fxnext.domain.model import Model
from fxnext.domain.timeutil import from_ns, to_ns

EXEC_MODEL_VERSION = "exec-model-v1"
COST_MODEL_VERSION = "cost-model-v1"
PIP_SIZE = {"EURUSD": 1e-4, "GBPUSD": 1e-4, "USDJPY": 1e-2}
_MIN = 60_000_000_000
_H1 = Timeframe.H1.ns


class Stream(StrEnum):
    SIGNAL = "signal"
    EXEC = "exec"
    COUNTERFACTUAL = "counterfactual"
    BROKER = "broker"  # filled from MT5 in M3; always empty in M2


class ExitReason(StrEnum):
    STOP = "STOP"
    STOP_GAP = "STOP_GAP"
    TARGET = "TARGET"
    TIME = "TIME"
    NOT_FILLED_NO_QUOTE = "NOT_FILLED_NO_QUOTE"
    END_OF_DATA = "END_OF_DATA"  # path ends before an exit: excluded from metrics, counted


class ExecParams(Model):
    version: Literal["exec-model-v1"] = "exec-model-v1"
    latency_minutes: int = 0
    time_exit_slots: int = 120
    path_gap_minutes: int = 60
    ambiguity: Literal["STOP_FIRST", "TARGET_FIRST"] = "STOP_FIRST"


class CostModel(Model):
    version: Literal["cost-model-v1"] = "cost-model-v1"
    commission_pips: float = 0.35  # per side
    slippage_entry_pips: float = 0.1
    slippage_stop_pips: float = 0.2  # STOP and TIME exits
    slippage_target_pips: float = 0.0
    multiplier: float = 1.0  # sensitivity: applies to commission and slippage, never to spread

    def scaled(self, multiplier: float) -> CostModel:
        return self.model_copy(update={"multiplier": multiplier})


@dataclass(frozen=True, slots=True)
class TradeSpec:
    """What the simulator needs from a candidate (or from a benchmark entry)."""

    key: str
    symbol: str
    direction: Direction
    entry_ref: float
    stop: float
    target: float
    available_at_ns: int
    expires_at_ns: int

    @property
    def risk(self) -> float:
        return abs(self.entry_ref - self.stop)

    @classmethod
    def of(cls, symbol: str, c: Candidate) -> TradeSpec:
        return cls(
            c.candidate_id,
            symbol,
            c.direction,
            c.entry_ref,
            c.stop,
            c.target,
            to_ns(c.available_at),
            to_ns(c.expires_at),
        )


@dataclass(frozen=True, slots=True)
class Fill:
    key: str
    stream: Stream
    reason: ExitReason
    entry_ns: int | None = None
    entry_price: float | None = None
    exit_ns: int | None = None
    exit_price: float | None = None
    r_gross: float | None = None  # after spread and slippage, before commission
    r_net: float | None = None
    commission_r: float = 0.0
    spread_pips: float = 0.0  # observed spread paid at entry (LONG) or exit (SHORT)
    slippage_pips: float = 0.0
    ambiguous: bool = False
    path_gap: bool = False
    max_missing_minutes: int = 0
    holding_slots: int = 0
    weekend_crossings: int = 0
    mae_r: float | None = None
    mfe_r: float | None = None

    @property
    def counted(self) -> bool:
        return self.r_net is not None


class Simulator:
    def __init__(self, path: PricePath, calendar: FxCalendar | None = None) -> None:
        self.path = path
        cols = path.columns
        self.t = cols["t_ns"]
        self.ordinal = path.ordinal
        self.status = cols["status"]
        self.cols = cols
        calendar = calendar or FxCalendar()
        self.h1 = calendar.slot_grid(
            Timeframe.H1,
            from_ns(path.coverage_start_ns),
            from_ns(path.coverage_end_ns) + timedelta(days=60),
        )

    def _time_exit_ns(self, entry_ns: int, slots: int) -> int:
        idx = int(np.searchsorted(self.h1, entry_ns, side="right")) - 1
        last = idx + slots - 1
        if idx < 0 or last >= self.h1.size:
            raise ValueError("calendar padding too small for the time exit")
        return int(self.h1[last]) + _H1

    def simulate(
        self, spec: TradeSpec, stream: Stream, params: ExecParams, costs: CostModel
    ) -> Fill:
        if spec.symbol != self.path.symbol:
            raise ValueError("trade and path symbols differ")
        signal = stream is Stream.SIGNAL
        long = spec.direction is Direction.LONG
        sign = 1.0 if long else -1.0
        pip = PIP_SIZE[spec.symbol]
        m = 0.0 if signal else costs.multiplier
        slip_entry = costs.slippage_entry_pips * pip * m
        slip_stop = costs.slippage_stop_pips * pip * m
        slip_target = costs.slippage_target_pips * pip * m
        commission = costs.commission_pips * pip * m

        # --- entry: first VALID minute in [available_at + latency, expires_at)
        t = self.t
        start = spec.available_at_ns + params.latency_minutes * _MIN
        i0 = int(np.searchsorted(t, start))
        i1 = int(np.searchsorted(t, spec.expires_at_ns))
        valid = np.flatnonzero(self.status[i0:i1] == int(MinuteStatus.VALID))
        if valid.size == 0:
            return Fill(spec.key, stream, ExitReason.NOT_FILLED_NO_QUOTE)
        j = i0 + int(valid[0])
        entry_ns = int(t[j])
        side = "bid" if (signal or long) else "ask"
        if signal:
            entry = spec.entry_ref
            spread = 0.0
        elif long:
            entry = float(self.cols["ask_open"][j]) + slip_entry
            spread = float(self.cols["ask_open"][j] - self.cols["bid_open"][j])
        else:
            entry = float(self.cols["bid_open"][j]) - slip_entry
            spread = 0.0

        # --- exit search on [j, end)
        time_exit = self._time_exit_ns(entry_ns, params.time_exit_slots)
        end = int(np.searchsorted(t, time_exit))
        op = self.cols[f"{side}_open"][j:end]
        hi = self.cols[f"{side}_high"][j:end]
        lo = self.cols[f"{side}_low"][j:end]
        cl = self.cols[f"{side}_close"][j:end]
        if long:
            stop_hit, tgt_hit = lo <= spec.stop, hi >= spec.target
            open_stop, open_tgt = op <= spec.stop, op >= spec.target
        else:
            stop_hit, tgt_hit = hi >= spec.stop, lo <= spec.target
            open_stop, open_tgt = op >= spec.stop, op <= spec.target
        open_stop[:1] = False  # the entry minute opens at the fill
        open_tgt[:1] = False
        hits = np.flatnonzero(stop_hit | tgt_hit | open_stop | open_tgt)
        ambiguous = False
        if hits.size:
            k = int(hits[0])
            if open_stop[k]:
                reason, exit_price = ExitReason.STOP_GAP, float(op[k])
            elif open_tgt[k]:
                reason, exit_price = ExitReason.TARGET, spec.target - sign * slip_target
            else:
                ambiguous = bool(stop_hit[k] and tgt_hit[k])
                stop_first = params.ambiguity == "STOP_FIRST"
                if stop_hit[k] and (stop_first or not tgt_hit[k]):
                    reason, exit_price = ExitReason.STOP, spec.stop - sign * slip_stop
                else:
                    reason, exit_price = ExitReason.TARGET, spec.target - sign * slip_target
        else:
            k = end - j - 1
            covered = end < t.size or time_exit <= self.path.coverage_end_ns
            if k < 0 or not covered:
                return Fill(spec.key, stream, ExitReason.END_OF_DATA, entry_ns, entry)
            reason, exit_price = ExitReason.TIME, float(cl[k]) - sign * slip_stop
        exit_ns = int(t[j + k])
        if not signal and not long:
            idx = j + k
            if reason is ExitReason.TIME:
                spread = float(self.cols["ask_close"][idx] - self.cols["bid_close"][idx])
            else:
                spread = float(self.cols["ask_low"][idx] - self.cols["bid_low"][idx])

        risk = spec.risk
        r_gross = sign * (exit_price - entry) / risk
        commission_r = 2.0 * commission / risk
        ords = self.ordinal[j : j + k + 1]
        times = t[j : j + k + 1]
        missing = np.diff(ords) - 1 if ords.size > 1 else np.zeros(0, np.int64)
        crossings = int(np.count_nonzero((np.diff(times) > _MIN) & (missing == 0)))
        max_missing = int(missing.max()) if missing.size else 0
        if long:
            mfe = float(hi[: k + 1].max()) - entry
            mae = entry - float(lo[: k + 1].min())
        else:
            mfe = entry - float(lo[: k + 1].min())
            mae = float(hi[: k + 1].max()) - entry
        slippage = (
            0.0 if signal else (slip_entry + _exit_slip(reason, slip_stop, slip_target)) / pip
        )
        return Fill(
            key=spec.key,
            stream=stream,
            reason=reason,
            entry_ns=entry_ns,
            entry_price=entry,
            exit_ns=exit_ns,
            exit_price=exit_price,
            r_gross=r_gross,
            r_net=r_gross - commission_r,
            commission_r=commission_r,
            spread_pips=spread / pip,
            slippage_pips=slippage,
            ambiguous=ambiguous,
            path_gap=max_missing > params.path_gap_minutes,
            max_missing_minutes=max_missing,
            holding_slots=int((int(ords[-1]) - int(ords[0])) // 60) + 1,
            weekend_crossings=crossings,
            mae_r=mae / risk,
            mfe_r=mfe / risk,
        )


def _exit_slip(reason: ExitReason, slip_stop: float, slip_target: float) -> float:
    if reason in (ExitReason.STOP, ExitReason.TIME):
        return slip_stop
    if reason is ExitReason.TARGET:
        return slip_target
    return 0.0


def apply_position_policy(fills: list[Fill]) -> tuple[list[int], list[int]]:
    """One position per symbol (E4). ``fills`` sorted by entry, one symbol.

    Returns (executed indexes, counterfactual indexes). A trade whose entry is not
    after the exit minute of the open position is counterfactual.
    """

    executed: list[int] = []
    blocked: list[int] = []
    busy_until: int | None = None
    for i, f in enumerate(fills):
        if f.entry_ns is None:
            continue
        if busy_until is not None and f.entry_ns <= busy_until:
            blocked.append(i)
            continue
        executed.append(i)
        busy_until = f.exit_ns if f.exit_ns is not None else np.iinfo(np.int64).max
    return executed, blocked


def as_arrays(fills: list[Fill]) -> dict[str, npt.NDArray[np.float64]]:
    counted = [f for f in fills if f.counted]
    return {
        "r": np.array([f.r_net for f in counted], dtype=np.float64),
        "entry_ns": np.array([f.entry_ns for f in counted], dtype=np.int64),
        "exit_ns": np.array([f.exit_ns for f in counted], dtype=np.int64),
    }
