"""Pre-registration of the hypotheses S0-S3 and S7 (TOML, hashed like M2's).

Common rules of the frozen text that the format enforces:

* development split only (rule 1): validation needs a new file and approval;
* every variant listed up front, at most 9 (rule 5, decision P3);
* ``exploratory`` marks S2 and S3 (decision P1): their best outcome is
  INCONCLUSIVE with a positive sign;
* the swap model exists only for S2 (decision P2, exception to E8).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Literal

from fxnext.domain.model import Model
from fxnext.research.execution import CostModel, ExecParams
from fxnext.research.protocol import ProtocolError

HYP_PROTOCOL_VERSION = "hypothesis-protocol-v1"
MAX_VARIANTS = 9
Kind = Literal[
    "s0_variance_ratio", "s1_failure_test", "s2_daily_trend", "s3_sunday_gap", "s7_intraday"
]


class PathRef(Model):
    symbol: str
    path: str  # content id of the M1 BID+ASK path used by the M2 development report
    reference_report: str


class Variant(Model):
    name: str
    tf: Literal["M15", "H1"] | None = None  # S1
    w: int | None = None  # S1 failure window
    k: float | None = None  # S1 target in R; S3 gap threshold in ATR20
    n: int | None = None  # S2 breakout days
    s: float | None = None  # S2, S3 stop in ATR20
    m: int | None = None  # S2 exit channel days
    d: int | None = None  # S3 delay in minutes
    legs: tuple[Literal["home", "ny"], ...] | None = None  # S7


class SwapModel(Model):
    version: Literal["swap-v1"] = "swap-v1"
    rates: Literal["policy-rates-v1"] = "policy-rates-v1"
    markup_pct_per_year: float  # charged on both sides; 1.0 chosen by Giacomo 2026-11-17


class Threshold(Model):
    """Threshold to propose validation (common rules, frozen 2026-11-11)."""

    benchmark_percentile: float = 95.0
    min_positive_pairs: int = 2
    plateau_positive: int = 5  # of plateau_of variants with positive net expectancy
    plateau_of: int = 8
    entry_test_min_win_rate: float | None = None  # S1 only: 0.52


class HypothesisPrereg(Model):
    experiment_id: str
    hypothesis: str
    kind: Kind
    frozen_text_sha256: str  # prereg-S0-S3-S7-congelato-2026-11-11.pdf
    split: Literal["development"] = "development"
    exploratory: bool = False
    data: tuple[PathRef, ...]
    variants: tuple[Variant, ...] = ()
    main_variant: str | None = None  # S2: the only variant judged
    execution: ExecParams = ExecParams()
    costs: CostModel = CostModel()
    cost_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0)
    benchmark_repetitions: int = 1000
    benchmark_seed: int = 20261103
    bootstrap_samples: int = 10000
    bootstrap_seed: int = 20261104
    entry_test_bars: int = 10
    threshold: Threshold = Threshold()
    swap: SwapModel | None = None
    phase1_ks: tuple[float, ...] = ()  # S3 phase 1 gap thresholds


def load_hypothesis(path: Path) -> tuple[HypothesisPrereg, str]:
    raw = Path(path).read_bytes()
    pre = HypothesisPrereg.model_validate_json(json.dumps(tomllib.loads(raw.decode("utf-8"))))
    names = [v.name for v in pre.variants]
    if len(names) != len(set(names)):
        raise ProtocolError("variant names must be unique")
    if len(names) > MAX_VARIANTS:
        raise ProtocolError(f"at most {MAX_VARIANTS} variants per hypothesis")
    if pre.kind == "s0_variance_ratio" and names:
        raise ProtocolError("S0 is descriptive: no variants")
    if pre.kind != "s0_variance_ratio" and not names:
        raise ProtocolError("a trading hypothesis needs its variants")
    if pre.main_variant is not None and pre.main_variant not in names:
        raise ProtocolError("main_variant is not a listed variant")
    if (pre.swap is not None) != (pre.kind == "s2_daily_trend"):
        raise ProtocolError("the swap model is an exception for S2 only")
    if 1.0 not in pre.cost_multipliers:
        raise ProtocolError("cost_multipliers must include 1.0")
    symbols = [d.symbol for d in pre.data]
    if len(symbols) != len(set(symbols)):
        raise ProtocolError("one path per symbol")
    return pre, hashlib.sha256(raw).hexdigest()
