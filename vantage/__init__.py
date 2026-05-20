"""Vantage Markets MT5 ingress via MetaApi.cloud bridge.

Thin wrapper around the generic `exness/` module (which is broker-agnostic
underneath — pure MetaApi + MT5). Vantage offers MT4/MT5 accounts; this
package preconfigures defaults for Vantage's XAU/USD symbol naming and
session conventions.

Setup (one-time):
  1. Open a Vantage MT5 account (demo or live)
  2. Sign up at https://metaapi.cloud (free tier available)
  3. In MetaApi dashboard: Add MT5 account
       Broker:   Vantage International (or VantageInternational-Demo for demo)
       Server:   from your Vantage credentials (e.g. VantageInternational-Live 5)
       Login:    your MT5 account number
       Password: investor (read-only) password is sufficient
  4. Wait for account state = CONNECTED + DEPLOYED
  5. Generate a token in MetaApi → Profile → Tokens
  6. Populate .env with:
       METAAPI_TOKEN=<token>
       METAAPI_ACCOUNT_ID=<account_uuid>
       METAAPI_REGION=new-york   (or london / singapore as configured)

Discover exact Vantage symbol name:
  PYTHONPATH=. python3 -m vantage.list_symbols
  # prints all XAU/GOLD-named symbols on your account

Run live ingress:
  python3 -m vantage.main --symbol XAUUSD --price-step 0.1
  # POSTs each closed 1m bar to http://localhost:5000/ingest
"""
