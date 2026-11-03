"""Crash-safe audit destination: Parquet segments + manifest commit (ADR-0014, contracts §10)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from fxnext.audit.lock import ExclusiveLock
from fxnext.domain.contracts import Abstention, Candidate, StrategyEvaluation
from fxnext.domain.hashing import canonical_bytes, chain_start, chain_step
from fxnext.domain.timeutil import to_ns

MANIFEST_VERSION = 1
CrashHook = Callable[[str], None]

SEGMENT_SCHEMA = pa.schema(
    [
        ("request_id", pa.string()),
        ("evaluation_hash", pa.string()),
        ("as_of_ns", pa.int64()),
        ("symbol", pa.string()),
        ("strategy_id", pa.string()),
        ("outcome", pa.string()),
        ("reason_code", pa.string()),
        ("candidate_id", pa.string()),
        ("direction", pa.string()),
        ("evaluation_json", pa.string()),
    ]
)


class IntegrityError(RuntimeError):
    pass


class CompletedDestination(RuntimeError):
    pass


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return  # not supported on Windows (documented limitation)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_of(ev: StrategyEvaluation) -> dict[str, Any]:
    out = ev.outcome
    return {
        "request_id": ev.request.request_id,
        "evaluation_hash": ev.evaluation_hash,
        "as_of_ns": to_ns(ev.request.as_of),
        "symbol": ev.request.symbol,
        "strategy_id": ev.request.strategy_id,
        "outcome": "CANDIDATE" if isinstance(out, Candidate) else "ABSTENTION",
        "reason_code": out.reason_code.value if isinstance(out, Abstention) else None,
        "candidate_id": out.candidate_id if isinstance(out, Candidate) else None,
        "direction": out.direction.value if isinstance(out, Candidate) else None,
        "evaluation_json": canonical_bytes(ev).decode("utf-8"),
    }


@dataclass
class AuditDestination:
    """Single-writer destination ``root/<run_fingerprint>/``."""

    root: Path
    run_fingerprint: str
    fingerprint_inputs: dict[str, Any]
    crash_hook: CrashHook = lambda point: None
    manifest: dict[str, Any] = field(default_factory=dict)
    _lock: ExclusiveLock | None = None
    _buffer: list[dict[str, Any]] = field(default_factory=list)
    _head: str = ""

    @property
    def path(self) -> Path:
        return Path(self.root) / self.run_fingerprint

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = ExclusiveLock(self.path / "LOCK")
        self._lock.acquire()
        try:
            self._recover()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def __enter__(self) -> AuditDestination:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _recover(self) -> None:
        manifest_path = self.path / "manifest.json"
        segments_dir = self.path / "segments"
        segments_dir.mkdir(exist_ok=True)
        if not manifest_path.exists():
            if any(segments_dir.glob("seg-*.parquet")):
                raise IntegrityError("segments present without manifest: manual intervention")
            self.manifest = {
                "manifest_version": MANIFEST_VERSION,
                "commit": 0,
                "run_id": uuid.uuid4().hex,
                "run_fingerprint": self.run_fingerprint,
                "fingerprint_inputs": self.fingerprint_inputs,
                "status": "RUNNING",
                "segments": [],
                "last_committed_as_of_ns": None,
                "chain_head": chain_start(self.run_fingerprint),
                "output_hash": None,
                "evaluation_count": 0,
            }
            self._commit_manifest()
        else:
            self.manifest = json.loads(manifest_path.read_text())
            if self.manifest["run_fingerprint"] != self.run_fingerprint:
                raise IntegrityError("manifest fingerprint mismatch")
        self.verify_segments()
        listed = {s["file"] for s in self.manifest["segments"]}
        quarantine = self.path / "quarantine"
        for item in sorted(segments_dir.iterdir()):
            if item.name.startswith(".tmp-") or item.name not in listed:
                quarantine.mkdir(exist_ok=True)
                shutil.move(str(item), str(quarantine / f"{uuid.uuid4().hex[:8]}-{item.name}"))
        for item in self.path.glob(".tmp-*"):
            quarantine.mkdir(exist_ok=True)
            shutil.move(str(item), str(quarantine / f"{uuid.uuid4().hex[:8]}-{item.name}"))
        self._head = self.manifest["chain_head"]

    # -- reading -------------------------------------------------------------
    def verify_segments(self) -> str:
        """Check checksums, seq continuity, unique request ids and the hash chain."""

        head = chain_start(self.run_fingerprint)
        seen: set[str] = set()
        last_as_of = -1
        for expected_seq, seg in enumerate(self.manifest["segments"], start=1):
            if seg["seq"] != expected_seq:
                raise IntegrityError("segment seq not contiguous")
            path = self.path / "segments" / seg["file"]
            if not path.exists() or _sha256_file(path) != seg["sha256"]:
                raise IntegrityError(f"segment {seg['file']} missing or checksum mismatch")
            table = pq.read_table(path)
            if table.num_rows != seg["rows"]:
                raise IntegrityError("segment row count mismatch")
            for rid, eh, as_of in zip(
                table.column("request_id").to_pylist(),
                table.column("evaluation_hash").to_pylist(),
                table.column("as_of_ns").to_pylist(),
                strict=True,
            ):
                if rid in seen:
                    raise IntegrityError(f"duplicate request_id {rid}")
                if as_of < last_as_of:
                    raise IntegrityError("as_of not monotonic across segments")
                seen.add(rid)
                last_as_of = as_of
                head = chain_step(head, rid, eh)
            if head != seg["chain_head"]:
                raise IntegrityError(f"chain mismatch after segment {seg['seq']}")
        if head != self.manifest["chain_head"]:
            raise IntegrityError("manifest chain_head mismatch")
        return head

    @property
    def last_committed_as_of_ns(self) -> int | None:
        value = self.manifest.get("last_committed_as_of_ns")
        return None if value is None else int(value)

    @property
    def completed(self) -> bool:
        return bool(self.manifest["status"] == "COMPLETED")

    # -- writing -------------------------------------------------------------
    def append(self, evaluation: StrategyEvaluation) -> None:
        if self.completed:
            raise CompletedDestination("destination already COMPLETED")
        row = row_of(evaluation)
        last = self._buffer[-1]["as_of_ns"] if self._buffer else self.last_committed_as_of_ns
        if last is not None and row["as_of_ns"] < last:
            raise IntegrityError("evaluations must be appended in as_of order")
        self._buffer.append(row)

    def pending(self) -> int:
        return len(self._buffer)

    def commit_segment(self) -> None:
        if not self._buffer:
            return
        seq = len(self.manifest["segments"]) + 1
        name = f"seg-{seq:06d}.parquet"
        segments_dir = self.path / "segments"
        tmp = segments_dir / f".tmp-{seq:06d}-{uuid.uuid4().hex}"
        table = pa.Table.from_pylist(self._buffer, schema=SEGMENT_SCHEMA)
        self.crash_hook("before_tmp_write")
        pq.write_table(table, tmp)
        with tmp.open("ab") as handle:  # write access: fsync needs it on Windows
            os.fsync(handle.fileno())
        self.crash_hook("after_tmp_fsync")
        checksum = _sha256_file(tmp)
        os.replace(tmp, segments_dir / name)
        _fsync_dir(segments_dir)
        self.crash_hook("after_segment_replace")
        head = self._head
        for row in self._buffer:
            head = chain_step(head, row["request_id"], row["evaluation_hash"])
        self.manifest["segments"].append(
            {
                "seq": seq,
                "file": name,
                "sha256": checksum,
                "rows": len(self._buffer),
                "first_request_id": self._buffer[0]["request_id"],
                "last_request_id": self._buffer[-1]["request_id"],
                "first_as_of_ns": self._buffer[0]["as_of_ns"],
                "last_as_of_ns": self._buffer[-1]["as_of_ns"],
                "chain_head": head,
            }
        )
        self.manifest["chain_head"] = head
        self.manifest["last_committed_as_of_ns"] = self._buffer[-1]["as_of_ns"]
        self.manifest["evaluation_count"] += len(self._buffer)
        self._commit_manifest(before_replace=lambda: self.crash_hook("after_manifest_tmp"))
        self.crash_hook("after_manifest_commit")
        self._head = head
        self._buffer = []

    def complete(self) -> str:
        self.commit_segment()
        self.manifest["status"] = "COMPLETED"
        self.manifest["output_hash"] = self.manifest["chain_head"]
        self._commit_manifest()
        return str(self.manifest["output_hash"])

    def _commit_manifest(self, before_replace: Callable[[], None] | None = None) -> None:
        self.manifest["commit"] = int(self.manifest.get("commit", 0)) + 1
        data = json.dumps(self.manifest, indent=2, sort_keys=True).encode("utf-8")
        path = self.path / "manifest.json"
        tmp = path.with_name(f".tmp-manifest-{uuid.uuid4().hex}")
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(tmp, path)
        _fsync_dir(self.path)


def read_manifest(run_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((Path(run_dir) / "manifest.json").read_text())
    return result


def iter_rows(run_dir: Path) -> Iterator[dict[str, Any]]:
    """Rows of committed segments only, in order (never globs the directory)."""

    manifest = read_manifest(run_dir)
    for seg in manifest["segments"]:
        table = pq.read_table(Path(run_dir) / "segments" / seg["file"])
        yield from table.to_pylist()
