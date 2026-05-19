"""Headless Playwright variant of the userscript — CI harness only.

Injects the same WS/fetch hook via page.add_init_script before navigation.
Useful for unattended replay batch runs. Not the primary live path.

Run: python -m browser_bridge.playwright.scrape --url https://gocharting.com/...
"""

import argparse
import asyncio
from pathlib import Path

HOOK = (Path(__file__).parent.parent / "userscript" / "gocharting_footprint.user.js").read_text()


async def run(url: str, headless: bool) -> None:
    from playwright.async_api import async_playwright  # lazy import; optional dep

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx = await browser.new_context()
        await ctx.add_init_script(HOOK)
        page = await ctx.new_page()
        await page.goto(url)
        print(f"[scrape] loaded {url}; hook installed; idle until Ctrl-C")
        await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.url, args.headless))


if __name__ == "__main__":
    main()
