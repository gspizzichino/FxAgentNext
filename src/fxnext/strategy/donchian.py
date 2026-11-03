"""``baseline_donchian_breakout`` v1 — normative definition in contracts §11.

Research only. Parameters frozen before any empirical result (ADR-0006).
Arithmetic uses only + - * / abs max min on IEEE binary64 in a fixed order.
"""

from __future__ import annotations

from typing import ClassVar

from fxnext.domain.contracts import (
    Abstention,
    Candidate,
    EvaluationRequest,
    FilterResult,
    MarketSnapshot,
    SnapshotRequirements,
    StrategyConfig,
    StrategyEvaluation,
    StrategyParams,
    StrategyState,
    StructuralEvent,
)
from fxnext.domain.enums import (
    Comparator,
    Direction,
    FilterKind,
    FilterStatus,
    PriceBasis,
    ReasonCode,
    SlotStatus,
    Timeframe,
)
from fxnext.domain.frozen import FrozenMap
from fxnext.domain.hashing import content_hash
from fxnext.domain.timeutil import from_ns
from fxnext.strategy.base import ENGINE_VERSION, SCHEDULER_VERSION


class DonchianParams(StrategyParams):
    channel_slots: int = 20
    atr_slots: int = 14
    stop_atr_mult: float = 1.5
    target_r: float = 2.0
    signal_timeframe: Timeframe = Timeframe.H1
    basis: PriceBasis = PriceBasis.BID
    validity_slots: int = 1


FILTERS = (
    "signal_bar_valid",
    "history_coverage",
    "window_available",
    "window_valid",
    "window_not_flat",
    "atr_positive",
    "entry_slot_in_session",
)
_KIND = {name: FilterKind.DATA_GATE for name in FILTERS} | {
    "entry_slot_in_session": FilterKind.MARKET_GATE
}
_SIGNAL_REASON = {
    SlotStatus.UNAVAILABLE: ReasonCode.BAR_NOT_AVAILABLE,
    SlotStatus.INVALID: ReasonCode.INVALID_DATA,
    SlotStatus.FLAT_ZERO_VOLUME: ReasonCode.FLAT_BAR_IN_WINDOW,
    SlotStatus.BEFORE_COVERAGE: ReasonCode.INSUFFICIENT_HISTORY,
}
_WINDOW_GATES = (
    ("history_coverage", SlotStatus.BEFORE_COVERAGE, ReasonCode.INSUFFICIENT_HISTORY),
    ("window_available", SlotStatus.UNAVAILABLE, ReasonCode.DATA_GAP),
    ("window_valid", SlotStatus.INVALID, ReasonCode.INVALID_DATA),
    ("window_not_flat", SlotStatus.FLAT_ZERO_VOLUME, ReasonCode.FLAT_BAR_IN_WINDOW),
)


class DonchianBreakout:
    strategy_id: ClassVar[str] = "baseline_donchian_breakout"
    strategy_version: ClassVar[str] = "1"
    params_model: ClassVar[type[StrategyParams]] = DonchianParams
    research_only: ClassVar[bool] = True
    declared_filters: ClassVar[tuple[str, ...]] = FILTERS

    def default_config(self) -> StrategyConfig:
        return StrategyConfig(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            params=DonchianParams(),
            detection_param_names=("channel_slots", "signal_timeframe", "basis"),
        )

    def requirements(self, config: StrategyConfig) -> tuple[SnapshotRequirements, ...]:
        p = _params(config)
        back = max(p.channel_slots + 1, p.atr_slots)
        return (SnapshotRequirements(timeframe=p.signal_timeframe, basis=p.basis, slots_back=back),)

    def evaluate(
        self,
        market_snapshot: MarketSnapshot,
        strategy_config: StrategyConfig,
        strategy_state: StrategyState,
    ) -> StrategyEvaluation:
        snap, config, p = market_snapshot, strategy_config, _params(strategy_config)
        if (
            config.strategy_id != self.strategy_id
            or config.strategy_version != self.strategy_version
        ):
            raise ValueError("config does not belong to this strategy")
        request = EvaluationRequest(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            config_hash=config.config_hash,
            symbol=snap.symbol,
            signal_timeframe=p.signal_timeframe,
            expected_close=snap.signal_slot.close_time,
            as_of=snap.as_of,
            scheduler_version=SCHEDULER_VERSION,
        )
        series = snap.series[p.signal_timeframe]
        if series.basis is not p.basis:
            raise ValueError("snapshot basis does not match config")
        back = self.requirements(config)[0].slots_back
        if len(series) != back + 1:
            raise ValueError(f"snapshot window must have {back + 1} slots")
        status = series.statuses()
        opens = series.open_time_ns
        t = back  # index of the signal slot
        filters: list[FilterResult] = []
        measures: dict[str, float] = {}

        def done(
            reason: ReasonCode, detail: str, events: tuple[StructuralEvent, ...] = ()
        ) -> StrategyEvaluation:
            evaluated = {f.name for f in filters}
            rest = [
                FilterResult(name=n, kind=_KIND[n], status=FilterStatus.NOT_EVALUATED)
                for n in FILTERS
                if n not in evaluated
            ]
            return StrategyEvaluation(
                request=request,
                engine_version=ENGINE_VERSION,
                snapshot_hash=snap.snapshot_hash,
                events=events,
                outcome=Abstention(reason_code=reason, detail=detail),
                filters=tuple(filters + rest),
                measures=FrozenMap(measures),
                next_state=strategy_state,
            )

        def gate(
            name: str,
            passed: bool,
            measure: object,
            threshold: object,
            comparator: Comparator | None,
            reason: ReasonCode,
        ) -> bool:
            filters.append(
                FilterResult(
                    name=name,
                    kind=_KIND[name],
                    status=FilterStatus.PASSED if passed else FilterStatus.FAILED,
                    measure=measure,
                    threshold=threshold,
                    comparator=comparator,
                    reason_code=None if passed else reason,
                )
            )
            return passed

        # Gate 1: signal bar
        s_t = status[t]
        if not gate(
            "signal_bar_valid",
            s_t is SlotStatus.VALID,
            s_t.name,
            "VALID",
            Comparator.EQ,
            _SIGNAL_REASON.get(s_t, ReasonCode.INVALID_DATA),
        ):
            return done(_SIGNAL_REASON[s_t], f"signal slot status {s_t.name}")
        # Gates 2-5: window t-back .. t-1
        for name, bad_status, reason in _WINDOW_GATES:
            offending = [i for i in range(t) if status[i] is bad_status]
            first = None if not offending else from_ns(int(opens[offending[0]])).isoformat()
            if not gate(
                name,
                not offending,
                {"count": len(offending), "first_open": first},
                0,
                Comparator.EQ,
                reason,
            ):
                return done(reason, f"{len(offending)} slot(s) {bad_status.name}, first {first}")

        high, low, close = series.high, series.low, series.close
        n, a = p.channel_slots, p.atr_slots
        upper = max(float(x) for x in high[t - n : t])
        lower = min(float(x) for x in low[t - n : t])
        upper_prev = max(float(x) for x in high[t - n - 1 : t - 1])
        lower_prev = min(float(x) for x in low[t - n - 1 : t - 1])
        tr_sum = 0.0
        for i in range(t - a + 1, t + 1):
            h_i, l_i, c_prev = float(high[i]), float(low[i]), float(close[i - 1])
            tr_sum += max(h_i - l_i, abs(h_i - c_prev), abs(l_i - c_prev))
        atr = tr_sum / a
        c_t, c_prev = float(close[t]), float(close[t - 1])
        measures.update(
            {
                "channel_upper": upper,
                "channel_lower": lower,
                "channel_upper_prev": upper_prev,
                "channel_lower_prev": lower_prev,
                "close_t": c_t,
                "close_t_minus_1": c_prev,
                "atr": atr,
            }
        )

        # Gate 6
        if not gate(
            "atr_positive", atr > 0.0, atr, 0.0, Comparator.GT, ReasonCode.INVALID_GEOMETRY
        ):
            return done(ReasonCode.INVALID_GEOMETRY, "ATR is not positive")
        # Gate 7
        nxt = snap.next_slot
        in_session = nxt is not None and nxt.open_time == snap.signal_slot.close_time
        if not gate(
            "entry_slot_in_session",
            in_session,
            None if nxt is None else nxt.open_time.isoformat(),
            snap.signal_slot.close_time.isoformat(),
            Comparator.EQ,
            ReasonCode.MARKET_CLOSED,
        ):
            return done(ReasonCode.MARKET_CLOSED, "no in-session entry slot after signal slot")

        direction: Direction | None = None
        level = 0.0
        if c_t > upper and c_prev <= upper_prev:
            direction, level = Direction.LONG, upper
        elif c_t < lower and c_prev >= lower_prev:
            direction, level = Direction.SHORT, lower
        if direction is None:
            return done(ReasonCode.NO_EVENT, "no fresh channel breakout")

        anchor = snap.signal_slot.open_time
        discriminator = {
            "channel_first_open": from_ns(int(opens[t - n])).isoformat(),
            "channel_last_open": from_ns(int(opens[t - 1])).isoformat(),
            "level": level,
        }
        event_id = content_hash(
            "StructuralEvent/1",
            {
                "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "detection_params": config.detection_params(),
                "symbol": snap.symbol,
                "timeframe": p.signal_timeframe,
                "kind": "donchian_breakout",
                "direction": direction,
                "anchor_open_time": anchor,
                "discriminator": discriminator,
            },
        )
        event = StructuralEvent(
            event_id=event_id,
            kind="donchian_breakout",
            timeframe=p.signal_timeframe,
            anchor_open_time=anchor,
            direction=direction,
            discriminator=discriminator,
        )
        e = c_t
        d = p.stop_atr_mult * atr
        if direction is Direction.LONG:
            stop, target = e - d, e + p.target_r * d
        else:
            stop, target = e + d, e - p.target_r * d
        if stop <= 0.0 or target <= 0.0:
            return done(ReasonCode.INVALID_GEOMETRY, "non-positive level", (event,))
        assert nxt is not None
        expires = nxt.close_time
        candidate_id = content_hash(
            "Candidate/1",
            {
                "event_id": event_id,
                "config_hash": config.config_hash,
                "direction": direction,
                "level_basis": p.basis,
                "entry_ref": e,
                "stop": stop,
                "target": target,
                "expires_at": expires,
            },
        )
        candidate = Candidate(
            candidate_id=candidate_id,
            event_id=event_id,
            direction=direction,
            level_basis=p.basis,
            entry_ref=e,
            stop=stop,
            target=target,
            available_at=snap.as_of,
            expires_at=expires,
        )
        return StrategyEvaluation(
            request=request,
            engine_version=ENGINE_VERSION,
            snapshot_hash=snap.snapshot_hash,
            events=(event,),
            outcome=candidate,
            filters=tuple(filters),
            measures=FrozenMap(measures),
            next_state=strategy_state,
        )


def _params(config: StrategyConfig) -> DonchianParams:
    if not isinstance(config.params, DonchianParams):
        raise TypeError("expected DonchianParams")
    return config.params


REGISTRY: dict[tuple[str, str], type[DonchianBreakout]] = {
    (DonchianBreakout.strategy_id, DonchianBreakout.strategy_version): DonchianBreakout,
}
