"""Strategy protocol and registry (contracts §8)."""

from __future__ import annotations

from typing import ClassVar, Protocol

from fxnext.domain.contracts import (
    MarketSnapshot,
    SnapshotRequirements,
    StrategyConfig,
    StrategyEvaluation,
    StrategyParams,
    StrategyState,
)

ENGINE_VERSION = "fxnext-engine-v1"
SCHEDULER_VERSION = "calendar-scheduler-v1"


class Strategy(Protocol):
    strategy_id: ClassVar[str]
    strategy_version: ClassVar[str]
    params_model: ClassVar[type[StrategyParams]]
    research_only: ClassVar[bool]
    declared_filters: ClassVar[tuple[str, ...]]

    def default_config(self) -> StrategyConfig: ...

    def requirements(self, config: StrategyConfig) -> tuple[SnapshotRequirements, ...]: ...

    def evaluate(
        self,
        market_snapshot: MarketSnapshot,
        strategy_config: StrategyConfig,
        strategy_state: StrategyState,
    ) -> StrategyEvaluation: ...
