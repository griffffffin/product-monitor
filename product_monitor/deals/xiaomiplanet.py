"""XiaomiPlanet (xiaomiplanet.sk) deal watcher - the editors' price boxes.

What it watches: the "Ponuka XiaomiPlanet" price boxes (div.cp-pbox) the
editors embed in their articles - one box per product, one panel per variant
(storage / warehouse / shop), each carrying the coupon-applied price, the
pre-coupon price, the coupon code, the shop, an "Aktualizované" timestamp and
the price history. Coupon stock can sell out within ~15 minutes of being
published, which is why this runs on its own fast loop instead of the 3-hourly
marketplace cycle (see MultiMarketplaceMonitor._xiaomiplanet_loop).

Facts that shaped the design (verified against the live site, 2026-10-21):
- A box update does NOT bump the article's `modified` date (the box data lives
  outside the post), so the article list alone misses a coupon refresh: the
  articles carrying the tracked boxes have to be re-read.
- The WP REST API returns freshly rendered content (no ETag / page cache),
  ~20 KB gzip per article; the latest-articles list is ~1.4 KB.
- Deal pushes often re-publish an existing article (same id, new date_gmt),
  which the cheap latest-articles check catches within a minute.
- Not faster: the Telegram channel (t.me/planetky) cross-posts ~3 minutes
  after the site; the Deal Planet catalogue (dealplanet.eu) mirrors the same
  prices, checks coupons daily and disallows /api/ in robots.txt.

Request budget, per the loop in monitor.py: the latest-articles list every
article_check_seconds; each "carrier" (the smallest article containing a
tracked box) every box_check_seconds - ~2.5k requests a day for the 7
boxes tracked in Oct 2026; every minute would be ~10k full renders a day on a
small blog. Every box gets the same interval, however long it has been
unchanged: boxes do get refreshed without their article being re-published
(one box was updated in Oct 2026 inside a Sept 2025 article), so a
"stale" box is exactly where a surprise price would otherwise be missed.

Alerting: every deal under max_price_eur is emailed, not only new lows - a
dearer deal that follows a cheaper, missed one matters just as much. An offer
(box variant) under the limit is emailed when first seen, when it comes back
under the limit, when its price moves at least MIN_CHANGE_FRACTION either way
from its last alert, when its coupon code changes, and when the site posts it
again (a new or re-dated article carrying the box) or the editors re-save its
box (a later "Aktualizované" stamp), even at an unchanged price and code: a
restock of the same coupon looks exactly like a mere re-check. Box stamps move
by hand and rarely (every 5-10 days for an actively maintained box, months for
others in Oct 2026), so this costs few emails. Not alerted: price moves under
MIN_CHANGE_FRACTION with no new stamp. The very first run sends one summary of
everything currently under the limit instead.
"""

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from bs4 import BeautifulSoup, SoupStrainer

from ..models import XiaomiPlanetConfig
from .state import DealState

BASE_URL = "https://xiaomiplanet.sk"
POSTS_API = f"{BASE_URL}/wp-json/wp/v2/posts"
# "Planéta zliav" - the deals category; its articles nearly always carry a
# price box, which makes it the health check's sample.
DEALS_CATEGORY_ID = 139

# Offers whose price moved this much (either way) since their last alert are
# alerted again; smaller moves are exchange-rate drift.
MIN_CHANGE_FRACTION = 0.02
# Full re-discovery of the carrier articles (titles via search, then content).
DISCOVERY_INTERVAL_SECONDS = 24 * 3600
# Offers not seen for this long (variant removed from its box) are forgotten.
OFFER_RETENTION_DAYS = 30
# Pause between article fetches within one tick; tests set the attribute to 0.
FETCH_GAP_SECONDS = 1.0
# How many latest-article stamps to remember.
MAX_REMEMBERED_POSTS = 200

_LIST_FIELDS = "id,date_gmt,modified_gmt,link,title"
_EUR_RE = re.compile(r"(\d[\d\s .,]*\d|\d)\s*€")
_UPDATED_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*o\s*(\d{1,2}):(\d{2})")
_GLOBAL_SUFFIX_RE = re.compile(r"\s+Glob\w*\s+Verzia\s*$", re.IGNORECASE)


def latest_url() -> str:
    return f"{POSTS_API}?per_page=10&_fields={_LIST_FIELDS}"


def discovery_url(search_terms: List[str]) -> str:
    query = quote(" ".join(t.strip() for t in search_terms if t.strip()))
    return f"{POSTS_API}?search={query}&per_page=100&orderby=date&_fields={_LIST_FIELDS}"


def article_url(post_id: int) -> str:
    return f"{POSTS_API}/{post_id}?_fields=id,link,content"


def health_url() -> str:
    return f"{POSTS_API}?categories={DEALS_CATEGORY_ID}&per_page=10&_fields=id,link,content"


@dataclass
class Offer:
    """One variant panel of one price box."""

    box_id: str
    variant_id: str
    product: str
    variant: str
    price: float
    old_price: Optional[float] = None
    card_price: Optional[float] = None
    coupon: Optional[str] = None
    shop: Optional[str] = None
    warehouse: Optional[str] = None
    updated: Optional[str] = None  # ISO, site-local time (CET/CEST, same as Hungary)
    history_min: Optional[float] = None
    buy_url: Optional[str] = None
    article_url: str = ""

    @property
    def key(self) -> str:
        return f"{self.box_id}:{self.variant_id}"


def parse_eur(text: Optional[str]) -> Optional[float]:
    """First euro amount in the text: "579,90 €", "1 049,00 €" (space/nbsp
    thousands), and the dot-decimal "579.90 €" just in case."""
    m = _EUR_RE.search(text or "")
    if not m:
        return None
    raw = re.sub(r"[\s ]", "", m.group(1))
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif not (raw.count(".") == 1 and len(raw.split(".")[1]) == 2):
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_updated(text: Optional[str]) -> Optional[str]:
    """ "Aktualizované 21. 10. 2026 o 14:05" -> "2026-10-21T14:05:00" """
    m = _UPDATED_RE.search(text or "")
    if not m:
        return None
    day, month, year, hour, minute = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute).isoformat()
    except ValueError:
        return None


def _text(element) -> Optional[str]:
    if element is None:
        return None
    text = element.get_text(" ", strip=True)
    return text or None


def _history_min(panel) -> Optional[float]:
    script = panel.find("script", attrs={"data-ppb-history-json": True})
    if script is None or not script.string:
        return None
    try:
        points = json.loads(script.string)
        values = [float(p[1]) for p in points]
    except (ValueError, TypeError, IndexError):
        return None
    return min(values) if values else None


def parse_price_boxes(html: str, page_url: str = "") -> List[Offer]:
    """Every variant offer of every price box in an article's HTML. Only the
    box subtrees are parsed (SoupStrainer) - a long article is ~100 KB."""
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer("div", class_="cp-pbox"))
    offers = []
    for box in soup.find_all("div", class_="cp-pbox"):
        box_id = box.get("data-ppb-box-id")
        product = _text(box.find(class_="cp-pbox__title"))
        if not box_id or not product:
            continue

        # Variant tabs (absent when the box has a single offer)
        labels: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        for button in box.find_all("button", class_="cp-pbox__variant"):
            labels[button.get("data-ppb-var")] = (
                _text(button.find(class_="cp-disp")),
                _text(button.find(class_="cp-pbox__variant-ship-txt")),
            )
        single_label = _text(box.find(class_="cp-pbox__title-variant"))

        for panel in box.find_all("div", class_="cp-pbox__panel"):
            variant_id = panel.get("data-ppb-var")
            price_element = panel.find(class_="cp-pbox__price")
            if not variant_id or price_element is None:
                continue
            # Gshopper adds a card-payment surcharge line inside the price element
            card_fee = price_element.find(class_="cp-pbox__cardfee")
            card_price = parse_eur(_text(card_fee)) if card_fee else None
            if card_fee:
                card_fee.extract()
            price = parse_eur(_text(price_element))
            if price is None:
                continue

            label, warehouse = labels.get(variant_id, (None, None))
            if warehouse is None:
                shop_tag = panel.find(class_="cp-pbox__shop-tag")
                warehouse = _text(shop_tag)
            code = panel.find(class_="cp-pbox__code")
            cta = panel.find("a", class_="cp-pbox__cta") or panel.find(
                "a", attrs={"data-ppb-out": True}
            )
            offers.append(
                Offer(
                    box_id=str(box_id),
                    variant_id=str(variant_id),
                    product=product,
                    variant=label or single_label or "",
                    price=price,
                    old_price=parse_eur(_text(panel.find(class_="cp-pbox__old"))),
                    card_price=card_price,
                    coupon=(code.get("data-ppb-copy") if code else None) or None,
                    shop=panel.get("data-ppb-shop") or None,
                    warehouse=warehouse,
                    updated=parse_updated(_text(panel.find(class_="cp-pbox__updated"))),
                    history_min=_history_min(panel),
                    buy_url=cta.get("href") if cta else None,
                    article_url=page_url,
                )
            )
    return offers


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower()


def title_matches(title: str, search_terms: List[str], exclude_terms: List[str]) -> bool:
    """All search terms as substrings (like the marketplace searches), and no
    exclude term as a whole word - so "13" excludes "Redmi Note 13" but not "Redmi Note 130"."""
    normalized = _normalize(title)
    if not all(_normalize(t.strip()) in normalized for t in search_terms if t.strip()):
        return False
    for term in exclude_terms:
        word = _normalize(term.strip())
        if word and re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", normalized):
            return False
    return True


def _box_refreshed(previous: Dict[str, Any], offer: Offer) -> bool:
    """The editors re-saved the offer since it was last seen (a later
    "Aktualizované" stamp)."""
    before = previous.get("updated")
    return bool(before and offer.updated and offer.updated > before)


def fmt_eur(value: float) -> str:
    """579.9 -> "579,90 €", 1049 -> "1 049,00 €" """
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " €"


def _fmt_limit(value: float) -> str:
    return f"{int(value)} €" if float(value).is_integer() else fmt_eur(value)


def short_product(product: str) -> str:
    """ "REDMI Note 17 Pro Globálna Verzia" -> "REDMI Note 17 Pro" """
    return _GLOBAL_SUFFIX_RE.sub("", product)


def _fmt_updated(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime("%Y.%m.%d. %H:%M")
    except ValueError:
        return iso


def build_offer_email(
    items: List[Tuple[Dict[str, Any], str]],
    max_price: float,
    highlight: Optional[float],
    summary: bool = False,
) -> Tuple[str, str]:
    """Subject + body (Hungarian). `items` are (offer record, reason) pairs; an
    offer record is asdict(Offer) plus the watcher's bookkeeping fields."""
    items = sorted(items, key=lambda item: item[0]["price"])
    cheapest = items[0][0]
    fire = "🔥 " if highlight is not None and cheapest["price"] <= highlight else ""
    limit = _fmt_limit(max_price)

    if summary:
        subject = f"XiaomiPlanet: {len(items)} db"
        intro = (
            f"Első futás: a jelenleg kint lévő, legfeljebb {limit} árú ajánlatok.\n"
            "Figyeld a 'Doboz frissítve' dátumot - a régóta nem frissített doboz "
            "kuponja már lejárhatott. Innentől csak a változásokról jön levél.\n"
        )
    else:
        if len(items) == 1:
            subject = (
                f"{fire}{short_product(cheapest['product'])} – {fmt_eur(cheapest['price'])}"
                + (f" ({cheapest['shop']})" if cheapest.get("shop") else "")
            )
        else:
            subject = (
                f"{fire}XiaomiPlanet: {len(items)} ajánlat (legolcsóbb: "
                f"{short_product(cheapest['product'])} – {fmt_eur(cheapest['price'])})"
            )
        intro = (
            f"Friss XiaomiPlanet ajánlat a figyelt modellekre (legfeljebb {limit}).\n"
            "A kuponos készlet gyorsan elfogyhat.\n"
        )

    parts = [intro, "\n", "-" * 72, "\n\n"]
    for i, (offer, reason) in enumerate(items, 1):
        mark = "🔥 " if highlight is not None and offer["price"] <= highlight else ""
        title = short_product(offer["product"])
        if offer.get("variant"):
            title += f" – {offer['variant']}"
        price_line = f"   Ár: {fmt_eur(offer['price'])}"
        if offer.get("old_price") and offer["old_price"] > offer["price"]:
            price_line += f"  (kupon nélkül: {fmt_eur(offer['old_price'])})"
        parts += [f"{i}. {mark}{title}\n", price_line, "\n"]
        if offer.get("card_price"):
            parts.append(f"   Kártyás fizetéssel: {fmt_eur(offer['card_price'])}\n")
        details = [
            f"Kupon: {offer['coupon']}" if offer.get("coupon") else "Kupon: nincs",
            f"Bolt: {offer['shop']}" if offer.get("shop") else None,
            f"Raktár: {offer['warehouse']}" if offer.get("warehouse") else None,
        ]
        parts.append("   " + "  |  ".join(d for d in details if d) + "\n")
        if reason:
            parts.append(f"   Miért most: {reason}\n")
        if offer.get("history_min") is not None:
            low = f"   Eddigi legalacsonyabb ár: {fmt_eur(offer['history_min'])}"
            if offer["price"] <= offer["history_min"] + 0.005:
                low += " - most ennyi, ez a mélypont!"
            parts += [low, "\n"]
        updated = _fmt_updated(offer.get("updated"))
        if updated:
            parts.append(f"   Doboz frissítve: {updated}\n")
        if offer.get("buy_url"):
            parts.append(f"   Vásárlás: {offer['buy_url']}\n")
        if offer.get("article_url"):
            parts.append(f"   Cikk: {offer['article_url']}\n")
        parts.append("\n")
    return subject, "".join(parts)


async def _get_json(session: Any, url: str) -> Any:
    async with session.get(url, timeout=30, headers={"Accept": "application/json"}) as response:
        response.raise_for_status()
        return json.loads(await response.text(encoding="utf-8"))


async def health_probe(session: Any) -> Dict[str, Any]:
    """Daily health check: the latest deals-category articles must still yield
    price boxes (raw = boxes) with readable prices (matched = offers). One
    request; config-independent, so it also works from the smoke-check script."""
    posts = await _get_json(session, health_url())
    if not isinstance(posts, list) or not posts:
        return {"site": "XiaomiPlanet", "error": "üres cikklista a REST API-ból"}
    box_ids = set()
    offer_count = 0
    for post in posts:
        html = post["content"]["rendered"]
        # Boxes counted independently of the offer parser, so "boxes present
        # but no readable price" is told apart from "no boxes at all"
        box_ids.update(re.findall(r'data-ppb-box-id="(\d+)"', html))
        offer_count += len(parse_price_boxes(html, post.get("link", "")))
    if box_ids and not offer_count:
        return {
            "site": "XiaomiPlanet",
            "raw": len(box_ids),
            "error": "vannak árdobozok, de egyetlen ár sem olvasható ki belőlük",
        }
    return {"site": "XiaomiPlanet", "raw": len(box_ids), "matched": offer_count}


class XiaomiPlanetWatcher:
    """Call tick() every article_check_seconds. Emails go through `send`
    (subject, body); state lives in the "xiaomiplanet" section of DealState."""

    NAME = "xiaomiplanet"

    def __init__(
        self,
        session: Any,
        config: XiaomiPlanetConfig,
        state: DealState,
        send: Callable[[str, str], None],
    ):
        self.session = session
        self.config = config
        self.state = state
        self.send = send
        self.section = state.section(self.NAME)
        self.offers: Dict[str, Dict[str, Any]] = self.section.setdefault("offers", {})
        self.posts: Dict[str, str] = self.section.setdefault("posts", {})
        # box_id -> (post_id, content length): the smallest article carrying it
        self.carriers: Dict[str, Tuple[int, int]] = {}
        # post_id -> clock() time its next re-read is due
        self.next_check: Dict[int, float] = {}
        self.last_discovery: Optional[float] = None
        self.fetch_gap = FETCH_GAP_SECONDS
        self.clock: Callable[[], float] = time.time
        self.logger = logging.getLogger(__name__)
        # offer key -> (record, reasons): one entry per offer even if several
        # articles carrying its box were read in the same tick
        self._pending: Dict[str, Tuple[Dict[str, Any], List[str]]] = {}
        self._dirty = False

    @property
    def initialized(self) -> bool:
        return bool(self.section.get("initialized"))

    def matches(self, product: str) -> bool:
        return title_matches(product, self.config.search_terms, self.config.exclude_terms)

    def _above_floor(self, price: float) -> bool:
        floor = self.config.min_price_eur
        return floor is None or price >= floor

    async def tick(self) -> None:
        now = self.clock()
        if self.last_discovery is None or now - self.last_discovery >= DISCOVERY_INTERVAL_SECONDS:
            await self.discover()
            self.last_discovery = now
        await self.check_latest()
        await self.refresh_carriers()
        self._finish_tick()

    async def discover(self) -> None:
        """Find the articles carrying the tracked boxes: search the article
        titles, read every matching one, keep the smallest carrier per box."""
        posts = await _get_json(self.session, discovery_url(self.config.search_terms))
        matching = [
            p
            for p in posts
            if title_matches(unescape(p["title"]["rendered"]), self.config.search_terms, [])
        ]
        for post in matching:
            await self._read_article(int(post["id"]))
        self.logger.info(
            f"XiaomiPlanet: {len(self.carriers)} figyelt árdoboz "
            f"{len({p for p, _ in self.carriers.values()})} cikkben "
            f"({len(matching)} cikk átnézve)."
        )

    async def check_latest(self) -> None:
        """Re-read any article that is new, re-published (new date_gmt) or
        edited. New or re-published means the site is posting its deals again:
        their offers under the limit are alerted even if unchanged."""
        posts = await _get_json(self.session, latest_url())
        # (Re-)published = dated after every post seen so far. An edit keeps its
        # date, and an unseen but older post only slid up the list because a
        # newer one was deleted or unpublished.
        newest_known = max((s.split("|")[0] for s in self.posts.values()), default="")
        for post in posts:
            post_id = str(post["id"])
            date = str(post.get("date_gmt"))
            stamp = f"{date}|{post.get('modified_gmt')}"
            if self.posts.get(post_id) == stamp:
                continue
            reposted = date > newest_known
            tracked = await self._read_article(int(post["id"]), reposted=reposted)
            # Stored only after a successful read, so a failed one is retried
            # (and its re-post still alerted) on the next tick. Re-inserted at
            # the end so the eviction below drops the longest-unchanged posts.
            self.posts.pop(post_id, None)
            self.posts[post_id] = stamp
            self._dirty = True
            if tracked:
                self.logger.info(
                    f"XiaomiPlanet: {'új/újra kiírt' if reposted else 'szerkesztett'} cikk "
                    f"{tracked} figyelt ajánlattal: {post.get('link', post_id)}"
                )
        while len(self.posts) > MAX_REMEMBERED_POSTS:
            self.posts.pop(next(iter(self.posts)))

    async def refresh_carriers(self) -> None:
        now = self.clock()
        for post_id in sorted({p for p, _ in self.carriers.values()}):
            if self.next_check.get(post_id, 0.0) <= now:
                await self._read_article(post_id)

    async def _read_article(self, post_id: int, reposted: bool = False) -> int:
        """Fetch one article, update carriers, ingest its offers. Returns the
        number of tracked offers it carries."""
        data = await _get_json(self.session, article_url(post_id))
        html = data["content"]["rendered"]
        offers = [
            o for o in parse_price_boxes(html, data.get("link", "")) if self.matches(o.product)
        ]

        present = {o.box_id for o in offers}
        for box_id, (carrier, _) in list(self.carriers.items()):
            if carrier == post_id and box_id not in present:
                del self.carriers[box_id]  # the editors removed it from this article
        for box_id in present:
            current = self.carriers.get(box_id)
            if current is None or current[0] == post_id or len(html) < current[1]:
                self.carriers[box_id] = (post_id, len(html))

        self.next_check[post_id] = self.clock() + self.config.box_check_seconds
        self._ingest(offers, reposted)
        if self.fetch_gap:
            await asyncio.sleep(self.fetch_gap)
        return len(offers)

    def _ingest(self, offers: List[Offer], reposted: bool = False) -> None:
        now_iso = datetime.fromtimestamp(self.clock()).isoformat()
        max_price = self.config.max_price_eur
        for offer in offers:
            if not self._above_floor(offer.price):
                continue  # an accessory variant, not the product itself
            previous = self.offers.get(offer.key)
            # Price and coupon as of the last alert (None = not alerted since
            # it was last over the limit)
            alert_price = previous.get("alert_price") if previous else None
            alert_coupon = previous.get("alert_coupon") if previous else None
            reasons = []
            if offer.price <= max_price:
                if previous is None:
                    reasons.append("új ajánlat")
                elif alert_price is None:
                    reasons.append(
                        f"a limit alá került ({fmt_eur(previous['price'])} → "
                        f"{fmt_eur(offer.price)})"
                    )
                else:
                    change = f"({fmt_eur(alert_price)} → {fmt_eur(offer.price)})"
                    if offer.price <= alert_price * (1 - MIN_CHANGE_FRACTION):
                        reasons.append(f"árcsökkenés {change}")
                    elif offer.price >= alert_price * (1 + MIN_CHANGE_FRACTION):
                        reasons.append(f"drágult, de még a limit alatt van {change}")
                    if offer.coupon != alert_coupon:
                        reasons.append(
                            f"kuponkód változott ({alert_coupon or 'nincs'} → "
                            f"{offer.coupon or 'nincs'})"
                        )
                    if not reasons and _box_refreshed(previous, offer):
                        # Same code restocked at the same price looks exactly
                        # like this - it can't be told apart from a re-check
                        if offer.price != previous["price"]:
                            change = f"({fmt_eur(previous['price'])} → {fmt_eur(offer.price)})"
                        else:
                            change = "(ár és kupon változatlan)"
                        reasons.append(f"frissítették az ajánlatot {change}")
                if previous is not None and reposted:
                    reasons.append("újra kiírták (friss cikk az oldalon)")
                if reasons:
                    alert_price, alert_coupon = offer.price, offer.coupon
            else:
                # Back above the limit: re-arm, so the next dip alerts again
                alert_price = alert_coupon = None

            record = asdict(offer)
            record.update(alert_price=alert_price, alert_coupon=alert_coupon, last_seen=now_iso)
            if previous is None or {k: v for k, v in previous.items() if k != "last_seen"} != {
                k: v for k, v in record.items() if k != "last_seen"
            }:
                self._dirty = True
            self.offers[offer.key] = record
            if reasons and self.initialized:
                earlier = self._pending.get(offer.key)
                if earlier:
                    reasons = earlier[1] + [r for r in reasons if r not in earlier[1]]
                self._pending[offer.key] = (dict(record), reasons)

    def _qualifying_records(self) -> List[Dict[str, Any]]:
        return [
            r
            for r in self.offers.values()
            if r["price"] <= self.config.max_price_eur
            and self._above_floor(r["price"])
            and self.matches(r["product"])
        ]

    def _finish_tick(self) -> None:
        if not self.initialized:
            # First run: one summary of everything currently under the limit
            # (built from the state, so a partly failed earlier tick's offers
            # are included too), then only changes.
            records = self._qualifying_records()
            if records:
                self.send(
                    *build_offer_email(
                        [(r, "") for r in records],
                        self.config.max_price_eur,
                        self.config.highlight_price_eur,
                        summary=True,
                    )
                )
                self.logger.info(f"XiaomiPlanet: első összesítő elküldve ({len(records)} ajánlat).")
            else:
                self.logger.info("XiaomiPlanet: első futás, jelenleg nincs a limit alatti ajánlat.")
            self.section["initialized"] = True
            self._dirty = True
        elif self._pending:
            items = [(record, "; ".join(reasons)) for record, reasons in self._pending.values()]
            self.send(
                *build_offer_email(
                    items, self.config.max_price_eur, self.config.highlight_price_eur
                )
            )
            self.logger.info(f"XiaomiPlanet: email {len(items)} ajánlatról.")
        self._pending = {}

        cutoff = (
            datetime.fromtimestamp(self.clock()) - timedelta(days=OFFER_RETENTION_DAYS)
        ).isoformat()
        for key in [k for k, r in self.offers.items() if r.get("last_seen", "") < cutoff]:
            del self.offers[key]
            self._dirty = True

        if self._dirty:
            self.state.save()
            self._dirty = False
