"""Telegram push alerts — fire-and-forget, never blocks trading.

No-op when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset, so the rest of
the system runs unchanged without configuration.

Each send runs on a daemon thread with a short timeout; failures are logged
at WARNING and swallowed — a dead notifier must never stall the bar loop or
an order.

Usage:
  from utils.notify import notify
  notify("ORDER FILLED", f"XAUUSD+ long 0.5 @ {price}")
"""

from __future__ import annotations

import logging
import os
import threading
from urllib import parse, request

LOG = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


def _enabled() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return token, chat
    return None


def _send_blocking(title: str, body: str) -> None:
    creds = _enabled()
    if not creds:
        return
    token, chat = creds
    text = f"*{title}*\n{body}" if body else f"*{title}*"
    data = parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = request.Request(_API.format(token=token), data=data, method="POST")
        with request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as e:
        LOG.warning(f"[notify] telegram send failed: {e}")


def notify(title: str, body: str = "") -> None:
    """Fire-and-forget push. Returns immediately; sends on a daemon thread."""
    if not _enabled():
        return
    threading.Thread(target=_send_blocking, args=(title, body), daemon=True).start()
