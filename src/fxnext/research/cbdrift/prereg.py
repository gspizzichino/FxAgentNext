"""Pre-registration h1-cbdrift-001 (frozen 2026-11-27).

Everything the frozen text fixes is fixed here, in code: banks, pairs, splits,
which rate series a run uses, timing, threshold. The TOML can only choose the
split and record the approval for the single validation run.
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

CBDRIFT_PROTOCOL_VERSION = "cbdrift-protocol-v1"
FROZEN_TEXT = "prereg-h1-cbdrift-001-congelato-2026-11-27.md"
FROZEN_TEXT_SHA256 = "b1bb6179f60e15645235e17e02a3344d5e924d7440ae9625a04667afd47eb494"

# [start, end). Validation is a single run on 2014-2026 (Giacomo, 2026-11-23).
CBDRIFT_SPLITS: dict[str, tuple[date, date]] = {
    "development": (date(2004, 1, 1), date(2014, 1, 1)),
    "validation": (date(2014, 1, 1), date(2026, 11, 1)),
}
# Rates are read from here: the first signal of 2004 needs 250 + 5 weekdays before it
# (frozen text §1.3, 'riscaldamento'). FX candles start in 2003 as in M5.
RATE_WARMUP_START = date(2002, 11, 1)
FX_FIRST_YEAR = 2003

# Bank -> currency. USD is the Fed (traded against the basket of admitted currencies).
BANKS = {
    "Fed": "USD",
    "ECB": "EUR",
    "BoE": "GBP",
    "SNB": "CHF",
    "RBA": "AUD",
    "RBNZ": "NZD",
    "BoC": "CAD",
    "Riksbank": "SEK",
}
# Norges Bank / NOK is in the frozen universe but has no forward-looking 1-month
# rate before 2020: it fails the admission rule in both runs (frozen text §1.3).
CANDIDATES = ("EUR", "GBP", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK")
PAIRS = {
    "EUR": "EURUSD",
    "GBP": "GBPUSD",
    "AUD": "AUDUSD",
    "NZD": "NZDUSD",
    "CHF": "USDCHF",
    "CAD": "USDCAD",
    "SEK": "USDSEK",
    "NOK": "USDNOK",
}

# Frozen text §1.2 and §1.1.
RBA_FIRST_MEETING = date(2007, 12, 1)
SNB_FLOOR = (date(2011, 9, 6), date(2015, 1, 15))  # SNB meetings in this span excluded

# Frozen text §3.
SIGNAL_LAG = 12  # S = 12 valid weekdays before T
ENTRY_LAG = 11  # entry at the close of T-11
EXIT_LAG = 1  # exit at the close of T-1
AVERAGE_DAYS = 5
MEDIAN_DAYS = 250
THRESHOLD_BP = 10.0
MAX_FILL_WEEKDAYS = 5
ADMISSION_COVERAGE = 0.90
EONIA_LAST_DAY = date(2021, 12, 31)
ESTR_TO_EONIA_BP = 8.5

# Frozen text §4.
SLIPPAGE_PIPS = 0.1
MARKUP_PCT_PER_YEAR = 1.6
COST_MULTIPLIERS = (1.0, 1.5, 2.0)


class CbdriftCriteria(Model):
    benchmark_percentile: float = 95.0
    stress_multiplier: float = 1.5
    subperiods: tuple[tuple[int, int], ...] = ((2004, 2007), (2008, 2010), (2011, 2013))
    min_events_subperiod: int = 10
    min_positive_subperiods: int = 2


class CbdriftPrereg(Model):
    experiment_id: Literal["h1-cbdrift-001"]
    hypothesis: str
    kind: Literal["cb_drift"] = "cb_drift"
    frozen_text: str
    frozen_text_sha256: str
    split: Literal["development", "validation"] = "development"
    validation_approval: str | None = None  # who approved and when, verbatim
    benchmark_repetitions: int = 1000
    benchmark_seed: int = 20261124
    bootstrap_samples: int = 10000
    bootstrap_seed: int = 20261123
    criteria: CbdriftCriteria = CbdriftCriteria()


def load_cbdrift(path: Path) -> tuple[CbdriftPrereg, str]:
    raw = Path(path).read_bytes()
    pre = CbdriftPrereg.model_validate_json(json.dumps(tomllib.loads(raw.decode("utf-8"))))
    if pre.frozen_text != FROZEN_TEXT or pre.frozen_text_sha256 != FROZEN_TEXT_SHA256:
        raise ProtocolError("the TOML must point to the frozen text h1-cbdrift-001 and its sha256")
    if (pre.benchmark_repetitions, pre.benchmark_seed) != (1000, 20261124):
        raise ProtocolError("random-direction benchmark is fixed: 1000 repetitions, seed 20261124")
    if (pre.bootstrap_samples, pre.bootstrap_seed) != (10000, 20261123):
        raise ProtocolError("bootstrap is fixed: 10000 samples, seed 20261123")
    if pre.criteria != CbdriftCriteria():
        raise ProtocolError("criteria are fixed by the frozen text")
    if pre.split == "validation" and not pre.validation_approval:
        raise ProtocolError("validation needs Giacomo's approval recorded in the file")
    if pre.split == "development" and pre.validation_approval:
        raise ProtocolError("validation_approval only goes with split = validation")
    return pre, hashlib.sha256(raw).hexdigest()


# Frozen text §1.3: one 1-month series and one overnight/policy series per currency and
# run (keys of ``sources.SERIES``). No series switch inside a run.
ONE_MONTH: dict[str, dict[str, str | None]] = {
    "USD": {"development": "DTB4WK", "validation": "DTB4WK"},
    "EUR": {"development": "EURIBOR1M", "validation": "EURIBOR1M"},
    "GBP": {"development": "IUDVNEA", "validation": "BOE_OIS1M"},
    "AUD": {"development": "FIRMMOIS1D", "validation": "FIRMMBAB30D"},
    "CAD": {"development": "TB.CDN.30D.MID", "validation": "TB.CDN.30D.MID"},
    "SEK": {"development": "SETB1MBENCHC", "validation": "SETB1MBENCHC"},
    "NZD": {"development": "INM.DB01.NZZV", "validation": "INM.DB01.NZZV"},
    "CHF": {"development": "SAR1M", "validation": "SAR1M"},
    "NOK": {"development": None, "validation": None},
}
OVERNIGHT: dict[str, str] = {
    "USD": "DFF",
    "EUR": "EUR_ON",  # EONIA to 2021-12-31, then ESTR + 8.5 bp
    "GBP": "IUDSOIA",
    "AUD": "FIRMMCRTD",
    "CAD": "AVG.INTWO",
    "SEK": "SECBREPOEFF",
    "NZD": "INM.DP1.N",
    "CHF": "SARON",
    "NOK": "IR/B.KPRA.SD.R",
}
