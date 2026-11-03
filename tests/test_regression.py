"""A1/A4/A7: regression hashes (identical on Linux and Windows) and run-wide invariants."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from conftest import make_dataset

from fxnext.audit.store import iter_rows
from fxnext.replay.runner import run_replay

START = datetime(2024, 1, 8, tzinfo=UTC)
END = datetime(2024, 2, 27, tzinfo=UTC)

EXPECTED_DATASET_ID = "dc80b3dbb3603610ba43a3b7d56234d08a858f84423bfb0afea160a9e98060c5"
EXPECTED_FINGERPRINT = "86683eaa216aa86def056e0b3d8b2cc73f5434b686d23e3c9a9669ee406a8d76"
EXPECTED_OUTPUT_HASH = "05941f857c57805cc0bcb470194b210471e5760f4e0f40120cb0e92651c0e15b"


def test_regression_hashes(tmp_path: Path) -> None:
    ds = make_dataset(tmp_path / "ds")
    a = run_replay([ds], tmp_path / "a", START, END, code_version="regression")
    b = run_replay([ds], tmp_path / "b", START, END, code_version="regression")
    assert a is not None and b is not None
    assert (a.run_fingerprint, a.output_hash) == (b.run_fingerprint, b.output_hash)
    assert ds.ref.dataset_id == EXPECTED_DATASET_ID
    assert a.run_fingerprint == EXPECTED_FINGERPRINT
    assert a.output_hash == EXPECTED_OUTPUT_HASH


def test_every_evaluation_has_exactly_one_outcome(tmp_path: Path) -> None:
    ds = make_dataset(
        tmp_path / "ds",
        gaps=[1706886000000000000],  # 2024-02-02 15:00 UTC
        flats=[1707130800000000000],
    )  # 2024-02-05 11:00 UTC
    r = run_replay([ds], tmp_path / "r", START, END, code_version="regression")
    assert r is not None
    reasons: dict[str, int] = {}
    for row in iter_rows(r.path):
        ev = json.loads(row["evaluation_json"])
        out = ev["outcome"]
        assert ("candidate_id" in out) != ("reason_code" in out)
        failed = [f for f in ev["filters"] if f["status"] == "FAILED"]
        if failed:
            assert out["reason_code"] == failed[0]["reason_code"]
        key = row["reason_code"] or "CANDIDATE"
        reasons[key] = reasons.get(key, 0) + 1
    assert reasons["CANDIDATE"] > 0 and reasons["DATA_GAP"] > 0
    # 22 windows contain the flat slot; one of them also contains the gap and the
    # earlier gate (window_available) wins, so 21 report FLAT_BAR_IN_WINDOW.
    assert reasons["FLAT_BAR_IN_WINDOW"] == 21
