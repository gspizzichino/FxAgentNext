"""T5: crash-safe audit, checkpoint, resume and locking."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_dataset

from fxnext.audit.explain import explain
from fxnext.audit.lock import DestinationLocked, ExclusiveLock
from fxnext.audit.store import AuditDestination, IntegrityError, iter_rows, read_manifest
from fxnext.replay.runner import run_replay

START = datetime(2024, 1, 8, tzinfo=UTC)
END = datetime(2024, 2, 27, tzinfo=UTC)  # spans two month segments
POINTS = [
    "before_tmp_write",
    "after_tmp_fsync",
    "after_segment_replace",
    "after_manifest_tmp",
    "after_manifest_commit",
]


class Crash(Exception):
    pass


def _hook(point: str, nth: int = 1):  # type: ignore[no-untyped-def]
    seen = {"n": 0}

    def hook(p: str) -> None:
        if p == point:
            seen["n"] += 1
            if seen["n"] == nth:
                raise Crash(p)

    return hook


@pytest.fixture(scope="module")
def ds(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("ds")
    return make_dataset(root), root


@pytest.fixture(scope="module")
def reference(ds, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    r = run_replay([ds[0]], tmp_path_factory.mktemp("ref"), START, END, code_version="t")
    assert r is not None
    return r


@pytest.mark.parametrize("point", POINTS)
def test_t5_1_t5_2_crash_and_resume(point: str, ds, reference, tmp_path: Path) -> None:
    with pytest.raises(Crash):
        run_replay([ds[0]], tmp_path, START, END, code_version="t", crash_hook=_hook(point))
    r = run_replay([ds[0]], tmp_path, START, END, code_version="t")
    assert r is not None
    assert r.output_hash == reference.output_hash
    assert r.evaluations == reference.evaluations
    rows = list(iter_rows(r.path))
    assert len({row["request_id"] for row in rows}) == len(rows) == reference.evaluations
    manifest = read_manifest(r.path)
    assert [s["seq"] for s in manifest["segments"]] == list(range(1, len(manifest["segments"]) + 1))
    assert not list((r.path / "segments").glob(".tmp-*"))
    if point in ("after_tmp_fsync", "after_segment_replace", "after_manifest_tmp"):
        assert any((r.path / "quarantine").iterdir())


def test_t5_1_kill_process_and_resume(ds, reference, tmp_path: Path) -> None:
    script = textwrap.dedent(f"""
        import os, sys
        from datetime import datetime, UTC
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from fxnext.data.store import load_dataset
        from fxnext.replay.runner import run_replay
        d = load_dataset({str(ds[1])!r}, {ds[0].ref.dataset_id!r})
        def hook(p):
            if p == "after_segment_replace":
                os._exit(9)
        run_replay([d], {str(tmp_path)!r}, datetime(2024,1,8,tzinfo=UTC),
                   datetime(2024,2,27,tzinfo=UTC), code_version="t", crash_hook=hook)
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode == 9
    r = run_replay([ds[0]], tmp_path, START, END, code_version="t")  # T5.5: lock released
    assert r is not None and r.output_hash == reference.output_hash


def test_t5_3_corrupt_segment(ds, reference, tmp_path: Path) -> None:
    import shutil

    shutil.copytree(reference.path, tmp_path / reference.path.name)
    seg = tmp_path / reference.path.name / "segments" / "seg-000001.parquet"
    data = bytearray(seg.read_bytes())
    data[len(data) // 2] ^= 0x01
    seg.write_bytes(bytes(data))
    with pytest.raises(IntegrityError):
        run_replay([ds[0]], tmp_path, START, END, code_version="t")


def test_t5_4_t5_5_concurrent_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "run" / "LOCK"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
        import time
        from fxnext.audit.lock import ExclusiveLock
        lock = ExclusiveLock(__import__("pathlib").Path({str(lock_path)!r}))
        lock.acquire()
        print("locked", flush=True)
        time.sleep(30)
    """),
        ],
        stdout=subprocess.PIPE,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == b"locked"
    t0 = time.monotonic()
    with pytest.raises(DestinationLocked):
        ExclusiveLock(lock_path).acquire()
    assert time.monotonic() - t0 < 1.0
    holder.kill()
    holder.wait()
    with ExclusiveLock(lock_path):  # released by the OS when the holder died
        pass


def test_t5_4_second_replay_rejected(ds, tmp_path: Path) -> None:
    from datetime import timedelta

    from helpers import CONFIG

    from fxnext.domain.hashing import content_hash
    from fxnext.replay.runner import fingerprint_inputs

    inputs = fingerprint_inputs([ds[0]], CONFIG, START, END, timedelta(0), timedelta(0), "t")
    fp = content_hash("RunFingerprint/1", inputs)
    with AuditDestination(tmp_path, fp, inputs):
        before = sorted(p.name for p in (tmp_path / fp).rglob("*"))
        with pytest.raises(DestinationLocked):
            run_replay([ds[0]], tmp_path, START, END, code_version="t")
        assert sorted(p.name for p in (tmp_path / fp).rglob("*")) == before


def test_t5_6_explain_reads_only_committed(ds, tmp_path: Path) -> None:
    assert run_replay([ds[0]], tmp_path, START, END, code_version="t", stop_after=700) is None
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    manifest = read_manifest(run_dir)
    assert manifest["status"] == "RUNNING" and len(manifest["segments"]) == 1
    (run_dir / "segments" / ".tmp-999-garbage").write_bytes(b"not parquet")
    (run_dir / "segments" / "seg-000009.parquet").write_bytes(b"not parquet")
    out = explain(run_dir, "EURUSD", datetime(2024, 1, 17, 10, 15, tzinfo=UTC))
    assert out["evaluation_as_of"] == "2024-01-17T10:00:00+00:00"
    late = explain(run_dir, "EURUSD", datetime(2024, 2, 20, 10, 15, tzinfo=UTC))
    assert late["evaluation_as_of"] < "2024-02-01"  # nothing uncommitted is visible
    weekend = explain(run_dir, "EURUSD", datetime(2024, 1, 20, 12, tzinfo=UTC))
    assert weekend["answer"] == "MARKET_CLOSED_BY_CALENDAR"


def test_dirty_code_version_refused(ds, tmp_path: Path) -> None:
    from fxnext.replay.runner import ReplayInputError

    with pytest.raises(ReplayInputError):
        run_replay([ds[0]], tmp_path, START, END, code_version="abc-dirty")


def test_completed_destination_is_reused(ds, reference, tmp_path: Path) -> None:
    again = run_replay([ds[0]], reference.path.parent, START, END, code_version="t")
    assert again is not None and again.output_hash == reference.output_hash
    assert json.loads((reference.path / "manifest.json").read_text())["status"] == "COMPLETED"
