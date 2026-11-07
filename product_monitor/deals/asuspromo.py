"""asuspromo.hu watcher - ASUS Hungary's promotion landing page.

The homepage is a small server-rendered page whose div.promo_list holds one
<a class="promo" href="/slug"><img alt="Title"></a> tile per promotion. The
site runs only a few promotions a year (trade-in programmes, bundles, coupons,
raffles - typically lasting weeks), so this runs in the regular 3-hourly cycle
and emails on any change: a new tile (with the first lines of the promo page's
terms), a changed promo page (with what changed), a changed tile title, a tile
that left or came back, and any text appearing on the homepage itself.

Learned from the Wayback Machine history (2020-2026):
- An empty promo_list is a normal state (e.g. since mid-2025), but a missing
  one is not: 3 of 44 archived snapshots were a bot-protection interstitial
  ("One moment, please...") with no promo_list at all. That must read as an
  error, never as "no promotions" (parse_promos raises). Promo pages are held
  to the same standard: a page without the site's #footer is not a real
  render, so it is skipped rather than reported as a change.
- Promo pages can carry a live stock counter ("Fennmaradó csomagok száma: 252",
  falling almost daily in 2023's bundle promo). It doesn't count as a change
  by itself (it would alert every cycle), but its current value is in every
  email. A change in numbers only (an unknown counter, but also e.g. a date
  moved within the same month) alerts at most once a day; text changes alert
  at once.
- Tiles often stay up long after the promotion ended, so the email carries the
  dates from the promo page itself rather than implying the offer is live.
"""

import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup, Tag

from .state import DealState

BASE_URL = "https://asuspromo.hu/"
TERMS_EXCERPT_CHARS = 700
DIFF_MAX_LINES = 20
DIFF_LINE_CHARS = 200
# A change in numbers only is alerted at most this often per page
NUMBERS_ONLY_ALERT_INTERVAL = timedelta(days=1)

_BLOCK_TAGS = (
    "address article br dd div dt footer h1 h2 h3 h4 h5 h6 header hr li ol p section "
    "table td th tr ul"
).split()
_STOCK_COUNTER_RE = re.compile(r"^(fennmaradó|hátralévő)\b.*\d", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")


class PageStructureError(Exception):
    """The page loaded but isn't the promo page we know (bot interstitial or redesign)."""


@dataclass
class Promo:
    id: str
    title: str
    url: str


@dataclass
class PromoChange:
    """One line item of an email. kind: new / changed / returned / removed / home."""

    kind: str
    title: str
    url: str
    detail: str


_KIND_LABELS = {
    "new": "új akció",
    "changed": "megváltozott",
    "returned": "visszakerült a főoldalra",
    "removed": "lekerült a főoldalról",
    "home": "megváltozott",
}


def parse_promos(html: str) -> List[Promo]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", class_="promo_list")
    if not isinstance(container, Tag):
        raise PageStructureError(
            "nincs promo_list az oldalon (botvédelmi oldal vagy átalakult oldal)"
        )
    promos = []
    for anchor in container.find_all("a", class_="promo"):
        href = anchor.get("href")
        if not href:
            continue
        # Slugs can contain raw spaces ("/Gamer raffle") - quote them so the
        # link stays clickable in the email; already-escaped ones stay as-is
        url = urljoin(BASE_URL, quote(href, safe="/:?=&#%"))
        promo_id = unquote(urlparse(url).path).strip("/") or url
        image = anchor.find("img")
        title = (image.get("alt") if image else None) or anchor.get_text(" ", strip=True)
        promos.append(Promo(id=promo_id, title=title or promo_id, url=url))
    return promos


def page_text(html: str, drop: Tuple[str, ...] = (), require_footer: bool = True) -> str:
    """The page's visible text, one block per line, without scripts, forms,
    the privacy-policy modal, the footer and any `drop` CSS selectors.
    Raises PageStructureError if the page has no #footer (not a real render)."""
    soup = BeautifulSoup(html, "html.parser")
    if require_footer and soup.select_one("#footer") is None:
        raise PageStructureError("nincs #footer az oldalon (botvédelmi oldal vagy átalakult oldal)")
    for tag in soup(["script", "style", "form", "select", "noscript"]):
        tag.decompose()
    for selector in ("#footer", ".modal", *drop):
        for tag in soup.select(selector):
            tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_after("\n")
    lines = (" ".join(line.split()) for line in soup.get_text().splitlines())
    return "\n".join(line for line in lines if line)


def excerpt(text: str, limit: int = TERMS_EXCERPT_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def extract_terms(html: str, limit: int = TERMS_EXCERPT_CHARS) -> str:
    """The promo page's opening text (title, summary, dates, conditions)."""
    return excerpt(page_text(html), limit)


def _content_lines(text: str) -> List[str]:
    """The lines a change is judged on: everything but the stock counter."""
    return [line for line in text.splitlines() if not _STOCK_COUNTER_RE.match(line)]


def stock_counter(text: str) -> Optional[str]:
    return next((line for line in text.splitlines() if _STOCK_COUNTER_RE.match(line)), None)


def numbers_only(old: str, new: str) -> bool:
    """True if the two texts differ only in their numbers."""
    return [_DIGITS_RE.sub("#", line) for line in _content_lines(old)] == [
        _DIGITS_RE.sub("#", line) for line in _content_lines(new)
    ]


def text_diff(old: str, new: str) -> str:
    """Changed lines, "- " removed / "+ " added, capped for the email."""
    lines = [
        f"{line[0]} {line[1:]}"[:DIFF_LINE_CHARS]
        for line in difflib.unified_diff(_content_lines(old), _content_lines(new), lineterm="", n=0)
        if not line.startswith(("---", "+++", "@@"))
    ]
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"… (még {len(lines) - DIFF_MAX_LINES} sor)"]
    return "\n".join(lines)


def build_promo_email(items: List[PromoChange]) -> Tuple[str, str]:
    all_new = all(item.kind == "new" for item in items)
    if len(items) == 1:
        item = items[0]
        label = "" if item.kind == "new" else f" – {_KIND_LABELS[item.kind]}"
        subject = f"ASUS promóció: {item.title}{label}"
    elif all_new:
        subject = f"ASUS promóciók: {len(items)} új az asuspromo.hu-n"
    else:
        subject = f"ASUS promóciók: {len(items)} változás az asuspromo.hu-n"
    intro = "Új akció az asuspromo.hu-n.\n\n" if all_new else "Változás az asuspromo.hu-n.\n\n"
    parts = [intro, "-" * 72, "\n\n"]
    for i, item in enumerate(items, 1):
        label = "" if item.kind == "new" else f" ({_KIND_LABELS[item.kind]})"
        detail = "\n".join(f"   {line}" for line in item.detail.splitlines())
        parts += [f"{i}. {item.title}{label}\n", f"   {item.url}\n\n", f"{detail}\n\n"]
    if any(item.kind in ("changed", "home") for item in items):
        parts.append('A változások: "-" = eltűnt sor, "+" = új sor.\n')
    parts.append(
        "Megjegyzés: a főoldali csempék gyakran a lejárat után is fent maradnak - "
        "a pontos időszakot lásd a feltételekben.\n"
    )
    return subject, "".join(parts)


async def _get_html(session: Any, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
        response.raise_for_status()
        return await response.text(encoding="utf-8")


async def health_probe(session: Any) -> Dict[str, Any]:
    """raw = 1 when the promo_list container is there (structure OK), matched =
    number of promotions on it. 0 promotions is fine; no container is not. With
    promotions up, the first one's page must be readable too - otherwise a
    redesign would silently stop the change alerts."""
    try:
        promos = parse_promos(await _get_html(session, BASE_URL))
        if promos:
            page_text(await _get_html(session, promos[0].url))
    except PageStructureError as e:
        return {"site": "AsusPromo", "raw": 0, "error": str(e)}
    return {"site": "AsusPromo", "raw": 1, "matched": len(promos)}


class AsusPromoWatcher:
    NAME = "asuspromo"

    def __init__(self, session: Any, state: DealState, send: Callable[[str, str], None]):
        self.session = session
        self.state = state
        self.send = send
        self.section = state.section(self.NAME)
        # promo id -> title, url, first_seen, text (last page text), removed,
        # numbers_alerted (last numbers-only change alert)
        self.seen: Dict[str, Dict[str, Any]] = self.section.setdefault("seen", {})
        self.clock: Callable[[], datetime] = datetime.now
        self.logger = logging.getLogger(__name__)

    async def check(self) -> None:
        try:
            home = await _get_html(self.session, BASE_URL)
            promos = parse_promos(home)
            # promo_list already proved this is a real render
            home_text = page_text(home, drop=(".promo_list",), require_footer=False)
        except PageStructureError as e:
            # Transient interstitials happen; the daily health check alerts if it persists
            self.logger.warning(f"AsusPromo: {e}")
            return

        items: List[PromoChange] = []
        previous_home = self.section.get("home_text")
        if previous_home is not None and _content_lines(home_text) != _content_lines(previous_home):
            items.append(
                PromoChange(
                    "home", "asuspromo.hu főoldal", BASE_URL, text_diff(previous_home, home_text)
                )
            )
        self.section["home_text"] = home_text

        for promo in promos:
            item = await self._check_promo(promo)
            if item:
                items.append(item)

        live = {promo.id for promo in promos}
        for promo_id, record in self.seen.items():
            if promo_id not in live and not record.get("removed"):
                record["removed"] = True
                items.append(
                    PromoChange(
                        "removed",
                        record["title"],
                        record["url"],
                        "Az akció valószínűleg véget ért.",
                    )
                )

        if items:
            self.send(*build_promo_email(items))
            self.logger.info(f"AsusPromo: email {len(items)} változásról.")
        self.state.save()

    async def _check_promo(self, promo: Promo) -> Optional[PromoChange]:
        try:
            text: Optional[str] = page_text(await _get_html(self.session, promo.url))
            error = None
        except Exception as e:
            text, error = None, f"{type(e).__name__}: {e}"

        record = self.seen.get(promo.id)
        if record is None:
            self.seen[promo.id] = {
                "title": promo.title,
                "url": promo.url,
                "first_seen": self.clock().isoformat(),
                "text": text,
            }
            detail = excerpt(text) if text else f"(a feltételek nem tölthetők le: {error})"
            return PromoChange("new", promo.title, promo.url, detail)

        if text is None:
            # A failed fetch is not a change; keep the old text and retry next cycle
            self.logger.warning(f"AsusPromo: {promo.url} nem tölthető le: {error}")
        notes = []
        returned = record.pop("removed", False)
        if promo.title != record["title"]:
            notes.append(f"A csempe címe: {record['title']} → {promo.title}")
            record["title"] = promo.title
        record["url"] = promo.url

        old = record.get("text")
        if text is not None and old is not None and _content_lines(text) != _content_lines(old):
            last = record.get("numbers_alerted")
            if (
                numbers_only(old, text)
                and last
                and self.clock() - datetime.fromisoformat(last) < NUMBERS_ONLY_ALERT_INTERVAL
            ):
                # Too soon after the last numbers-only alert: keep the old text,
                # so the change is still there (cumulated) on a later cycle
                text = old
            else:
                notes.append(text_diff(old, text))
                if numbers_only(old, text):
                    record["numbers_alerted"] = self.clock().isoformat()
        if text is not None:
            record["text"] = text

        if returned:
            current = record.get("text")
            notes.append(excerpt(current) if current else "")
            kind = "returned"
        elif notes:
            kind = "changed"
        else:
            return None
        counter = stock_counter(record.get("text") or "")
        if counter:
            notes.append(f"Jelenleg: {counter}")
        return PromoChange(kind, promo.title, promo.url, "\n".join(n for n in notes if n))
