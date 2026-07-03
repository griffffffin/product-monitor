#!/usr/bin/env python3
"""Live check against the real marketplace sites - confirms the CSS
selectors in product_monitor still match the live page structure.

Deliberately NOT part of pytest: it needs real network access, is slow, and
is non-deterministic (site inventory changes constantly). Run it by hand
whenever a dependency gets bumped, or periodically, to catch a silent site
redesign that the unit tests (which run against static fixtures) can't see.

Reports two numbers per site:
  - raw cards   : how many listing elements the page-level extraction found
                  (li.media / div.copy / div.gtm-impression for the DOM-based
                  scrapers; the __NEXT_DATA__ JSON ad list for Jofogas, which
                  moved to a Next.js frontend on 2026-07-03)
                  -> if this is 0, the extraction is almost certainly broken.
  - matched     : how many of those survived process_ad_item() with a very
                  loose filter (no search terms, a huge max_price)
                  -> 0 here does NOT necessarily mean broken - it can
                  legitimately mean "nothing in stock right now" (this is
                  especially true for Moly, which only ever counts 5-star
                  "Eladó" copies). Only worth investigating if raw cards > 0
                  but matched stays 0 across repeated runs.

Doesn't touch seen-products.json and doesn't send email - read-only.

Usage: .venv/bin/python scripts/live_smoke_check.py
"""

import asyncio
import json
import sys
import types
from pathlib import Path

from bs4 import BeautifulSoup

# Makes `import product_monitor` work without requiring `pip install -e .`
# first - same mechanism tests/conftest.py uses, since this script is run as
# a plain path (`python3 scripts/live_smoke_check.py`, not `-m`), which only
# puts scripts/ itself on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import product_monitor as pm  # noqa: E402

HUGE_PRICE = 999_999_999


def dom_card_counter(tag: str, css_class: str):
    """Counts elements matching a static CSS selector - for the sites that
    still render listing cards directly into the HTML."""

    def counter(html: str) -> int:
        return len(BeautifulSoup(html, "html.parser").find_all(tag, class_=css_class))

    return counter


def jofogas_json_counter(html: str) -> int:
    """Jofogas (2026-07-03+) server-renders listings into a __NEXT_DATA__
    JSON blob instead of static DOM markup - see JofogasScraper's docstring
    in product_monitor/scrapers/jofogas.py."""
    script = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return 0
    data = json.loads(script.string)
    return len(data.get("props", {}).get("pageProps", {}).get("adList", {}).get("ads", []))


# Deliberately generic/neutral search targets - NOT the real product-monitor-config.json
# terms (those are personal: what the owner is actually shopping for).
CHECKS = [
    {
        "site": "HardverApro",
        "raw_counter": dom_card_counter("li", "media"),
        "search_config": pm.SearchConfig(
            site="HardverApro",
            search_terms=[],
            max_price=HUGE_PRICE,
            url="https://hardverapro.hu/aprok/keres.php?stext=telefon",
        ),
    },
    {
        "site": "Moly",
        "raw_counter": dom_card_counter("div", "copy"),
        "search_config": pm.SearchConfig(
            site="Moly",
            search_terms=[],
            max_price=HUGE_PRICE,
            url="https://moly.hu/konyvek/george-orwell-1984/elado-peldanyok",
        ),
    },
    {
        "site": "Vatera",
        "raw_counter": dom_card_counter("div", "gtm-impression"),
        "search_config": pm.SearchConfig(
            site="Vatera", search_terms=["könyv"], max_price=HUGE_PRICE
        ),
    },
    {
        "site": "Jofogas",
        "raw_counter": jofogas_json_counter,
        "search_config": pm.SearchConfig(
            site="Jofogas", search_terms=["könyv"], max_price=HUGE_PRICE
        ),
    },
]


async def check_one(monitor, check: dict) -> dict:
    site = check["site"]
    scraper = monitor.scrapers[site]
    search_config = check["search_config"]

    try:
        url = (
            scraper.build_search_url(search_config)
            if hasattr(scraper, "build_search_url")
            else search_config.url
        )
        async with monitor.session.get(url, timeout=30) as response:
            response.raise_for_status()
            html = await response.text(encoding="utf-8")
        raw_count = check["raw_counter"](html)
    except Exception as e:
        return {"site": site, "error": str(e)}

    try:
        matched = await scraper.fetch_advertisements(search_config)
    except Exception as e:
        return {"site": site, "raw": raw_count, "error": f"fetch_advertisements failed: {e}"}

    return {"site": site, "raw": raw_count, "matched": len(matched)}


async def main() -> int:
    monitor = object.__new__(pm.MultiMarketplaceMonitor)
    monitor.config = types.SimpleNamespace(max_concurrent_requests=4, request_timeout=30)
    await monitor.create_session()

    exit_code = 0
    try:
        results = await asyncio.gather(*(check_one(monitor, c) for c in CHECKS))
    finally:
        await monitor.session.close()

    print(f"{'Site':<14}{'Raw cards':<12}{'Matched':<10}Status")
    print("-" * 50)
    for r in results:
        if "error" in r:
            exit_code = 1
            raw = r.get("raw", "?")
            print(f"{r['site']:<14}{raw!s:<12}{'?':<10}ERROR: {r['error']}")
        elif r["raw"] == 0:
            exit_code = 1
            print(
                f"{r['site']:<14}{r['raw']:<12}{r['matched']:<10}WARN: 0 raw cards - selector likely broken"
            )
        else:
            print(f"{r['site']:<14}{r['raw']:<12}{r['matched']:<10}OK")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
