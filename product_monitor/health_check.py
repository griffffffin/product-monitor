"""Live health check against the real marketplace sites - confirms the
scrapers' selectors still match the live page structure.

This is the one thing the unit tests structurally cannot do: they run against
static fixtures, so a silent site redesign (like the Jofogas Next.js rebuild
on 2026-07-03, which made that scraper return 0 results forever without a
single error) looks exactly like "no new listings today" to them.

Two numbers per site:
  - raw cards : how many listing elements the page-level extraction found.
                0 here means the extraction is almost certainly broken.
  - matched   : how many of those survived process_ad_item() with a very loose
                filter (no search terms, a huge max_price). 0 here does NOT
                mean broken - it can legitimately mean "nothing in stock right
                now" (especially for Moly, which only counts 5-star "Eladó"
                copies).

Hence is_failure(): a fetch error or 0 raw cards is a real problem worth an
email; 0 matches with raw cards present is a normal, quiet result.

Used from two places:
  - scripts/live_smoke_check.py - manual/CI run, prints a table, sets exit code
  - MultiMarketplaceMonitor.maybe_run_health_check() - the daily in-service run
    that emails on failure only

Read-only: doesn't touch seen-products.json and doesn't send email itself.
"""

import asyncio
import json
from typing import Any, Callable, Dict, List

from bs4 import BeautifulSoup

from .models import SearchConfig

HUGE_PRICE = 999_999_999


def dom_card_counter(tag: str, css_class: str) -> Callable[[str], int]:
    """Counts elements matching a static CSS selector - for the sites that
    still render listing cards directly into the HTML."""

    def counter(html: str) -> int:
        return len(BeautifulSoup(html, "html.parser").find_all(tag, class_=css_class))

    return counter


def jofogas_json_counter(html: str) -> int:
    """Jofogas (2026-07-03+) server-renders listings into a __NEXT_DATA__ JSON
    blob instead of static DOM markup - see JofogasScraper's docstring in
    product_monitor/scrapers/jofogas.py."""
    script = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if not script or not script.string:  # type: ignore[union-attr]
        return 0
    data = json.loads(script.string)  # type: ignore[union-attr]
    return len(data.get("props", {}).get("pageProps", {}).get("adList", {}).get("ads", []))


# Deliberately generic/neutral search targets - NOT the real
# product-monitor-config.json terms (those are personal: what the owner is
# actually shopping for). The point is to exercise each scraper's parsing, not
# to reproduce the owner's searches.
CHECKS: List[Dict[str, Any]] = [
    {
        "site": "HardverApro",
        "raw_counter": dom_card_counter("li", "media"),
        "search_config": SearchConfig(
            site="HardverApro",
            search_terms=[],
            max_price=HUGE_PRICE,
            url="https://hardverapro.hu/aprok/keres.php?stext=telefon",
        ),
    },
    {
        "site": "Moly",
        "raw_counter": dom_card_counter("div", "copy"),
        "search_config": SearchConfig(
            site="Moly",
            search_terms=[],
            max_price=HUGE_PRICE,
            url="https://moly.hu/konyvek/george-orwell-1984/elado-peldanyok",
        ),
    },
    {
        "site": "Vatera",
        "raw_counter": dom_card_counter("div", "gtm-impression"),
        "search_config": SearchConfig(site="Vatera", search_terms=["könyv"], max_price=HUGE_PRICE),
    },
    {
        "site": "Jofogas",
        "raw_counter": jofogas_json_counter,
        "search_config": SearchConfig(site="Jofogas", search_terms=["könyv"], max_price=HUGE_PRICE),
    },
]


async def check_site(monitor: Any, check: Dict[str, Any]) -> Dict[str, Any]:
    """Run one site's check against the live page. Reuses the monitor's own
    session (same headers/timeout as production) instead of building its own."""
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


async def run_health_check(monitor: Any) -> List[Dict[str, Any]]:
    """Check every site. The monitor must already have a session and scrapers
    (i.e. create_session() has been called)."""
    return list(await asyncio.gather(*(check_site(monitor, c) for c in CHECKS)))


def is_failure(result: Dict[str, Any]) -> bool:
    """A real problem: the fetch/parse errored, or the page yielded 0 raw cards
    (selector broken). 0 matches with raw cards present is NOT a failure - it
    just means nothing is for sale right now."""
    return "error" in result or result.get("raw", 0) == 0


def format_report(results: List[Dict[str, Any]]) -> str:
    """The per-site table, shared by the CLI script and the alert email."""
    lines = [f"{'Site':<14}{'Raw cards':<12}{'Matched':<10}Status", "-" * 50]
    for r in results:
        if "error" in r:
            raw = r.get("raw", "?")
            lines.append(f"{r['site']:<14}{raw!s:<12}{'?':<10}ERROR: {r['error']}")
        elif r["raw"] == 0:
            lines.append(
                f"{r['site']:<14}{r['raw']:<12}{r['matched']:<10}"
                "WARN: 0 raw cards - selector likely broken"
            )
        else:
            lines.append(f"{r['site']:<14}{r['raw']:<12}{r['matched']:<10}OK")
    return "\n".join(lines)


def build_health_alert(results: List[Dict[str, Any]]) -> tuple[str, str]:
    """Subject + body for the failure email (Hungarian, like every other
    outgoing message - see the language note in README.md)."""
    failed = [r for r in results if is_failure(r)]
    subject = f"Product Monitor: hibás oldal ({len(failed)} db)"

    body_parts = [
        "A napi ellenőrzés hibát talált - az alábbi oldal(ak) scraperje "
        "valószínűleg nem működik:\n\n",
        ", ".join(r["site"] for r in failed),
        "\n\n",
        format_report(results),
        "\n\n",
        "Magyarázat:\n",
        "- ERROR: az oldal letöltése vagy feldolgozása hibára futott.\n",
        "- WARN (0 raw cards): az oldal betöltődött, de egyetlen hirdetéskártyát "
        "sem talált a scraper - ez általában azt jelenti, hogy az oldal "
        "átalakult és a szelektorok elavultak.\n",
        "- A 0 találat (matched) önmagában NEM hiba, ezért erről nem is jön email.\n\n",
        "Ez a hiba lehet átmeneti is (pl. az oldal ideiglenesen blokkolja a kéréseket). "
        "Ha holnap is jön ilyen email, akkor valószínűleg tényleg elromlott valami.\n",
    ]
    return subject, "".join(body_parts)
