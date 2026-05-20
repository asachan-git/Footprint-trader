"""Discover available symbols on the Vantage MT5 account (via MetaApi).

Re-exports the exness/ diagnostic — broker-agnostic.

Run: PYTHONPATH=. python3 -m vantage.list_symbols
"""

from exness.list_symbols import main  # noqa: F401
import asyncio

if __name__ == "__main__":
    asyncio.run(main())
