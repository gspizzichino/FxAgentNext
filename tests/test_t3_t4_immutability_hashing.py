"""T3 (deep immutability) and T4 (identities and hashing)."""

from __future__ import annotations

import hashlib
import inspect
import re
import typing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from conftest import make_dataset
from helpers import CONFIG, H1, STRATEGY, evaluate_at, snapshot_at

import fxnext.domain.contracts as contracts
from fxnext.domain.contracts import (
    EMPTY_STATE,
    StrategyConfig,
    StrategyEvaluation,
    StructuralEvent,
)
from fxnext.domain.enums import Direction, PriceBasis, Timeframe
from fxnext.domain.frozen import FrozenMap, freeze_json
from fxnext.domain.hashing import canonical_bytes, content_hash
from fxnext.domain.model import Model
from fxnext.domain.series import BarSeries
from fxnext.strategy.donchian import DonchianParams

SIGNAL = datetime(2024, 2, 7, 10, tzinfo=UTC)


def _series_cols() -> dict[str, np.ndarray]:
    return {
        "open_time_ns": np.array([0]),
        "close_time_ns": np.array([H1.ns]),
        "available_at_ns": np.array([H1.ns]),
        "availability": np.array([1]),
        "status": np.array([0]),
        "open": np.array([1.0]),
        "high": np.array([1.5]),
        "low": np.array([0.5]),
        "close": np.array([1.25]),
        "volume": np.array([3.0]),
        "source_count": np.array([4]),
    }


def test_t3_1_columns_cannot_be_written() -> None:
    s = BarSeries("EURUSD", H1, PriceBasis.BID, _series_cols())
    for name in ("close", "available_at_ns", "status"):
        arr = s.column(name)
        with pytest.raises(ValueError):
            arr[0] = 0
        with pytest.raises(ValueError):
            arr.setflags(write=True)
        with pytest.raises(ValueError):
            arr[:1][0] = 0
    with pytest.raises(AttributeError):
        s.symbol = "GBPUSD"  # type: ignore[misc]


def test_t3_2_defensive_copy() -> None:
    cols = _series_cols()
    s = BarSeries("EURUSD", H1, PriceBasis.BID, cols)
    before = content_hash("BarSeries/1", s)
    cols["close"][0] = 1.4
    cols["open_time_ns"][0] = 99
    assert float(s.close[0]) == 1.25 and content_hash("BarSeries/1", s) == before


def test_t3_3_frozen_containers() -> None:
    fm = FrozenMap({"b": 1, "a": 2})
    assert list(fm) == ["a", "b"]
    with pytest.raises(TypeError):
        fm["c"] = 3  # type: ignore[index]
    assert not hasattr(fm, "update")
    with pytest.raises(AttributeError):
        fm._data = {}  # type: ignore[misc]
    fj = freeze_json({"x": [1, {"y": [2]}]})
    assert fj["x"] == (1, FrozenMap({"y": (2,)}))
    with pytest.raises(AttributeError):
        fj["x"].append(3)
    with pytest.raises(ValueError):
        freeze_json({"x": float("nan")})
    with pytest.raises(TypeError):
        FrozenMap({"a": [1, 2]})
    with pytest.raises(ValueError):
        contracts.FilterResult(
            name="x",
            kind="DATA_GATE",
            status="PASSED",  # type: ignore[arg-type]
            measure=set(),
        )


_MUTABLE = {list, dict, set, bytearray, np.ndarray}


def _origins(tp: object) -> set[object]:
    out = {typing.get_origin(tp) or tp}
    for arg in typing.get_args(tp):
        out |= _origins(arg)
    return out


def test_t3_4_no_mutable_field_types() -> None:
    models = [
        obj
        for _, obj in inspect.getmembers(contracts, inspect.isclass)
        if issubclass(obj, Model) and obj is not Model
    ] + [DonchianParams]
    assert len(models) > 10
    for model in models:
        for name, field in model.model_fields.items():
            assert not (_origins(field.annotation) & _MUTABLE), f"{model.__name__}.{name}"


def test_t3_5_evaluate_does_not_mutate(dataset) -> None:
    snap = snapshot_at(dataset, SIGNAL)
    hashes = (content_hash("MarketSnapshot/1", snap), CONFIG.config_hash, EMPTY_STATE.state_hash)
    STRATEGY.evaluate(snap, CONFIG, EMPTY_STATE)
    assert hashes == (
        content_hash("MarketSnapshot/1", snap),
        content_hash("StrategyConfig/1", CONFIG),
        EMPTY_STATE.state_hash,
    )


# -- T4 ----------------------------------------------------------------------


def test_t4_1_canonical_vectors() -> None:
    value = {
        "b": None,
        "a": (1, 2.5, -0.0),
        "d": datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC),
        "s": frozenset({"y", "x"}),
        "dec": Decimal("1.2300"),
        "dir": Direction.LONG,
    }
    expected = (
        b'{"a":[1,"f:0x1.4000000000000p+1","f:0x0.0p+0"],"b":null,'
        b'"d":"t:1000000000","dec":"d:1.23","dir":"LONG","s":["x","y"]}'
    )
    assert canonical_bytes(value) == expected
    tag = "Vector/1"
    manual = hashlib.sha256(b"fxnext-hash-v1\x00" + tag.encode() + b"\x00" + expected).hexdigest()
    assert content_hash(tag, value) == manual
    assert content_hash(tag, value) == (
        "637a900436cf3d1af143b882871619eb58ed21b397adfd450d513a3df100855e"
    )
    with pytest.raises(ValueError):
        content_hash(tag, float("nan"))
    with pytest.raises(ValueError):
        content_hash(tag, datetime(2024, 1, 1))


def test_t4_2_one_ulp_changes_hash() -> None:
    x = 1.1
    assert content_hash("V/1", x) != content_hash("V/1", np.nextafter(x, 2.0).item())


def test_t4_3_event_vs_candidate_identity(dataset) -> None:
    # find a slot with a candidate
    from helpers import CAL

    from fxnext.domain.timeutil import from_ns

    grid = CAL.slot_grid(H1, datetime(2024, 1, 15, tzinfo=UTC), datetime(2024, 2, 25, tzinfo=UTC))
    for open_ns in grid.tolist():
        ev = evaluate_at(dataset, from_ns(open_ns))
        if isinstance(ev.outcome, contracts.Candidate):
            break
    else:
        pytest.fail("no candidate in synthetic data")
    snap = snapshot_at(dataset, from_ns(open_ns))

    def with_params(**kw: object) -> StrategyEvaluation:
        cfg = StrategyConfig(
            strategy_id=CONFIG.strategy_id,
            strategy_version=CONFIG.strategy_version,
            params=DonchianParams(**kw),  # type: ignore[arg-type]
            detection_param_names=CONFIG.detection_param_names,
        )
        return STRATEGY.evaluate(snap, cfg, EMPTY_STATE)

    base = with_params()
    other_stop = with_params(stop_atr_mult=1.75)
    assert base.request.config_hash != other_stop.request.config_hash
    assert base.events[0].event_id == other_stop.events[0].event_id
    assert base.outcome.candidate_id != other_stop.outcome.candidate_id  # type: ignore[union-attr]
    p = DonchianParams(channel_slots=19)
    cfg19 = StrategyConfig(
        strategy_id=CONFIG.strategy_id,
        strategy_version="1",
        params=p,
        detection_param_names=CONFIG.detection_param_names,
    )
    assert cfg19.detection_params() != CONFIG.detection_params()


def _event(disc: object) -> StructuralEvent:
    return StructuralEvent(
        event_id=content_hash("StructuralEvent/1", disc),
        kind="k",
        timeframe=Timeframe.H1,
        anchor_open_time=SIGNAL,
        direction=Direction.LONG,
        discriminator=disc,
    )


def test_t4_4_duplicate_events(dataset) -> None:
    ev = evaluate_at(dataset, SIGNAL)
    a, b = _event({"level": 1.0}), _event({"level": 2.0})
    assert a.event_id != b.event_id
    with pytest.raises(ValueError, match="duplicate event_id"):
        StrategyEvaluation(
            request=ev.request,
            engine_version="x",
            snapshot_hash="x",
            events=(a, a),
            outcome=ev.outcome,
            filters=ev.filters,
            measures=ev.measures,
            next_state=EMPTY_STATE,
        )


def test_t4_5_same_request_different_content(tmp_path: Path, dataset) -> None:
    from fxnext.audit.store import AuditDestination, IntegrityError

    ev = evaluate_at(dataset, SIGNAL)
    dest = AuditDestination(tmp_path, "ab" * 32, {})
    with dest:
        dest.append(ev)
        dest.commit_segment()
    # tamper: rewrite the manifest chain as if a different evaluation_hash was stored
    import json

    mpath = dest.path / "manifest.json"
    m = json.loads(mpath.read_text())
    m["chain_head"] = "00" * 32
    m["segments"][0]["chain_head"] = "00" * 32
    mpath.write_text(json.dumps(m))
    with pytest.raises(IntegrityError):
        AuditDestination(tmp_path, "ab" * 32, {}).open()


def test_t4_7_no_forbidden_math_in_core() -> None:
    root = Path(__file__).parents[1] / "src" / "fxnext"
    pattern = re.compile(
        r"\b(math\.(exp|log|sqrt|pow|sin|cos)|np\.(sum|mean|exp|log|sqrt)|"
        r"\.sum\(|\.mean\(|float32)\b"
    )
    for path in list((root / "strategy").glob("*.py")) + list((root / "domain").glob("*.py")):
        assert not pattern.search(path.read_text()), path.name


def test_t4_dataset_id_stable(tmp_path: Path) -> None:
    a = make_dataset(tmp_path / "a")
    b = make_dataset(tmp_path / "b")
    assert a.ref.dataset_id == b.ref.dataset_id
    c = make_dataset(tmp_path / "c", seed=8)
    assert c.ref.dataset_id != a.ref.dataset_id
