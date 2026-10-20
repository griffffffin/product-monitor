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
from typing import Any, Callable, Dict, List, Set

from bs4 import BeautifulSoup

from .models import SearchConfig
from .scrapers.vinted import count_item_cards as vinted_card_counter
from .scrapers.vinted import unknown_conditions as vinted_unknown_conditions

HUGE_PRICE = 999_999_999

# Retry a failing site a couple of times before believing it. The production
# failure emails (Sept 2026) were all transient connection hiccups - a single
# "broken pipe" / "cannot connect" / timeout on one request - not real scraper
# breaks: a genuine selector break (raw=0) persists across retries, a network
# blip does not. Only the final result (after retries) is reported.
_HEALTH_RETRY_ATTEMPTS = 3
_HEALTH_RETRY_DELAY = 5.0  # seconds between attempts; module-level so tests zero it


def _format_error(e: Exception) -> str:
    """Human-readable error string. Some exceptions (notably asyncio timeouts)
    stringify to '' - the empty 'ERROR:' lines in the production emails - so we
    always prefix the exception type, keeping the message when there is one."""
    detail = str(e).strip()
    return f"{type(e).__name__}: {detail}" if detail else type(e).__name__


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
    {
        "site": "Vinted",
        "raw_counter": vinted_card_counter,
        # Beyond "did any card render", also flag an unrecognised condition
        # string - a Vinted rename/new tier would silently rot the hardcoded
        # condition filter otherwise (see scrapers/vinted.py). Returns an error
        # message (-> failure email) or None.
        "extra": vinted_unknown_conditions,
        "search_config": SearchConfig(site="Vinted", search_terms=["könyv"], max_price=HUGE_PRICE),
    },
]


async def _check_site_once(monitor: Any, check: Dict[str, Any]) -> Dict[str, Any]:
    """One attempt at one site's check against the live page. Reuses the
    monitor's own session (same headers/timeout as production)."""
    site = check["site"]
    scraper = monitor.scrapers[site]
    search_config = check["search_config"]

    try:
        # Lets a scraper set up session state first (Vinted primes its cookie);
        # no-op for the others.
        await scraper.prepare()
        url = (
            scraper.build_search_url(search_config)
            if hasattr(scraper, "build_search_url")
            else search_config.url
        )
        async with monitor.session.get(url, timeout=30) as response:
            response.raise_for_status()
            html = await response.text(encoding="utf-8")
        raw_count = check["raw_counter"](html)
        extra = check.get("extra")
        if extra:
            extra_error = extra(html)
            if extra_error:
                return {"site": site, "raw": raw_count, "error": extra_error}
    except Exception as e:
        return {"site": site, "error": _format_error(e)}

    try:
        matched = await scraper.fetch_advertisements(search_config)
    except Exception as e:
        return {
            "site": site,
            "raw": raw_count,
            "error": f"fetch_advertisements: {_format_error(e)}",
        }

    return {"site": site, "raw": raw_count, "matched": len(matched)}


async def check_site(monitor: Any, check: Dict[str, Any]) -> Dict[str, Any]:
    """One site's check, retried on failure (transient blip vs. real break).
    Returns the last attempt's result once it succeeds or the retries run out."""
    result = await _check_site_once(monitor, check)
    attempts = 1
    while is_failure(result) and attempts < _HEALTH_RETRY_ATTEMPTS:
        await asyncio.sleep(_HEALTH_RETRY_DELAY)
        result = await _check_site_once(monitor, check)
        attempts += 1
    return result


async def run_health_check(monitor: Any) -> List[Dict[str, Any]]:
    """Check every site. The monitor must already have a session and scrapers
    (i.e. create_session() has been called)."""
    return list(await asyncio.gather(*(check_site(monitor, c) for c in CHECKS)))


def is_failure(result: Dict[str, Any]) -> bool:
    """A real problem: the fetch/parse errored, or the page yielded 0 raw cards
    (selector broken). 0 matches with raw cards present is NOT a failure - it
    just means nothing is for sale right now."""
    return "error" in result or result.get("raw", 0) == 0


def failing_sites(results: List[Dict[str, Any]]) -> Set[str]:
    """The set of site names that failed this run - used by the caller to gate
    alerting on repeated failures (see MultiMarketplaceMonitor)."""
    return {r["site"] for r in results if is_failure(r)}


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


def build_health_alert(results: List[Dict[str, Any]], alert_sites: List[str]) -> tuple[str, str]:
    """Subject + body for the failure email (Hungarian, like every other
    outgoing message - see the language note in README.md).

    `alert_sites` are the sites actually being alerted on - those that failed on
    two consecutive daily checks (each already retried). The full table below
    still shows every site's current status for context."""
    subject = f"Product Monitor: hibás oldal ({len(alert_sites)} db)"

    body_parts = [
        "A napi ellenőrzés két egymást követő napon is hibát talált (minden "
        "kérés többszöri újrapróba után) - az alábbi oldal(ak) scraperje "
        "valószínűleg tényleg nem működik:\n\n",
        ", ".join(alert_sites),
        "\n\n",
        format_report(results),
        "\n\n",
        "Magyarázat:\n",
        "- ERROR: az oldal letöltése vagy feldolgozása hibára futott (a hibaüzenet "
        "elején a kivétel típusa áll).\n",
        "- WARN (0 raw cards): az oldal betöltődött, de egyetlen hirdetéskártyát "
        "sem talált a scraper - ez általában azt jelenti, hogy az oldal "
        "átalakult és a szelektorok elavultak.\n",
        "- A 0 találat (matched) önmagában NEM hiba, ezért erről nem is jön email.\n\n",
        "Mivel ez már két egymást követő ellenőrzésen is elhasalt (nem egy átmeneti "
        "hálózati akadás), érdemes ránézni.\n",
    ]
    return subject, "".join(body_parts)
