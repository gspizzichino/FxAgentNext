"""Pre-registration g10-cm-dev-001: carry and momentum on a G10 basket (frozen 2026-11-17).

The splits of this study are fixed here, in code, like ``protocol.SPLITS``: a
pre-registration chooses a split but cannot move its boundaries. The holdout is
not runnable from this module; validation needs an explicit approval recorded in
its own pre-registration file and exactly one variant.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import date
from pathlib import Path
from typing import Literal

from fxnext.domain.model import Model
from fxnext.research.protocol import ProtocolError

G10_PROTOCOL_VERSION = "g10-protocol-v1"
CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "NOK", "SEK")
PAIRS = {
    "EUR": "EURUSD",
    "GBP": "GBPUSD",
    "AUD": "AUDUSD",
    "NZD": "NZDUSD",
    "JPY": "USDJPY",
    "CHF": "USDCHF",
    "CAD": "USDCAD",
    "NOK": "USDNOK",
    "SEK": "USDSEK",
}
MAX_VARIANTS = 9

# [start, end) as month starts. Decided by Giacomo on 2026-11-17.
G10_SPLITS: dict[str, tuple[date, date]] = {
    "development": (date(2004, 1, 1), date(2014, 1, 1)),
    "validation": (date(2014, 1, 1), date(2020, 1, 1)),
    "holdout": (date(2020, 1, 1), date(2026, 11, 1)),
}
WARMUP_FIRST_YEAR = 2003


class G10Variant(Model):
    name: str
    signal: Literal["carry", "momentum", "combined"]
    k: int
    lookback_months: int | None = None  # momentum and combined


class G10Costs(Model):
    # v1: cost-model-v1 adapted (g10-cm-dev-001 §5). v2: same, but a zero spread at a
    # rebalance is replaced by the median non-zero spread of that pair (g10-cm-dev-002).
    version: Literal["g10-cost-v1", "g10-cost-v2"] = "g10-cost-v1"
    commission_pips: float = 0.35  # per side, on traded notional
    slippage_pips: float = 0.1  # per side, on traded notional
    markup_pct_per_year: float = 1.0  # on every open position (Giacomo, 2026-11-17)

    @property
    def fill_zero_spread(self) -> bool:
        return self.version == "g10-cost-v2"


class G10Criteria(Model):
    benchmark_percentile: float = 95.0
    min_positive_variants: int = 4
    subperiods: tuple[tuple[int, int], ...] = ((2004, 2007), (2008, 2010), (2011, 2013))
    min_positive_subperiods: int = 2
    validation_stress_multiplier: float = 1.5


class G10Prereg(Model):
    experiment_id: str
    hypothesis: str
    kind: Literal["g10_carry_momentum"] = "g10_carry_momentum"
    frozen_text: str
    frozen_text_sha256: str
    split: Literal["development", "validation"] = "development"
    validation_approval: str | None = None  # who approved and when, verbatim
    rate_lag_months: int = 1
    variants: tuple[G10Variant, ...]
    costs: G10Costs = G10Costs()
    cost_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0)
    benchmark_repetitions: int = 1000
    benchmark_seed: int = 20261103
    bootstrap_samples: int = 10000
    bootstrap_block_months: int = 3
    bootstrap_seed: int = 20261104
    criteria: G10Criteria = G10Criteria()


def load_g10(path: Path) -> tuple[G10Prereg, str]:
    raw = Path(path).read_bytes()
    pre = G10Prereg.model_validate_json(json.dumps(tomllib.loads(raw.decode("utf-8"))))
    names = [v.name for v in pre.variants]
    if not names or len(names) != len(set(names)):
        raise ProtocolError("variant names must be listed and unique")
    if len(names) > MAX_VARIANTS:
        raise ProtocolError(f"at most {MAX_VARIANTS} variants")
    for v in pre.variants:
        if not 1 <= v.k <= len(CURRENCIES) // 2:
            raise ProtocolError(f"{v.name}: k out of range")
        if (v.signal == "carry") != (v.lookback_months is None):
            raise ProtocolError(f"{v.name}: lookback only for momentum and combined")
        if v.lookback_months is not None and v.lookback_months < 1:
            raise ProtocolError(f"{v.name}: lookback must be positive")
    if 1.0 not in pre.cost_multipliers:
        raise ProtocolError("cost_multipliers must include 1.0")
    if pre.rate_lag_months < 1:
        raise ProtocolError("rates need at least one month of publication lag")
    if pre.split == "validation":
        if not pre.validation_approval:
            raise ProtocolError("validation needs the approval recorded in the file")
        if len(names) != 1:
            raise ProtocolError("validation runs only the best development variant")
    return pre, hashlib.sha256(raw).hexdigest()
