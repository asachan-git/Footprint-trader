"""Capital.com REST session login.

Endpoint: POST /api/v1/session
Headers: X-CAP-API-KEY: <api_key>
Body: {"identifier": "<email>", "password": "<password>"}
Response headers: CST, X-SECURITY-TOKEN — both required for WS auth + REST calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

DEMO_BASE = "https://demo-api-capital.backend-capital.com"
LIVE_BASE = "https://api-capital.backend-capital.com"


@dataclass
class Session:
    cst: str
    security_token: str
    account_id: str
    base_url: str


def login(demo: bool = True) -> Session:
    """Use env: CAPITAL_API_KEY, CAPITAL_IDENTIFIER (email), CAPITAL_PASSWORD."""
    api_key = os.environ["CAPITAL_API_KEY"]
    identifier = os.environ["CAPITAL_IDENTIFIER"]
    password = os.environ["CAPITAL_PASSWORD"]
    base = DEMO_BASE if demo else LIVE_BASE

    resp = requests.post(
        f"{base}/api/v1/session",
        headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
        json={"identifier": identifier, "password": password},
        timeout=10,
    )
    resp.raise_for_status()

    cst = resp.headers["CST"]
    sec = resp.headers["X-SECURITY-TOKEN"]
    body = resp.json()
    account_id = body.get("currentAccountId") or body["accounts"][0]["accountId"]

    return Session(cst=cst, security_token=sec, account_id=account_id, base_url=base)
