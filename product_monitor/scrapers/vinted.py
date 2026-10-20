"""Vinted (vinted.hu) scraper - books only.

Unlike the other four sites, Vinted sits behind DataDome bot protection and a
Next.js App Router frontend, which shaped every design choice here:

- The JSON API (/api/v2/catalog/items) is DataDome-blocked - it returns a 404
  HTML page even from a real browser with valid cookies. So we scrape the
  server-rendered catalog HTML instead, which DataDome does let through once the
  session holds an anonymous cookie (obtained by a plain GET of the homepage -
  see prepare()).
- The listing data is NOT in a clean JSON blob (there is no __NEXT_DATA__, and
  the RSC __next_f stream carries no parseable item objects). Every field we
  need is in the item anchor's `title` attribute, comma-structured:
      "{title}[, Márka: X], Állapot: {condition}[, Méret: Y], {price} Ft, {total} Ft"
  The last two "... Ft" values are the item price and the buyer-protection
  total; the numeric item id is in the href (/items/{id}-slug).
- A catalog page is ~7 MB. On the 350 MB Pi we must NOT build a full
  BeautifulSoup tree of it, so we parse only the <a> tags via SoupStrainer.

Condition handling mirrors Moly's hardcoded 5-star rule (no per-search config
field): only "very good quality" passes. The full condition ladder lives in one
place (CONDITION_LADDER) so a future Vinted rename is a one-line fix, and the
health check flags any unrecognised condition string so we're told to update it
rather than silently letting the filter rot (see health_check.py).
"""

import asyncio
import random
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from bs4 import BeautifulSoup, SoupStrainer

from ..models import Advertisement, SearchConfig
from .base import BaseScraper

BASE_URL = "https://www.vinted.hu"

# Full known condition ladder, best -> worst. Single source of truth: the
# accepted set is derived from it, and the health check compares live condition
# strings against it. If Vinted renames a tier, fix it HERE.
CONDITION_LADDER = (
    "Új, címkékkel",
    "Új, címkék nélkül",
    "Kiváló",
    "Jó",
    "Kielégítő",
)

# "Very good quality" only - the Moly-5★ analog: the two new tiers plus the top
# used tier ("Kiváló" = Vinted's "very good"). "Jó"/"Kielégítő" are rejected.
ACCEPTED_CONDITIONS = frozenset(CONDITION_LADDER[:3])

# Seller-trust hard gate (verified live: feedback_reputation is a continuous 0-1
# float - e.g. 0.92/0.96/0.98 all seen - so 4.5★ is a real, distinguishable
# threshold, not rounded away). A candidate's seller must clear BOTH bars, which
# means fetching the item detail page per candidate (the catalog card carries no
# seller data). New/unproven sellers (0 reviews) are deliberately excluded - the
# owner accepted missing those. Both are one-line constants, easily retuned.
MIN_SELLER_REPUTATION = 0.9  # 4.5★ on the 0-1 scale
MIN_SELLER_FEEDBACK_COUNT = 5
# Safety cap on per-search item-page fetches, so a pathologically broad search
# can't trigger hundreds of 2 MB downloads. Newest-first, so we keep the newest.
_MAX_SELLER_LOOKUPS = 30

# Seller-trust cache: item_id -> (trusted, monotonic timestamp). Avoids re-fetching
# the item page for a listing we already vetted (steady state: a search returns the
# same few listings each cycle, so item fetches drop to ~0). TTL so a borderline
# seller who later crosses 5 reviews gets re-checked. In-memory, process lifetime -
# a restart just costs one bounded, paced re-vet burst.
_SELLER_CACHE_TTL_SECONDS = 7 * 24 * 3600

# Pacing: minimum seconds between two item-page fetches (+ up to _..._JITTER random),
# applied globally across concurrent Vinted searches. The browser isn't rate-limited
# because it's slow and opens few pages; this mimics that. Cycles are 3h apart, so
# latency is free. Instance-overridable (tests set it to 0).
_SELLER_FETCH_MIN_INTERVAL = 1.5
_SELLER_FETCH_JITTER = 1.0

# Parse only <a> tags out of the ~7 MB page - never the whole DOM tree.
_ONLY_ANCHORS = SoupStrainer("a")

_STATUS_MARKER = "Állapot: "
# Integer part of an amount that ends in ".dd"/",dd" right before "Ft"
# (Vinted renders e.g. "8000.00 Ft"); thousands may be space/nbsp separated (\s covers both).
_PRICE_DEC_RE = re.compile(r"(\d[\d\s]*)[.,]\d{2}\s*Ft")
# Fallback for a hypothetical no-decimals format.
_PRICE_INT_RE = re.compile(r"(\d[\d\s]*)\s*Ft")
_BRAND_TAIL_RE = re.compile(r",\s*Márka:.*$")
# Seller feedback from an item detail page. The fields live in the escaped RSC
# JSON (e.g. `\"feedback_reputation\":0.98`); the backslash is optional so a
# plain-JSON form matches too.
_SELLER_REP_RE = re.compile(r'feedback_reputation\\?":\s*([0-9.]+)')
_SELLER_CNT_RE = re.compile(r'feedback_count\\?":\s*([0-9]+)')


def extract_seller_feedback(item_html: str):
    """(reputation 0-1, review count) from an item detail page's HTML, or
    (None, None)/partial when a field is absent."""
    rep = _SELLER_REP_RE.search(item_html)
    cnt = _SELLER_CNT_RE.search(item_html)
    reputation = float(rep.group(1)) if rep else None
    count = int(cnt.group(1)) if cnt else None
    return reputation, count


def seller_is_trusted(reputation, count) -> bool:
    """Hard gate: both bars must be cleared, and both fields must be present (a
    missing field fails safe - we don't trust what we can't read)."""
    return (
        reputation is not None
        and count is not None
        and count >= MIN_SELLER_FEEDBACK_COUNT
        and reputation >= MIN_SELLER_REPUTATION
    )


def extract_condition_token(title_attr: str) -> Optional[str]:
    """The condition value out of an item's `title` attribute.

    Returns the exact CONDITION_LADDER value when it recognises one (matching by
    full string, NOT by comma-splitting - two ladder values contain a comma
    themselves, e.g. "Új, címkék nélkül"). Returns the raw token (up to the next
    comma) when it's something unrecognised, so the health check can report it.
    None if there's no condition marker at all.
    """
    idx = title_attr.find(_STATUS_MARKER)
    if idx == -1:
        return None
    rest = title_attr[idx + len(_STATUS_MARKER) :]
    for cond in CONDITION_LADDER:
        if rest.startswith(cond) and rest[len(cond) : len(cond) + 1] in ("", ","):
            return cond
    return rest.split(",")[0].strip() or None


def _item_anchors(html: str):
    """Every item-card anchor on the page: an <a> that links to /items/ and
    carries the structured `title` attribute (which is what holds the data)."""
    soup = BeautifulSoup(html, "html.parser", parse_only=_ONLY_ANCHORS)
    return soup.find_all(
        "a",
        href=lambda h: h and "/items/" in h,
        title=lambda t: t and _STATUS_MARKER in t,
    )


def count_item_cards(html: str) -> int:
    """Raw card count for the health check - 0 means the page structure changed
    and the extraction is broken."""
    return len(_item_anchors(html))


def unknown_conditions(html: str) -> Optional[str]:
    """Health-check hook: report any live condition string not in
    CONDITION_LADDER. That means Vinted renamed a tier or added a new one - our
    filter would silently rot otherwise, so this turns it into an alert telling
    us to update CONDITION_LADDER / ACCEPTED_CONDITIONS."""
    unknown = set()
    for a in _item_anchors(html):
        token = extract_condition_token(a.get("title", ""))
        if token and token not in CONDITION_LADDER:
            unknown.add(token)
    if unknown:
        listed = ", ".join(repr(u) for u in sorted(unknown))
        return f"ismeretlen Vinted-állapot: {listed} – frissítsd a CONDITION_LADDER-t"
    return None


class VintedScraper(BaseScraper):
    def __init__(self, session):
        super().__init__(session)
        # Guards the one-time (and re-priming) homepage cookie fetch so
        # concurrent searches don't all hammer the homepage at once.
        self._prime_lock = asyncio.Lock()
        # Seller-trust cache (item_id -> (trusted, monotonic ts)).
        self._seller_cache: Dict[str, Tuple[bool, float]] = {}
        # Serialises + paces item-page fetches across concurrent searches.
        self._fetch_lock = asyncio.Lock()
        self._last_item_fetch = 0.0
        # Pacing knobs as instance attrs so tests can zero them out.
        self._fetch_min_interval = _SELLER_FETCH_MIN_INTERVAL
        self._fetch_jitter = _SELLER_FETCH_JITTER

    def build_search_url(self, search_config: SearchConfig) -> str:
        query = quote_plus(" ".join(search_config.search_terms))
        return f"{BASE_URL}/catalog?search_text={query}&order=newest_first"

    @staticmethod
    def _nav_headers(referer: Optional[str] = None) -> Dict[str, str]:
        """Firefox-consistent navigation headers. We send Sec-Fetch-* and a real
        Referer (Firefox does send these), but NOT sec-ch-ua client hints - those
        are Chromium-only, and our User-Agent is Firefox, so sending them would be
        an inconsistency a bot-detector could flag."""
        headers = {
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Sec-Fetch-User": "?1",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _has_anon_cookie(self) -> bool:
        try:
            return any(c.key == "access_token_web" for c in self.session.cookie_jar)
        except Exception:
            return False

    async def prepare(self, force: bool = False) -> None:
        """Obtain the anonymous session cookie DataDome requires. A plain GET of
        the homepage sets it; it then rides on the shared session's cookie jar
        for every catalog request. Re-primes when the cookie is missing/expired
        (force=True, or the jar has none) - so this survives long uptimes."""
        async with self._prime_lock:
            if not force and self._has_anon_cookie():
                return
            try:
                async with self.session.get(
                    BASE_URL, timeout=30, headers=self._nav_headers()
                ) as response:
                    await response.read()
            except Exception as e:
                self.logger.debug(f"Vinted cookie-priming sikertelen: {e}")

    def process_ad_item(self, item, search_config: SearchConfig) -> Optional[Advertisement]:
        """`item` is a single item-card <a> element (bs4 Tag) with href + title."""
        try:
            href = item.get("href")
            if not href or "/items/" not in href:
                return None
            id_match = re.search(r"/items/(\d+)", href)
            if not id_match:
                return None
            ad_id = id_match.group(1)

            title_attr = item.get("title") or ""
            marker_idx = title_attr.find(_STATUS_MARKER)
            if marker_idx == -1:
                return None

            # Condition gate (hardcoded, like Moly's 5 stars). Unknown tokens are
            # not in ACCEPTED_CONDITIONS, so they fail safe (rejected).
            condition = extract_condition_token(title_attr)
            if condition not in ACCEPTED_CONDITIONS:
                return None

            # Book title: everything before the condition marker, minus a
            # trailing brand segment (clothing has ", Márka: X"; books rarely do).
            title = title_attr[:marker_idx].rstrip(", ")
            title = _BRAND_TAIL_RE.sub("", title).strip()
            if not title:
                return None

            if not self.matches_search_terms(title, search_config.search_terms):
                return None
            if self.is_excluded(title, search_config.exclude_terms):
                return None

            # Price = the buyer-protection TOTAL (the user's decision), which is
            # the last "... Ft" value in the title attribute.
            prices = self._prices_from_title(title_attr)
            if not prices:
                return None
            total_price = prices[-1]
            if not self.price_in_range(
                total_price, search_config.max_price, search_config.min_price
            ):
                return None

            url = f"{BASE_URL}{href.split('?')[0]}" if href.startswith("/") else href.split("?")[0]

            return self.create_advertisement(
                ad_id, title, total_price, url, "Vinted", search_config.get_search_id()
            )

        except Exception as e:
            self.logger.debug(f"Hiba Vinted hirdetés feldolgozásakor: {e}")
            return None

    def _prices_from_title(self, title_attr: str) -> List[int]:
        matches = _PRICE_DEC_RE.findall(title_attr) or _PRICE_INT_RE.findall(title_attr)
        prices = [self.parse_price(m) for m in matches]
        return [p for p in prices if p > 0]

    async def fetch_advertisements(self, search_config: SearchConfig) -> List[Advertisement]:
        try:
            await self.prepare()
            url = self.build_search_url(search_config)

            # The catalog GET comes "from" the homepage (same-origin referer).
            html = await self._get_html(url, referer=BASE_URL)
            anchors = _item_anchors(html)

            # No cards at all is likely a stale/missing cookie (a genuine
            # no-results search still returns the site's loose grid) - re-prime
            # once and retry before giving up.
            if not anchors:
                await self.prepare(force=True)
                html = await self._get_html(url, referer=BASE_URL)
                anchors = _item_anchors(html)

            # Cheap filters first (title/terms/price/condition), off the catalog
            # card alone - no extra requests.
            candidates = [
                ad for ad in (self.process_ad_item(a, search_config) for a in anchors) if ad
            ]

            # Then the seller gate, which needs one item-page fetch per candidate
            # (the card has no seller data). Only the few that passed the cheap
            # filters reach here; cap it so a broad search can't fan out. The
            # per-item fetch is cached + paced (see _seller_trusted). The item
            # fetch's referer is this catalog page, mimicking a click from results.
            advertisements = []
            for ad in candidates[:_MAX_SELLER_LOOKUPS]:
                if await self._seller_trusted(ad.url, referer=url):
                    advertisements.append(ad)
                    self.logger.info(f"Vinted: {ad.title} - {f'{ad.price:_}'.replace('_', ' ')} Ft")

            return advertisements

        except Exception as e:
            self.logger.error(f"Hiba Vinted hirdetések lekérésekor: {e}")
            return []

    async def _seller_trusted(self, item_url: str, referer: Optional[str] = None) -> bool:
        """Apply the seller-trust hard gate for one listing. Uses a per-item cache
        (avoids re-fetching a listing we already vetted) and paces the fetch. Any
        fetch/parse problem fails safe (untrusted) and is NOT cached, so a
        transient hiccup never lets an unvetted seller through or sticks."""
        id_match = re.search(r"/items/(\d+)", item_url)
        cache_key = id_match.group(1) if id_match else item_url

        cached = self._seller_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[1]) < _SELLER_CACHE_TTL_SECONDS:
            return cached[0]

        try:
            item_html = await self._paced_get(item_url, referer=referer)
        except Exception as e:
            self.logger.debug(f"Vinted eladó-ellenőrzés sikertelen ({item_url}): {e}")
            return False

        reputation, count = extract_seller_feedback(item_html)
        trusted = seller_is_trusted(reputation, count)
        self._seller_cache[cache_key] = (trusted, time.monotonic())
        self._prune_seller_cache()
        if not trusted:
            self.logger.debug(
                f"Vinted eladó kiszűrve (rep={reputation}, értékelés={count}): {item_url}"
            )
        return trusted

    def _prune_seller_cache(self) -> None:
        """Keep the cache from growing without bound (books stay tiny, but a
        broad search could bloat it). Drops expired entries once it's large."""
        if len(self._seller_cache) <= 1000:
            return
        now = time.monotonic()
        self._seller_cache = {
            k: v for k, v in self._seller_cache.items() if (now - v[1]) < _SELLER_CACHE_TTL_SECONDS
        }

    async def _paced_get(self, url: str, referer: Optional[str] = None) -> str:
        """Item-page fetch, globally serialised and paced so bursts of item-page
        loads (unlike a human's browsing) don't trip Vinted's rate limiting."""
        async with self._fetch_lock:
            wait = self._last_item_fetch + self._fetch_min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, self._fetch_jitter))
            try:
                return await self._get_html(url, referer=referer)
            finally:
                self._last_item_fetch = time.monotonic()

    async def _get_html(self, url: str, referer: Optional[str] = None) -> str:
        async with self.session.get(
            url, timeout=30, headers=self._nav_headers(referer)
        ) as response:
            response.raise_for_status()
            return await response.text(encoding="utf-8")
