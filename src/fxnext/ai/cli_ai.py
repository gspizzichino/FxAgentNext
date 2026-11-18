"""``fxnext ai ...``: the shadow AI from the command line (scheduled on the PC)."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from fxnext.ai.store import AiStore


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("ai", help="shadow AI: dated judgements, never used by the engine")
    ai = p.add_subparsers(dest="ai_cmd", required=True)
    for name, text in (
        ("daily", "morning judgement on EUR, GBP, JPY, USD (Monday to Friday, once a day)"),
        ("events", "one judgement after each new central bank decision in the archive"),
        ("preview", "print what the model would read, without calling it"),
        ("status", "last runs and this month's spending"),
    ):
        q = ai.add_parser(name, help=text)
        q.add_argument("--news", type=Path, default=Path("data/news"))
        q.add_argument("--out", type=Path, default=Path("data/ai"))
        if name in ("daily", "events"):
            q.add_argument("--cap-usd", type=float, default=10.0, help="monthly spending cap")
            q.add_argument("--no-telegram", action="store_true")
        if name == "daily":
            q.add_argument("--force", action="store_true", help="run even if done today")


def _notifier(enabled: bool):  # type: ignore[no-untyped-def]
    if not enabled:
        return None
    from fxnext.ops.telegram import TelegramConfig, TelegramError, send_message

    cfg = TelegramConfig.from_env()
    if cfg is None:
        return None

    def notify(text: str) -> None:
        try:
            send_message(cfg, text)
        except TelegramError as exc:
            print(f"Telegram: {exc}", file=sys.stderr)

    return notify


def main(args: argparse.Namespace) -> int:
    store = AiStore(args.out)

    def now() -> datetime:
        return datetime.now(tz=UTC)

    if args.ai_cmd == "status":
        for r in list(store.records())[-10:]:
            line = f"{r.get('as_of', '')[:16]} {r.get('kind'):6} {r.get('status'):8}"
            line += f" {float(r.get('cost_usd') or 0):.3f} $"
            if r.get("error"):
                line += f"  {r['error']}"
            print(line)
        print(f"Spesa del mese: {store.month_spend_usd(now()):.2f} $")
        return 0
    if args.ai_cmd == "preview":
        from fxnext.ai.context import collect_sources, user_message
        from fxnext.ai.judge import MODEL, worst_case_usd
        from fxnext.ai.prompt import PROMPT_SHA, SYSTEM_PROMPT
        from fxnext.ai.schema import CURRENCIES

        at = now()
        sources = collect_sources(args.news, at)
        user = user_message("daily", at, CURRENCIES, sources, store.last_views(PROMPT_SHA))
        print(user)
        print(
            f"\n--- {len(sources)} fonti, {len(user)} caratteri, "
            f"costo massimo {worst_case_usd(MODEL, SYSTEM_PROMPT, user):.3f} $",
            file=sys.stderr,
        )
        return 0

    from fxnext.ai.judge import AnthropicJudge, JudgeError
    from fxnext.ai.run import run_daily, run_events

    try:
        judge = AnthropicJudge()
    except JudgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    notify = _notifier(not args.no_telegram)
    if args.ai_cmd == "daily":
        res = run_daily(args.news, store, judge, now, notify, args.cap_usd, args.force)
        if res is None:
            print("Niente da fare: fine settimana o giudizio di oggi già scritto.")
            return 0
        print(f"{res.status}: {res.message or 'giudizio scritto'}")
        return 0 if res.status == "ok" else 1
    results = run_events(args.news, store, judge, now, notify, args.cap_usd)
    for res in results:
        print(f"{res.record.get('trigger_title')}: {res.status} {res.message}")
    if not results:
        print("Nessuna nuova decisione di banca centrale.")
    return 0 if all(res.status == "ok" for res in results) else 1


__all__ = ["add_parser", "main"]
