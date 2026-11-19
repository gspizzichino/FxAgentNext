"""Web app (first slice): reads only, shows no holdout numbers, registers no writes."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from fxnext.cli import main as cli_main
from fxnext.dashboard import readers as rd
from fxnext.dashboard import telegram as tg
from fxnext.dashboard.app import create_app

FIX = Path(__file__).parent / "fixtures" / "dashboard"
PREREG = """experiment_id = "gbpusd-dev-001"
hypothesis = "Baseline su GBPUSD"
split = "development"

[[data]]
symbol = "GBPUSD"
m15_bid_dataset = "4e95b394"
path = "098f7a1d"
"""


def _outcomes(path: Path) -> None:
    ns = 1_420_070_400_000_000_000
    table = pa.table(
        {
            "stream": ["exec", "exec", "exec", "signal"],
            "variant": ["x1", "x1", "x1.5", "x1"],
            "symbol": ["GBPUSD"] * 4,
            "direction": ["LONG", "SHORT", "LONG", "LONG"],
            "reason": ["TARGET", "STOP", "TARGET", "TARGET"],
            "entry_ns": [ns, ns + 10**12, ns, ns],
            "exit_ns": [ns + 10**11, ns + 2 * 10**12, ns + 10**11, ns + 10**11],
            "r_gross": [2.0, -1.0, 1.9, 2.0],
            "r_net": [1.95, -1.05, 1.9, 2.0],
            "mae_r": [0.2, 1.0, 0.2, 0.2],
            "mfe_r": [2.0, 0.3, 2.0, 2.0],
            "outcome_id": ["a", "b", "c", "d"],
        }
    )
    pq.write_table(table, path)


@pytest.fixture
def tree(tmp_path: Path) -> rd.Sources:
    report = json.loads((FIX / "report_dev.json").read_text(encoding="utf-8"))
    research = tmp_path / "research"
    dev = research / report["fingerprint"]
    dev.mkdir(parents=True)
    (dev / "report.json").write_text(json.dumps(report), encoding="utf-8")
    _outcomes(dev / "outcomes.parquet")

    hold = copy.deepcopy(report)
    hold.update(fingerprint="f" * 64, experiment_id="gbpusd-hold-001", split="holdout")
    hold["streams"]["exec"]["x1"]["expectancy"] = 0.777
    (research / hold["fingerprint"]).mkdir()
    (research / hold["fingerprint"] / "report.json").write_text(json.dumps(hold))

    (research / "summary-dev-001").mkdir()
    shutil.copy(FIX / "summary_dev.json", research / "summary-dev-001" / "summary.json")

    prereg = tmp_path / "prereg"
    prereg.mkdir()
    (prereg / "gbpusd-dev-001.toml").write_text(PREREG, encoding="utf-8")
    (prereg / "s0-vr-dev-001.toml").write_text(
        'experiment_id = "s0-vr-dev-001"\nhypothesis = "VR"\nsplit = "development"\n'
    )

    raw = tmp_path / "raw" / "GBPUSD-BID"
    raw.mkdir(parents=True)
    recs = [
        {
            "symbol": "GBPUSD",
            "basis": "BID",
            "day": "2021-01-04",
            "status": "FAILED",
            "fetched_at": "2026-11-14T10:00:00+00:00",
        },
        {
            "symbol": "GBPUSD",
            "basis": "BID",
            "day": "2021-01-04",
            "status": "OK",
            "fetched_at": "2026-11-15T10:00:00+00:00",
        },
        {
            "symbol": "GBPUSD",
            "basis": "BID",
            "day": "2021-01-05",
            "status": "FAILED",
            "fetched_at": "2026-11-15T10:00:00+00:00",
        },
        {
            "symbol": "GBPUSD",
            "basis": "BID",
            "day": "2020-12-31",
            "status": "OK",
            "fetched_at": "2026-11-15T10:00:00+00:00",
        },
    ]
    (raw / "fetch_log.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    datasets = tmp_path / "datasets" / "abc"
    datasets.mkdir(parents=True)
    (datasets / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "abc" * 21 + "a",
                "symbol": "GBPUSD",
                "basis": "BID",
                "timeframe": "M15",
                "rows": 10,
                "rejected": 0,
                "coverage_start_ns": 1_420_070_400_000_000_000,
                "coverage_end_ns": 1_609_459_200_000_000_000,
                "quality": {"in_session_missing": 0},
            }
        )
    )

    frozen = tmp_path / "frozen"
    frozen.mkdir()
    (frozen / "doc.pdf").write_bytes(b"frozen")
    (frozen / "doc.pdf.sha256").write_text(hashlib.sha256(b"frozen").hexdigest() + "  doc.pdf\n")

    return rd.Sources(
        research=research,
        prereg=(prereg,),
        datasets=tmp_path / "datasets",
        raw=(tmp_path / "raw",),
        frozen=(frozen,),
    )


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


PAGES = [
    "/stato",
    "/esperimenti",
    "/confronto",
    "/preregistrazioni",
    "/dati",
    "/sistema",
    "/oggi",
    "/posizioni",
    "/equity",
    "/segnali",
    "/proposte",
    "/news",
]


def test_every_page_renders_and_nothing_on_disk_changes(tree: rd.Sources, tmp_path: Path) -> None:
    before = _snapshot(tmp_path)
    client = TestClient(create_app(tree))
    for page in PAGES:
        assert client.get(page).status_code == 200, page
    fp = json.loads((FIX / "report_dev.json").read_text())["fingerprint"]
    page = client.get(f"/esperimenti/{fp[:8]}")
    assert page.status_code == 200
    assert "gbpusd-dev-001" in page.text and "<svg" in page.text
    assert client.get("/confronto/summary-dev-001").status_code == 200
    assert client.get("/").status_code == 200  # redirect to /stato
    assert _snapshot(tmp_path) == before


def test_only_read_routes_exist(tree: rd.Sources) -> None:
    """Read-only, except one route that queues a kill switch / resume command (M3)."""

    app = create_app(tree)
    for route in app.routes:
        if isinstance(route, APIRoute):
            if route.path == "/oggi/comando":
                assert route.methods == {"POST"}
                continue
            assert route.methods <= {"GET", "HEAD"}, route.path
    client = TestClient(app)
    assert client.post("/stato").status_code == 405


def test_holdout_numbers_are_never_shown(tree: rd.Sources) -> None:
    rows = rd.list_experiments(tree.research)
    hold = next(r for r in rows if r["split"] == "holdout")
    assert hold["expectancy"] is None and hold["outcome"] == "SEALED"
    client = TestClient(create_app(tree))
    assert client.get("/esperimenti/" + "f" * 8).status_code == 404
    for page in ("/esperimenti", "/stato"):
        assert "0,777" not in client.get(page).text


def test_bad_fingerprints_are_rejected(tree: rd.Sources) -> None:
    client = TestClient(create_app(tree))
    for fp in ("abc", "../../etc", "zzzzzzzz"):
        assert client.get(f"/esperimenti/{fp}").status_code == 404


def test_preregistration_status(tree: rd.Sources) -> None:
    experiments = rd.list_experiments(tree.research)
    report = json.loads((FIX / "report_dev.json").read_text())
    # the fixture report was produced from a different file: the dashboard must say so
    by_id = {p["experiment_id"]: p for p in rd.list_preregistrations(tree.prereg, experiments)}
    assert by_id["s0-vr-dev-001"]["status"] == "NOT_RUN"
    assert by_id["gbpusd-dev-001"]["status"] == "CHANGED"
    # matching hash -> RUN
    for e in experiments:
        if e["experiment_id"] == "gbpusd-dev-001":
            e["prereg_sha256"] = rd.sha256_file(tree.prereg[0] / "gbpusd-dev-001.toml")
    by_id = {p["experiment_id"]: p for p in rd.list_preregistrations(tree.prereg, experiments)}
    assert by_id["gbpusd-dev-001"]["status"] == "RUN"
    assert report["experiment_id"] == "gbpusd-dev-001"


def test_frozen_documents_are_checked(tree: rd.Sources) -> None:
    assert rd.frozen_documents(tree.frozen)[0]["ok"] is True
    (tree.frozen[0] / "doc.pdf").write_bytes(b"changed")
    assert rd.frozen_documents(tree.frozen)[0]["ok"] is False


def test_coverage_uses_last_record_per_day(tree: rd.Sources) -> None:
    cov = rd.data_coverage(tree.raw)
    row = cov["rows"][0]
    assert (row["symbol"], row["basis"]) == ("GBPUSD", "BID")
    assert row["years"][2021] == {"ok": 1, "failed": 1}
    assert row["years"][2020] == {"ok": 1, "failed": 0}
    assert cov["latest_fetch"].startswith("2026-11-15")


def test_equity_curve_uses_exec_x1_only(tree: rd.Sources) -> None:
    fp = json.loads((FIX / "report_dev.json").read_text())["fingerprint"]
    curve = rd.equity_curve(tree.research / fp)
    assert curve["trades"] == 2
    assert curve["points"][-1][1] == pytest.approx(0.9)


def test_telegram_request_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tg.TelegramConfig.from_env({}) is None
    cfg = tg.TelegramConfig.from_env({tg.TOKEN_ENV: "123:ABC", tg.CHAT_ENV: "4567"})
    assert cfg is not None
    req = tg.build_request(cfg, "ciao")
    assert req.full_url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert req.data is not None and b"chat_id=4567" in req.data

    class Resp:
        def __init__(self, body: dict[str, Any]) -> None:
            self.body = json.dumps(body).encode()

        def read(self) -> bytes:
            return self.body

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    sent: list[urllib.request.Request] = []

    def ok(req: urllib.request.Request, timeout: float) -> Resp:
        sent.append(req)
        return Resp({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", ok)
    tg.send_message(cfg, "prova")
    assert len(sent) == 1

    def down(req: urllib.request.Request, timeout: float) -> Resp:
        raise OSError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(tg.TelegramError) as err:
        tg.send_message(cfg, "prova")
    assert "123:ABC" not in str(err.value)


def test_notify_test_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tg.TOKEN_ENV, raising=False)
    monkeypatch.delenv(tg.CHAT_ENV, raising=False)
    assert cli_main(["notify-test"]) == 1


def test_data_page_refreshes_itself(tree: rd.Sources) -> None:
    client = TestClient(create_app(tree))
    assert 'data-refresh="30"' in client.get("/dati").text
    assert "data-refresh" not in client.get("/stato").text
