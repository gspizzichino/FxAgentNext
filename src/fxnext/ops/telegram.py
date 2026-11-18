"""Telegram: outgoing notifications and, for the demo engine, two incoming commands.

The bot token and chat id come from environment variables and stay on the
machine that runs the app; they are never written to disk by this module.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

TOKEN_ENV = "FXNEXT_TELEGRAM_TOKEN"
CHAT_ENV = "FXNEXT_TELEGRAM_CHAT_ID"
API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TelegramConfig | None:
        env = dict(os.environ) if env is None else env
        token, chat = env.get(TOKEN_ENV, "").strip(), env.get(CHAT_ENV, "").strip()
        if not token or not chat:
            return None
        return cls(token, chat)

    @property
    def masked_chat(self) -> str:
        return self.chat_id[:2] + "…" + self.chat_id[-2:] if len(self.chat_id) > 4 else "…"


def build_request(cfg: TelegramConfig, text: str) -> urllib.request.Request:
    data = urllib.parse.urlencode(
        {"chat_id": cfg.chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    return urllib.request.Request(
        f"{API}/bot{cfg.token}/sendMessage",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _call(req: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("description", "")
        except (OSError, ValueError):
            detail = ""
        raise TelegramError(f"Telegram ha risposto {exc.code}: {detail}") from None
    except OSError as exc:
        # never echo the URL: it contains the token
        raise TelegramError(f"Telegram non raggiungibile: {type(exc).__name__}") from None
    if not isinstance(body, dict):
        raise TelegramError("risposta Telegram non valida")
    return body


def send_message(cfg: TelegramConfig, text: str, timeout: float = 10.0) -> None:
    body = _call(build_request(cfg, text), timeout)
    if not body.get("ok"):
        raise TelegramError(f"Telegram ha rifiutato il messaggio: {body.get('description', '')}")


@dataclass(frozen=True)
class IncomingCommand:
    update_id: int
    command: str  # "stop" | "stato" | other text, lower case, without "/"


def get_commands(
    cfg: TelegramConfig, offset: int | None, timeout: float = 10.0
) -> list[IncomingCommand]:
    """Messages to the bot since ``offset``, **only from the configured chat**.

    Messages from any other chat are dropped (their update_id is still returned
    as an empty command so that the offset moves past them).
    """

    params = {"timeout": "0", "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = str(offset)
    req = urllib.request.Request(
        f"{API}/bot{cfg.token}/getUpdates?" + urllib.parse.urlencode(params), method="GET"
    )
    body = _call(req, timeout)
    if not body.get("ok"):
        raise TelegramError(f"getUpdates rifiutato: {body.get('description', '')}")
    out = []
    for upd in body.get("result", []):
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = str(msg.get("text") or "").strip().lower()
        cmd = text.lstrip("/").split("@")[0].split(" ")[0] if chat == cfg.chat_id else ""
        out.append(IncomingCommand(int(upd["update_id"]), cmd))
    return out


__all__ = [
    "IncomingCommand",
    "get_commands",
    "CHAT_ENV",
    "TOKEN_ENV",
    "TelegramConfig",
    "TelegramError",
    "build_request",
    "send_message",
]
