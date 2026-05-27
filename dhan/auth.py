"""DhanHQ client factory. Reads DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN from env."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from dhanhq import DhanContext, dhanhq

LOG = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_client: dhanhq | None = None


def get_client() -> dhanhq:
    global _client
    if _client is None:
        client_id = os.environ["DHAN_CLIENT_ID"]
        access_token = os.environ["DHAN_ACCESS_TOKEN"]
        ctx = DhanContext(client_id, access_token)
        _client = dhanhq(ctx)
        LOG.info(f"[dhan_auth] client initialised id={client_id[:6]}***")
    return _client
