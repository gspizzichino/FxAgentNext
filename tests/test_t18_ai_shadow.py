"""T18: shadow AI (ADR-0018). Fake model only: no network, no API key."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fxnext.ai import context as ctx
from fxnext.ai.judge import AnthropicJudge, JudgeError, RawAnswer, cost_usd
from fxnext.ai.prompt import PROMPT_SHA, prompt_sha
from fxnext.ai.run import digest_text, run_daily, run_events, run_once
from fxnext.ai.schema import ModelAnswer, check_answer
from fxnext.ai.store import AiStore
from fxnext.dashboard.app import create_app
from fxnext.dashboard.readers import Sources
from fxnext.news import centralbanks as cb
from fxnext.news.archive import Snapshot, store

# Frozen with the proposal on 2026-11-18. A new prompt is a new version: change both.
FROZEN_PROMPT_SHA = "6d3fa1ed42fc768f694d406fff01ee597026992f3a6a2046a5aaf0e2a8aeee64"

T0 = datetime(2026, 11, 18, 6, 30, tzinfo=UTC)  # Wednesday, 07:30 in Rome


def _put(root: Path, source: str, body: Any, at: datetime, url: str = "u") -> None:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    store(root, Snapshot(source, url, 200, raw), at)


def _cb_text(root: Path, bank: str, ccy: str, title: str, at: datetime, decision: bool) -> None:
    body = {
        "bank": bank,
        "currency": ccy,
        "title": title,
        "published": "",
        "decision": decision,
        "text": f"{title}: the committee decided...",
    }
    _put(root, "cb-text", body, at, url=f"https://x/{title}")


@pytest.fixture
def news(tmp_path: Path) -> Path:
    root = tmp_path / "news"
    cal = [
        {
            "country": "USD",
            "date": "2026-11-18T14:30:00+00:00",
            "title": "CPI m/m",
            "impact": "High",
            "forecast": "0.3%",
            "previous": "0.2%",
        },
        {"country": "CHF", "date": "2026-11-18T10:00:00+00:00", "title": "x", "impact": "Low"},
    ]
    _put(root, "calendar", cal, T0 - timedelta(hours=1))
    cot = [
        {
            "cftc_contract_market_code": "099741",
            "report_date_as_yyyy_mm_dd": "2026-11-11",
            "lev_money_positions_long": "100",
            "lev_money_positions_short": "40",
            "asset_mgr_positions_long": "10",
            "asset_mgr_positions_short": "5",
            "open_interest_all": "1000",
        }
    ]
    _put(root, "cot", cot, T0 - timedelta(days=2))
    _cb_text(root, "ecb", "EUR", "Monetary policy decisions", T0 - timedelta(days=20), True)
    return root


class FakeJudge:
    model = "claude-opus-5-5"

    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def ask(self, system: str, user: str, schema: dict[str, Any]) -> RawAnswer:
        self.calls.append(user)
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        return RawAnswer(json.dumps(ans), self.model, 20_000, 2_000, "end_turn")


def _view(
    ccy: str, tone: int = 0, surprise: int | None = None, srcs: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "currency": ccy,
        "tone": tone,
        "surprise": surprise,
        "event_risk_24h": "medio",
        "bias_5d": 0,
        "confidence": 0.4,
        "sources": list(srcs),
        "rationale": "Le fonti non indicano una direzione chiara.",
    }


def _all(tone: int = 0) -> dict[str, Any]:
    return {"judgements": [_view(c, tone, None, ("S1",)) for c in ("EUR", "GBP", "JPY", "USD")]}


def test_prompt_is_frozen() -> None:
    assert prompt_sha() == FROZEN_PROMPT_SHA
    assert len(PROMPT_SHA) == 64


def test_context_is_point_in_time(news: Path) -> None:
    _cb_text(news, "fed", "USD", "FOMC statement", T0 + timedelta(minutes=1), True)
    sources = ctx.collect_sources(news, T0)
    labels = " ".join(s.label for s in sources)
    assert "FED" not in labels  # received after the judgement time
    assert "ECB" in labels and [s.id for s in sources] == ["S1", "S2", "S3"]
    cal = sources[0].text
    assert "CPI m/m" in cal and "CHF" not in cal
    assert "fondi a leva netti +60" in sources[1].text


def test_old_central_bank_texts_are_left_out(news: Path) -> None:
    sources = ctx.collect_sources(news, T0 + timedelta(days=90))
    assert all("ECB" not in s.label for s in sources)


def test_check_answer_rules() -> None:
    ok = ModelAnswer.model_validate(_all())
    check_answer(ok, "daily", ("EUR", "GBP", "JPY", "USD"), {"S1"})
    with pytest.raises(ValueError, match="fonti sconosciute"):
        check_answer(ok, "daily", ("EUR", "GBP", "JPY", "USD"), {"S2"})
    with pytest.raises(ValueError, match="valute attese"):
        check_answer(ok, "daily", ("EUR",), {"S1"})
    with pytest.raises(ValueError, match="surprise mancante"):
        check_answer(
            ModelAnswer.model_validate({"judgements": [_view("EUR")]}), "event", ("EUR",), set()
        )
    with pytest.raises(ValueError, match="solo dopo"):
        check_answer(
            ModelAnswer.model_validate({"judgements": [_view("EUR", 0, 1)]}),
            "daily",
            ("EUR",),
            set(),
        )


def test_schema_rejects_out_of_range_values() -> None:
    bad = _all()
    bad["judgements"][0]["tone"] = 3
    with pytest.raises(ValueError):
        ModelAnswer.model_validate(bad)
    bad = _all()
    bad["judgements"][0]["confidence"] = 1.5
    with pytest.raises(ValueError):
        ModelAnswer.model_validate(bad)


def test_successful_run_is_appended_with_hashes(news: Path, tmp_path: Path) -> None:
    st = AiStore(tmp_path / "ai")
    judge = FakeJudge([_all(1), _all(-1)])
    res = run_once("daily", news, st, judge, lambda: T0)
    assert res.status == "ok"
    rec = res.record
    assert rec["prompt_sha"] == PROMPT_SHA and rec["model"] == "claude-opus-5-5"
    assert rec["cost_usd"] == pytest.approx(cost_usd("claude-opus-5-5", 20_000, 2_000))
    eur = rec["judgements"][0]
    assert eur["currency"] == "EUR" and eur["tone_change"] is None
    assert eur["sources_sha256"] == [rec["sources"][0]["sha256"]]
    res2 = run_once("daily", news, st, judge, lambda: T0 + timedelta(days=1))
    assert res2.record["judgements"][0]["tone_change"] == -2
    assert "tono 1, bias 5g 0" in judge.calls[1]  # previous judgement shown
    assert len(list(st.records())) == 2


def test_bad_answer_and_refusal_are_recorded_as_failed(news: Path, tmp_path: Path) -> None:
    st = AiStore(tmp_path / "ai")
    bad = {"judgements": [_view("EUR")]}
    spent = RawAnswer("", "claude-opus-5-5", 1000, 50, "refusal")
    judge = FakeJudge([bad, JudgeError("rifiuto", spent), JudgeError("rete")])
    for _ in range(3):
        assert run_once("daily", news, st, judge, lambda: T0).status == "failed"
    recs = list(st.records())
    assert "fuori schema" in recs[0]["error"]
    assert recs[1]["cost_usd"] > 0 and recs[2]["cost_usd"] == 0
    assert all("judgements" not in r for r in recs)


def test_spending_cap_skips_the_call(news: Path, tmp_path: Path) -> None:
    st = AiStore(tmp_path / "ai")
    st.append({"kind": "daily", "status": "ok", "cost_usd": 9.95, "written_at": T0.isoformat()}, T0)
    judge = FakeJudge([_all()])
    res = run_once("daily", news, st, judge, lambda: T0)
    assert res.status == "skipped" and judge.calls == []
    assert "tetto di spesa" in res.message


def test_daily_once_on_weekdays(news: Path, tmp_path: Path) -> None:
    st = AiStore(tmp_path / "ai")
    sent: list[str] = []
    judge = FakeJudge([_all(), _all()])
    assert run_daily(news, st, judge, lambda: T0, sent.append) is not None
    assert run_daily(news, st, judge, lambda: T0 + timedelta(hours=3), sent.append) is None
    saturday = datetime(2026, 11, 21, 6, 30, tzinfo=UTC)
    assert run_daily(news, st, judge, lambda: saturday, sent.append) is None
    assert len(sent) == 1 and sent[0].startswith("AI in ombra") and "EUR: tono +0" in sent[0]


def test_event_runs_once_per_decision_and_alerts_on_big_surprise(
    news: Path, tmp_path: Path
) -> None:
    st = AiStore(tmp_path / "ai")
    at = T0 + timedelta(hours=12)
    _cb_text(
        news,
        "fed",
        "USD",
        "Federal Reserve issues FOMC statement",
        at - timedelta(minutes=20),
        True,
    )
    _cb_text(
        news,
        "fed",
        "USD",
        "Minutes of the Federal Open Market Committee",
        at - timedelta(minutes=10),
        False,
    )
    sent: list[str] = []
    judge = FakeJudge([{"judgements": [_view("USD", 1, 2, ("S3",))]}])
    results = run_events(news, st, judge, lambda: at, sent.append)
    assert [r.status for r in results] == ["ok"]  # the old ECB decision is outside the window
    assert "Decisione appena pubblicata" in judge.calls[0]
    assert len(sent) == 1 and "sorpresa +2 su USD" in sent[0]
    assert run_events(news, st, judge, lambda: at + timedelta(minutes=30), sent.append) == []


def test_digest_reports_failure(news: Path, tmp_path: Path) -> None:
    st = AiStore(tmp_path / "ai")
    res = run_once("daily", news, st, FakeJudge([JudgeError("rete")]), lambda: T0)
    assert "non scritto" in digest_text(res)


# ------------------------------------------------------------------ Anthropic adapter
class _FakeMessages:
    def __init__(self, stop: str) -> None:
        self.stop = stop
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self.stop,
            model=kwargs["model"],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text="{}"),
            ],
        )


def test_anthropic_adapter_uses_structured_output_and_no_fallback() -> None:
    msgs = _FakeMessages("end_turn")
    judge = AnthropicJudge(client=SimpleNamespace(messages=msgs))
    ans = judge.ask("sys", "user", {"type": "object"})
    assert ans.text == "{}" and ans.input_tokens == 10
    assert msgs.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert msgs.kwargs["model"] == "claude-opus-5-5"
    assert "fallbacks" not in msgs.kwargs and "temperature" not in msgs.kwargs


def test_anthropic_adapter_refusal_is_an_error_with_cost() -> None:
    judge = AnthropicJudge(client=SimpleNamespace(messages=_FakeMessages("refusal")))
    with pytest.raises(JudgeError) as exc:
        judge.ask("sys", "user", {})
    assert exc.value.answer is not None and exc.value.answer.output_tokens == 5


# ------------------------------------------------------------------ central bank archive
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Federal Reserve issues FOMC statement</title>
<link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20261029a.htm</link>
<pubDate>Wed, 29 Oct 2026 18:00:00 GMT</pubDate></item>
<item><title>Federal Reserve announces new board member</title>
<link>https://www.federalreserve.gov/newsevents/x.htm</link></item>
<item><title>FOMC statement mirror</title><link>https://evil.example.com/fomc.htm</link></item>
</channel></rss>"""

PAGE = (
    b"<html><head><script>x()</script></head><body><nav>menu</nav>"
    b"<p>The Committee decided to lower the target range.</p></body></html>"
)


def test_central_bank_archive(tmp_path: Path) -> None:
    calls: list[str] = []

    def get(url: str) -> tuple[int, bytes]:
        calls.append(url)
        return 200, RSS if url == cb.BANKS["fed"].feed else PAGE

    out = cb.archive_banks(tmp_path, get, lambda: T0, ("fed",))
    assert out == {"fed": "saved 1 new"}
    assert "https://evil.example.com/fomc.htm" not in calls
    rec = json.loads(next((tmp_path / "cb-text").glob("*/*.jsonl")).read_text().splitlines()[0])
    body = rec["body"]
    assert body["decision"] is True and body["currency"] == "USD"
    assert body["text"] == "The Committee decided to lower the target range."
    calls.clear()
    assert cb.archive_banks(tmp_path, get, lambda: T0, ("fed",)) == {"fed": "unchanged"}
    assert calls == [cb.BANKS["fed"].feed]  # the page is not downloaded twice


def test_atom_feed_and_garbage() -> None:
    atom = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        b"<title>Monetary policy decisions</title>"
        b'<link href="https://www.ecb.europa.eu/press/pr/date/2026/html/x.en.html"/>'
        b"<updated>2026-10-30</updated></entry></feed>"
    )
    items = cb.parse_feed(cb.BANKS["ecb"], atom)
    assert items[0].url.endswith("x.en.html") and cb.is_decision(cb.BANKS["ecb"], items[0].title)
    assert cb.parse_feed(cb.BANKS["ecb"], b"not xml") == []


# ------------------------------------------------------------------ dashboard
def test_dashboard_shows_ai_context(news: Path, tmp_path: Path) -> None:
    st = AiStore(tmp_path / "ai")
    run_once("daily", news, st, FakeJudge([_all(2)]), lambda: datetime.now(UTC))
    app = create_app(Sources(ai=tmp_path / "ai", ops=tmp_path / "none.sqlite"))
    html = TestClient(app).get("/oggi").text
    assert "Contesto AI" in html and "Solo osservazione" in html and "+2" in html
    empty = create_app(Sources(ai=tmp_path / "nothing", ops=tmp_path / "none.sqlite"))
    assert "non ha ancora scritto nulla" in TestClient(empty).get("/oggi").text


def test_cli_writes_utf8_even_on_a_cp1252_stream(
    news: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    from fxnext.cli import main

    recent = datetime.now(UTC) - timedelta(minutes=1)  # preview reads at the real time
    _cb_text(news, "boj", "JPY", "Statement on Monetary Policy ¥ — 日本銀行", recent, True)
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    args = ["ai", "preview", "--news", str(news), "--out", str(tmp_path / "ai")]
    assert main(args) == 0
    out.flush()
    assert "日本銀行".encode() in raw.getvalue()
