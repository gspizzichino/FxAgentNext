"""``fxnext demo ...``: engine on the Naga demo account (M3)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fxnext.execution.config import EXAMPLE_TOML, DemoConfig
from fxnext.ops import store as ops

ACTIONS = (
    "init-config",
    "check",
    "run",
    "status",
    "stop",
    "resume",
    "resume-week",
    "clear-suspension",
    "watchdog",
)


def add_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("demo", help="engine on the MT5 demo account (orders only on demo)")
    p.add_argument("action", choices=ACTIONS)
    p.add_argument("--config", type=Path, default=Path("demo.toml"))
    p.add_argument("--terminal", help="path of terminal64.exe if not the default one")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run: read MT5 and evaluate, but never send an order (separate database)",
    )
    p.add_argument("--max-age", type=float, default=600.0, help="watchdog: seconds")
    p.add_argument("--iterations", type=int, default=None, help=argparse.SUPPRESS)


def main(args: argparse.Namespace) -> int:
    from fxnext.execution.runner import now_ns

    if args.action == "init-config":
        if args.config.exists():
            print(f"{args.config} esiste già: non lo sovrascrivo.", file=sys.stderr)
            return 1
        args.config.write_text(EXAMPLE_TOML, encoding="utf-8")
        print(f"Scritto {args.config}: inserisci il login demo in [account] logins.")
        return 0
    config = DemoConfig.load(args.config)
    if args.dry_run:
        from fxnext.execution.runner import dry_run_config

        config = dry_run_config(config)
    t = now_ns()
    if args.action in ("stop", "resume", "resume-week", "clear-suspension"):
        command = {
            "stop": ops.CMD_KILL_ON,
            "resume": ops.CMD_KILL_OFF,
            "resume-week": ops.CMD_RESUME_WEEKLY,
            "clear-suspension": ops.CMD_CLEAR_SUSPENSION,
        }[args.action]
        cid = ops.enqueue_command(config.ops_db, command, "cli", t)
        print(f"Comando {command} in coda (n. {cid}): il motore lo applica al prossimo giro.")
        return 0
    if args.action == "status":
        from fxnext.execution.runner import status_text

        if not config.ops_db.exists():
            print("Database operativo assente: il motore non è mai partito.")
            return 1
        store = ops.OpsStore(config.ops_db)
        try:
            print(status_text(store, t))
        finally:
            store.close()
        return 0
    if args.action == "watchdog":
        from fxnext.execution.runner import watchdog
        from fxnext.ops.telegram import TelegramConfig, TelegramError, send_message

        msg = watchdog(config.ops_db, args.max_age, t)
        if msg is None:
            print("Motore attivo.")
            return 0
        print(msg)
        cfg = TelegramConfig.from_env()
        if cfg is not None:
            try:
                send_message(cfg, "⚠️ " + msg)
            except TelegramError as exc:
                print(str(exc), file=sys.stderr)
        return 1

    from fxnext.execution.mt5 import MT5Broker

    broker = MT5Broker(args.terminal)
    try:
        if args.action == "check":
            from fxnext.execution.runner import readonly_check

            print(json.dumps(readonly_check(broker, config, t), indent=2, ensure_ascii=False))
            return 0
        # run
        from fxnext.execution.broker import Broker
        from fxnext.execution.engine import DemoEngine
        from fxnext.execution.runner import ReadOnlyBroker, TelegramLink, run_loop
        from fxnext.ops.telegram import TelegramConfig

        active: Broker = ReadOnlyBroker(broker) if args.dry_run else broker
        store = ops.OpsStore(config.ops_db)
        tcfg = TelegramConfig.from_env()
        link = TelegramLink(tcfg) if tcfg is not None else None
        mode = "SOLA LETTURA (nessun ordine)" if args.dry_run else "DEMO con ordini automatici"
        print(f"Motore {mode}. Database {config.ops_db}. Ctrl+C per fermare.")
        if link is None:
            print("Telegram non configurato: niente avvisi né comandi.", file=sys.stderr)
        else:
            store.alert(t, "INFO", f"Motore avviato: {mode}.")
        engine = DemoEngine(active, store, config)
        try:
            run_loop(engine, store, telegram=link, max_iterations=args.iterations)
        except KeyboardInterrupt:
            print("Fermato.")
        finally:
            store.observe(now_ns(), "shutdown", {})
            store.close()
        return 0
    finally:
        broker.shutdown()


__all__ = ["add_parser", "main"]
