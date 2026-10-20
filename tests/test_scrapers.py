"""Card-parsing tests for each site's process_ad_item(), against small
hand-built HTML fragments that mirror the real selectors. Network-free -
process_ad_item() takes an already-parsed BeautifulSoup element, it never
touches self.session.
"""

import asyncio
import base64
import json
import time
import types

from bs4 import BeautifulSoup

import product_monitor as pm
from product_monitor.scrapers import vinted


def soup_fragment(html: str):
    return BeautifulSoup(html, "html.parser").find(True)


class _FakeResponse:
    def __init__(self, text):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def text(self, encoding=None):
        return self._text

    async def read(self):
        return b""


def _jwt(exp):
    """An unsigned JWT-shaped token carrying just an `exp` claim."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


def _cookie(key, value="x"):
    return types.SimpleNamespace(key=key, value=value)


class _FakeJar:
    def __init__(self, cookies):
        self.cookies = list(cookies)
        self.cleared = 0

    def __iter__(self):
        return iter(self.cookies)

    def clear_domain(self, domain):
        self.cookies = []
        self.cleared += 1


class _FakeSession:
    """Minimal aiohttp-session stand-in for network-free async tests: serves
    canned pages by URL and records every GET. Injected via the constructor -
    no monkeypatching (the suite deliberately avoids it).

    A page may be a callable taking the session, so it can depend on the jar.
    A homepage GET mimics Vinted's priming: on a clean jar it issues a fresh
    access + refresh token, but with a refresh token already present it issues
    nothing (the real behaviour behind the 2026-10 outage)."""

    def __init__(self, pages, token_exp=None):
        self.pages = pages
        self.get_calls = []
        # A day-valid token by default -> prepare() won't fetch.
        exp = time.time() + 86400 if token_exp is None else token_exp
        self.cookie_jar = _FakeJar(
            [_cookie("access_token_web", _jwt(exp)), _cookie("refresh_token_web")]
        )

    def get(self, url, timeout=None, headers=None):
        self.get_calls.append(url)
        if url == vinted.BASE_URL and not any(
            c.key == "refresh_token_web" for c in self.cookie_jar
        ):
            self.cookie_jar.cookies += [
                _cookie("access_token_web", _jwt(time.time() + 86400)),
                _cookie("refresh_token_web"),
            ]
        page = self.pages.get(url, "")
        return _FakeResponse(page(self) if callable(page) else page)


class TestHardverApro:
    scraper = pm.HardverAproScraper(None)

    def item(
        self,
        uadid="7600155",
        price="50 000 Ft",
        title="Xbox Series S eladó",
        href="/apro/xbox_series_s_elado_rkma/friss.html",
    ):
        return soup_fragment(f"""
            <li class="media" data-uadid="{uadid}">
              <h1><a href="{href}">{title}</a></h1>
              <div class="uad-price"><span class="text-nowrap">{price}</span></div>
            </li>
        """)

    def test_happy_path(self):
        cfg = pm.SearchConfig(
            site="HardverApro", search_terms=["xbox", "series", "s"], max_price=60000
        )
        ad = self.scraper.process_ad_item(self.item(), cfg)
        assert ad is not None
        assert ad.id == "HardverApro_7600155"
        assert ad.price == 50000
        assert ad.url == "https://hardverapro.hu/apro/xbox_series_s_elado_rkma/friss.html"
        assert ad.site == "HardverApro"

    def test_missing_ad_id_returns_none(self):
        cfg = pm.SearchConfig(site="HardverApro", search_terms=[], max_price=60000)
        frag = soup_fragment("""
            <li class="media">
              <h1><a href="/x">Cím</a></h1>
              <div class="uad-price"><span class="text-nowrap">1000 Ft</span></div>
            </li>
        """)
        assert self.scraper.process_ad_item(frag, cfg) is None

    def test_price_above_max_returns_none(self):
        cfg = pm.SearchConfig(site="HardverApro", search_terms=[], max_price=10000)
        assert self.scraper.process_ad_item(self.item(), cfg) is None

    def test_search_term_mismatch_returns_none(self):
        cfg = pm.SearchConfig(site="HardverApro", search_terms=["playstation"], max_price=60000)
        assert self.scraper.process_ad_item(self.item(), cfg) is None

    def test_min_price_filters_out_cheap_false_match(self):
        # Searching for an "n305" (Alder Lake-N) CPU also substring-matches a
        # cheap old "N3050" Celeron listing; a lower price bound rules it out
        # while a genuine n305 board (well above the floor) still passes.
        item = self.item(title="Intel Celeron N3050 mini PC", price="8 000 Ft")
        no_floor = pm.SearchConfig(site="HardverApro", search_terms=["n305"], max_price=200000)
        assert self.scraper.process_ad_item(item, no_floor) is not None
        with_floor = pm.SearchConfig(
            site="HardverApro", search_terms=["n305"], max_price=200000, min_price=30000
        )
        assert self.scraper.process_ad_item(item, with_floor) is None

    def test_exclude_terms_drops_155hz_monitor(self):
        # Searching "155h" (a laptop CPU suffix) substring-matches a "155Hz"
        # monitor; excluding "hz" drops the monitor. Without the exclusion the
        # same listing passes, proving the exclusion is what filters it.
        item = self.item(title="LG UltraGear 155Hz Gaming Monitor", price="90 000 Ft")
        no_exclude = pm.SearchConfig(site="HardverApro", search_terms=["155h"], max_price=200000)
        assert self.scraper.process_ad_item(item, no_exclude) is not None
        excluded = pm.SearchConfig(
            site="HardverApro", search_terms=["155h"], max_price=200000, exclude_terms=["hz"]
        )
        assert self.scraper.process_ad_item(item, excluded) is None

    def test_build_search_url_prefers_explicit_url(self):
        cfg = pm.SearchConfig(
            site="HardverApro", search_terms=["x"], max_price=1000, url="https://example.com/fixed"
        )
        assert self.scraper.build_search_url(cfg) == "https://example.com/fixed"

    def test_build_search_url_falls_back_to_terms(self):
        cfg = pm.SearchConfig(site="HardverApro", search_terms=["iets", "gt500"], max_price=20000)
        url = self.scraper.build_search_url(cfg)
        assert url == "https://hardverapro.hu/aprok/keres.php?stext=iets+gt500&stprice=20000"


class TestMoly:
    scraper = pm.MolyScraper(None)

    def item(
        self,
        copy_id="copy_456",
        stars="★★★★★",
        price="5000 Ft",
        title="Teszt Könyv",
        example_href="/pelda/456",
    ):
        example = f'<a class="button_icon" href="{example_href}"></a>' if example_href else ""
        return soup_fragment(f"""
            <div class="copy" id="{copy_id}">
              <div class="right">Eladó {stars} {price}</div>
              <a class="book_selector">{title}</a>
              {example}
            </div>
        """)

    def default_cfg(self):
        return pm.SearchConfig(
            site="Moly",
            search_terms=[],
            max_price=10000,
            url="https://moly.hu/konyvek/teszt/elado-peldanyok",
        )

    def test_happy_path(self):
        ad = self.scraper.process_ad_item(self.item(), self.default_cfg())
        assert ad is not None
        assert ad.id == "Moly_456"
        assert ad.price == 5000
        assert ad.title == "Teszt Könyv"
        assert ad.url == "https://moly.hu/pelda/456"

    def test_requires_exactly_five_stars(self):
        four_stars = self.item(stars="★★★★")
        assert self.scraper.process_ad_item(four_stars, self.default_cfg()) is None

    def test_requires_elado_in_text(self):
        frag = soup_fragment("""
            <div class="copy" id="copy_1">
              <div class="right">Elkelt ★★★★★ 5000 Ft</div>
              <a class="book_selector">Cím</a>
            </div>
        """)
        assert self.scraper.process_ad_item(frag, self.default_cfg()) is None

    def test_missing_example_link_falls_back_to_search_url(self):
        no_example = self.item(example_href=None)
        ad = self.scraper.process_ad_item(no_example, self.default_cfg())
        assert ad.url == "https://moly.hu/konyvek/teszt/elado-peldanyok"

    def test_wrong_id_prefix_returns_none(self):
        frag = soup_fragment("""
            <div class="copy" id="notcopy_1">
              <div class="right">Eladó ★★★★★ 5000 Ft</div>
              <a class="book_selector">Cím</a>
            </div>
        """)
        assert self.scraper.process_ad_item(frag, self.default_cfg()) is None

    def test_exclude_terms_apply_even_without_search_terms(self):
        # Moly matches by book URL, not search_terms, but exclude_terms still
        # filters on the (book) title - here a "díszkiadás" the user wants to skip.
        cfg = pm.SearchConfig(
            site="Moly",
            search_terms=[],
            max_price=10000,
            url="https://moly.hu/konyvek/teszt/elado-peldanyok",
            exclude_terms=["diszkiadas"],
        )
        assert self.scraper.process_ad_item(self.item(title="1984 díszkiadás"), cfg) is None
        assert self.scraper.process_ad_item(self.item(title="1984 puhafedeles"), cfg) is not None


class TestVatera:
    scraper = pm.VateraScraper(None)

    def item(
        self,
        product_id="3497299823",
        price="9 000 Ft",
        title="George Orwell - 1984",
        href="https://www.vatera.hu/george-orwell-1984-3497299823.html",
    ):
        return soup_fragment(f"""
            <div class="gtm-impression" data-product-id="{product_id}">
              <a class="product_link" href="{href}"><h3>{title}</h3></a>
              <span class="originalVal">{price}</span>
            </div>
        """)

    def test_happy_path(self):
        cfg = pm.SearchConfig(site="Vatera", search_terms=["orwell", "1984"], max_price=15000)
        ad = self.scraper.process_ad_item(self.item(), cfg)
        assert ad is not None
        assert ad.id == "Vatera_3497299823"
        assert ad.price == 9000
        assert ad.url == "https://www.vatera.hu/george-orwell-1984-3497299823.html"

    def test_relative_url_gets_joined(self):
        cfg = pm.SearchConfig(site="Vatera", search_terms=[], max_price=15000)
        ad = self.scraper.process_ad_item(
            self.item(href="/george-orwell-1984-3497299823.html"), cfg
        )
        assert ad.url == "https://www.vatera.hu/george-orwell-1984-3497299823.html"

    def test_missing_product_id_returns_none(self):
        cfg = pm.SearchConfig(site="Vatera", search_terms=[], max_price=15000)
        frag = soup_fragment("""
            <div class="gtm-impression">
              <a class="product_link" href="/x"><h3>Cím</h3></a>
              <span class="originalVal">1000 Ft</span>
            </div>
        """)
        assert self.scraper.process_ad_item(frag, cfg) is None

    def test_exclude_terms_drops_dock_false_match(self):
        # "mac"+"mini"+"m1" as AND-ed terms also match a docking-station
        # listing; excluding "dokkolo" drops it. Diacritic-insensitive, so the
        # accented "dokkoló" in the title is caught by the unaccented term.
        item = self.item(product_id="99", title="USB-C dokkoló Mac mini M1-hez", price="12 000 Ft")
        terms = ["mac", "mini", "m1"]
        no_exclude = pm.SearchConfig(site="Vatera", search_terms=terms, max_price=300000)
        assert self.scraper.process_ad_item(item, no_exclude) is not None
        excluded = pm.SearchConfig(
            site="Vatera", search_terms=terms, max_price=300000, exclude_terms=["dokkolo"]
        )
        assert self.scraper.process_ad_item(item, excluded) is None

    def test_build_search_url(self):
        cfg = pm.SearchConfig(site="Vatera", search_terms=["orwell", "1984"], max_price=15000)
        assert (
            self.scraper.build_search_url(cfg)
            == "https://www.vatera.hu/listings/?q=orwell+1984&sort=end_time"
        )


class TestJofogas:
    """As of 2026-07-03, jofogas.hu is a Next.js app - listing data comes
    from the __NEXT_DATA__ JSON blob (props.pageProps.adList.ads), not the
    DOM, so process_ad_item() takes a plain dict here, not a bs4 element."""

    scraper = pm.JofogasScraper(None)

    def item(
        self,
        list_id=321,
        price=8500,
        title="Orwell 1984",
        url="https://www.jofogas.hu/valami-321.htm",
    ):
        return {
            "list_id": list_id,
            "subject": title,
            "price": {"value": price, "label": f"{price} Ft"},
            "url": url,
        }

    def test_happy_path(self):
        cfg = pm.SearchConfig(site="Jofogas", search_terms=["orwell"], max_price=15000)
        ad = self.scraper.process_ad_item(self.item(), cfg)
        assert ad is not None
        assert ad.id == "Jofogas_321"
        assert ad.price == 8500
        assert ad.url == "https://www.jofogas.hu/valami-321.htm"

    def test_missing_list_id_returns_none(self):
        cfg = pm.SearchConfig(site="Jofogas", search_terms=[], max_price=15000)
        item = self.item()
        del item["list_id"]
        assert self.scraper.process_ad_item(item, cfg) is None

    def test_price_above_max_returns_none(self):
        cfg = pm.SearchConfig(site="Jofogas", search_terms=[], max_price=1000)
        assert self.scraper.process_ad_item(self.item(price=8500), cfg) is None

    def test_search_term_mismatch_returns_none(self):
        cfg = pm.SearchConfig(site="Jofogas", search_terms=["xbox"], max_price=15000)
        assert self.scraper.process_ad_item(self.item(), cfg) is None

    def test_min_price_filters_below_floor(self):
        cfg = pm.SearchConfig(
            site="Jofogas", search_terms=["orwell"], max_price=15000, min_price=5000
        )
        assert self.scraper.process_ad_item(self.item(price=3000), cfg) is None
        assert self.scraper.process_ad_item(self.item(price=8500), cfg) is not None

    def test_build_search_url(self):
        cfg = pm.SearchConfig(site="Jofogas", search_terms=["orwell", "1984"], max_price=15000)
        assert (
            self.scraper.build_search_url(cfg)
            == "https://www.jofogas.hu/magyarorszag?q=orwell+1984"
        )


class TestVinted:
    """Vinted (books only). process_ad_item() takes a single item-card <a>
    element whose `title` attribute holds all the data, comma-structured:
        "{title}[, Márka: X], Állapot: {condition}[, Méret: Y], {price} Ft, {total} Ft"
    The price stored is the buyer-protection TOTAL (the last "... Ft"), and the
    accepted condition set is hardcoded (top-3 tiers), like Moly's 5 stars.
    """

    scraper = pm.VintedScraper(None)

    def item(
        self,
        item_id="9274645524",
        condition="Kiváló",
        price="35000.00",
        total="37030.00",
        title="George Orwell: 1984",
        href=None,
    ):
        href = href or f"/items/{item_id}-george-orwell-1984"
        title_attr = f"{title}, Állapot: {condition}, {price} Ft, {total} Ft"
        return soup_fragment(f'<a href="{href}" title="{title_attr}">x</a>')

    def default_cfg(self, **kw):
        kw.setdefault("site", "Vinted")
        kw.setdefault("search_terms", ["orwell", "1984"])
        kw.setdefault("max_price", 40000)
        return pm.SearchConfig(**kw)

    def test_happy_path(self):
        ad = self.scraper.process_ad_item(self.item(), self.default_cfg())
        assert ad is not None
        assert ad.id == "Vinted_9274645524"
        assert ad.title == "George Orwell: 1984"
        assert ad.url == "https://www.vinted.hu/items/9274645524-george-orwell-1984"
        assert ad.site == "Vinted"

    def test_price_is_the_buyer_protection_total(self):
        # Stored price is the total (37030), NOT the listing price (35000).
        ad = self.scraper.process_ad_item(self.item(), self.default_cfg())
        assert ad.price == 37030

    def test_max_price_filters_on_total_not_listing(self):
        # 35000 listing / 37030 total: a budget between the two must reject it,
        # proving the gate is on the total the user actually pays.
        cfg = self.default_cfg(max_price=36000)
        assert self.scraper.process_ad_item(self.item(), cfg) is None

    def test_accepts_new_with_tags_comma_value(self):
        # "Új, címkékkel" itself contains a comma - must not break the parse.
        ad = self.scraper.process_ad_item(self.item(condition="Új, címkékkel"), self.default_cfg())
        assert ad is not None

    def test_accepts_new_without_tags_comma_value(self):
        ad = self.scraper.process_ad_item(
            self.item(condition="Új, címkék nélkül"), self.default_cfg()
        )
        assert ad is not None

    def test_rejects_good_condition(self):
        # "Jó" is deliberately below the accepted bar (the Moly-5★ analog).
        assert self.scraper.process_ad_item(self.item(condition="Jó"), self.default_cfg()) is None

    def test_rejects_satisfactory_condition(self):
        assert (
            self.scraper.process_ad_item(self.item(condition="Kielégítő"), self.default_cfg())
            is None
        )

    def test_unknown_condition_fails_safe(self):
        # A condition string we don't know is rejected, not let through.
        assert (
            self.scraper.process_ad_item(self.item(condition="Újszerű"), self.default_cfg()) is None
        )

    def test_search_term_mismatch_returns_none(self):
        cfg = self.default_cfg(search_terms=["playstation"])
        assert self.scraper.process_ad_item(self.item(), cfg) is None

    def test_missing_item_id_returns_none(self):
        frag = self.item(href="/member/profile/123")
        assert self.scraper.process_ad_item(frag, self.default_cfg()) is None

    def test_strips_brand_segment_from_title(self):
        frag = self.item(title="Nike pulóver, Márka: Nike", condition="Kiváló")
        cfg = self.default_cfg(search_terms=["nike"], max_price=100000)
        ad = self.scraper.process_ad_item(frag, cfg)
        assert ad is not None
        assert ad.title == "Nike pulóver"

    def test_exclude_terms_drops_match(self):
        cfg = self.default_cfg(exclude_terms=["orwell"])
        assert self.scraper.process_ad_item(self.item(), cfg) is None

    def test_build_search_url(self):
        # Also exercises "+"-joining and accented-char percent-encoding (ó -> %C3%B3).
        cfg = self.default_cfg(search_terms=["régi", "könyv"])
        assert (
            self.scraper.build_search_url(cfg)
            == "https://www.vinted.hu/catalog?search_text=r%C3%A9gi+k%C3%B6nyv&order=newest_first"
        )


class TestVintedHelpers:
    """Module-level helpers used by both the scraper and the health check."""

    def anchor_html(self, condition, title="Egy könyv", item_id="111"):
        title_attr = f"{title}, Állapot: {condition}, 1000.00 Ft, 1150.00 Ft"
        return f'<a href="/items/{item_id}-egy-konyv" title="{title_attr}">x</a>'

    def test_extract_condition_token_recognises_comma_value(self):
        title = "Valami, Állapot: Új, címkék nélkül, Méret: M, 1000.00 Ft, 1150.00 Ft"
        assert vinted.extract_condition_token(title) == "Új, címkék nélkül"

    def test_extract_condition_token_plain_value(self):
        title = "Valami, Állapot: Kiváló, 1000.00 Ft, 1150.00 Ft"
        assert vinted.extract_condition_token(title) == "Kiváló"

    def test_extract_condition_token_unknown(self):
        title = "Valami, Állapot: Újszerű, 1000.00 Ft, 1150.00 Ft"
        assert vinted.extract_condition_token(title) == "Újszerű"

    def test_count_item_cards(self):
        html = self.anchor_html("Kiváló", item_id="1") + self.anchor_html("Jó", item_id="2")
        assert vinted.count_item_cards(html) == 2

    def test_unknown_conditions_none_when_all_known(self):
        html = self.anchor_html("Kiváló", item_id="1") + self.anchor_html("Kielégítő", item_id="2")
        assert vinted.unknown_conditions(html) is None

    def test_unknown_conditions_flags_new_label(self):
        html = self.anchor_html("Kiváló", item_id="1") + self.anchor_html("Újszerű", item_id="2")
        msg = vinted.unknown_conditions(html)
        assert msg is not None
        assert "Újszerű" in msg

    def test_extract_seller_feedback_escaped_rsc(self):
        # The real item page carries these in escaped RSC JSON.
        html = r"...\"business\":false,\"feedback_count\":145,\"feedback_reputation\":0.98,..."
        rep, count = vinted.extract_seller_feedback(html)
        assert rep == 0.98
        assert count == 145

    def test_extract_seller_feedback_plain_json(self):
        html = '..."feedback_count":10,"feedback_reputation":1,...'
        rep, count = vinted.extract_seller_feedback(html)
        assert rep == 1.0
        assert count == 10

    def test_extract_seller_feedback_missing(self):
        assert vinted.extract_seller_feedback("<html>no seller data</html>") == (None, None)

    def test_seller_is_trusted_at_both_thresholds(self):
        # 4.5★ (0.9) and 5 reviews are the exact bars - both inclusive.
        assert vinted.seller_is_trusted(0.9, 5) is True

    def test_seller_rejected_reputation_too_low(self):
        assert vinted.seller_is_trusted(0.89, 500) is False

    def test_seller_rejected_too_few_reviews(self):
        # A perfect score on only 4 reviews still fails (just under the bar).
        assert vinted.seller_is_trusted(1.0, 4) is False

    def test_seller_rejected_when_data_missing(self):
        assert vinted.seller_is_trusted(None, 100) is False
        assert vinted.seller_is_trusted(0.99, None) is False


class TestVintedRequestDiscipline:
    """The cache / pacing / header behaviour that keeps Vinted (DataDome + the
    per-candidate item fetches) from tripping rate limits. Network-free: a
    _FakeSession serves canned pages and records GETs."""

    CATALOG = "https://www.vinted.hu/catalog?search_text=orwell&order=newest_first"
    URL1 = "https://www.vinted.hu/items/111-orwell-1984"
    URL2 = "https://www.vinted.hu/items/222-orwell-masik"

    def _card(self, item_id, slug):
        title = "Orwell 1984, Állapot: Kiváló, 1000.00 Ft, 1150.00 Ft"
        return f'<a href="/items/{item_id}-{slug}" title="{title}">x</a>'

    def _item_page(self, count, reputation):
        return f'x\\"feedback_count\\":{count},\\"feedback_reputation\\":{reputation}x'

    def _scraper(self, pages):
        session = _FakeSession(pages)
        s = pm.VintedScraper(session)
        s._fetch_min_interval = 0  # no real sleeping in tests
        s._fetch_jitter = 0
        return s, session

    def _cfg(self):
        return pm.SearchConfig(site="Vinted", search_terms=["orwell"], max_price=5000)

    def test_nav_headers_firefox_consistent(self):
        # Sec-Fetch-* present, NO Chromium-only sec-ch-ua (UA is Firefox).
        with_ref = vinted.VintedScraper._nav_headers("https://www.vinted.hu/")
        assert with_ref["Sec-Fetch-Site"] == "same-origin"
        assert with_ref["Referer"] == "https://www.vinted.hu/"
        assert not any(h.lower().startswith("sec-ch-ua") for h in with_ref)
        no_ref = vinted.VintedScraper._nav_headers()
        assert no_ref["Sec-Fetch-Site"] == "none"
        assert "Referer" not in no_ref

    def test_seller_gate_keeps_only_trusted(self):
        pages = {
            self.CATALOG: self._card("111", "orwell-1984") + self._card("222", "orwell-masik"),
            self.URL1: self._item_page(10, 1),  # trusted
            self.URL2: self._item_page(2, 1),  # too few reviews
        }
        s, _ = self._scraper(pages)
        ads = asyncio.run(s.fetch_advertisements(self._cfg()))
        assert [a.id for a in ads] == ["Vinted_111"]

    def test_cache_avoids_refetch_next_cycle(self):
        pages = {
            self.CATALOG: self._card("111", "orwell-1984") + self._card("222", "orwell-masik"),
            self.URL1: self._item_page(10, 1),
            self.URL2: self._item_page(2, 1),
        }
        s, session = self._scraper(pages)

        asyncio.run(s.fetch_advertisements(self._cfg()))
        first = list(session.get_calls)
        # catalog + both item pages
        assert first.count(self.URL1) == 1
        assert first.count(self.URL2) == 1

        asyncio.run(s.fetch_advertisements(self._cfg()))
        # Second cycle: catalog re-fetched, but both item pages served from cache.
        assert session.get_calls.count(self.URL1) == 1
        assert session.get_calls.count(self.URL2) == 1
        assert session.get_calls.count(self.CATALOG) == 2

    def test_fetch_error_not_cached(self):
        # URL1 missing from pages -> "" -> no feedback fields -> untrusted, and
        # (per _seller_trusted) a real fetch exception is not cached. Here we
        # assert the simpler property: an empty/parse-less page is untrusted.
        pages = {self.CATALOG: self._card("111", "orwell-1984"), self.URL1: "no data here"}
        s, _ = self._scraper(pages)
        ads = asyncio.run(s.fetch_advertisements(self._cfg()))
        assert ads == []


class TestVintedSession:
    """Session/token handling - the 2026-10 outage: the 24h access token outlived
    by its 7-day cookie made every catalog page come back with 0 cards ~24h after
    each service start, and a plain homepage re-GET couldn't fix it while the
    refresh token was still in the jar. _FakeSession reproduces that server
    behaviour (no new token while a refresh token is present)."""

    CATALOG = "https://www.vinted.hu/catalog?search_text=orwell&order=newest_first"
    ITEM = "https://www.vinted.hu/items/111-orwell-1984"
    CARD = '<a href="/items/111-orwell-1984" title="Orwell 1984, Állapot: Kiváló, 1000.00 Ft, 1150.00 Ft">x</a>'
    TRUSTED_SELLER = 'x\\"feedback_count\\":10,\\"feedback_reputation\\":1x'

    def _scraper(self, pages, token_exp=None):
        session = _FakeSession(pages, token_exp=token_exp)
        s = pm.VintedScraper(session)
        s._fetch_min_interval = 0
        s._fetch_jitter = 0
        return s, session

    def _cfg(self):
        return pm.SearchConfig(site="Vinted", search_terms=["orwell"], max_price=5000)

    @staticmethod
    def _cards_if_fresh(card):
        """A catalog page that only renders cards for a still-valid token."""

        def page(session):
            fresh = any(
                c.key == "access_token_web"
                and (vinted.access_token_expiry(c.value) or 0) > time.time()
                for c in session.cookie_jar
            )
            return card if fresh else ""

        return page

    def test_access_token_expiry(self):
        assert vinted.access_token_expiry(_jwt(1792601339)) == 1792601339
        assert vinted.access_token_expiry("not-a-jwt") is None
        assert vinted.access_token_expiry("a.!!!.c") is None

    def test_valid_token_does_not_prime(self):
        s, session = self._scraper({})
        asyncio.run(s.prepare())
        assert session.get_calls == []

    def test_expired_token_reprimes_from_clean_jar(self):
        s, session = self._scraper({}, token_exp=time.time() - 3600)
        asyncio.run(s.prepare())
        assert session.get_calls == [vinted.BASE_URL]
        # The jar was wiped first - otherwise the leftover refresh token would
        # have stopped the homepage from issuing a new access token.
        assert session.cookie_jar.cleared == 1
        assert s._has_valid_token()

    def test_token_expiring_within_margin_reprimes(self):
        s, session = self._scraper({}, token_exp=time.time() + 600)
        asyncio.run(s.prepare())
        assert session.get_calls == [vinted.BASE_URL]

    def test_missing_token_reprimes(self):
        s, session = self._scraper({})
        session.cookie_jar.cookies = [_cookie("refresh_token_web")]
        asyncio.run(s.prepare())
        assert session.get_calls == [vinted.BASE_URL]
        assert s._has_valid_token()

    def test_undecodable_token_counts_as_valid(self):
        s, session = self._scraper({})
        session.cookie_jar.cookies = [_cookie("access_token_web", "opaque")]
        asyncio.run(s.prepare())
        assert session.get_calls == []

    def test_stale_session_recovers_without_restart(self):
        # The production scenario: token expired, refresh token still in the jar.
        pages = {self.CATALOG: self._cards_if_fresh(self.CARD), self.ITEM: self.TRUSTED_SELLER}
        s, session = self._scraper(pages, token_exp=time.time() - 3600)
        ads = asyncio.run(s.fetch_advertisements(self._cfg()))
        assert [a.id for a in ads] == ["Vinted_111"]
        # Re-primed up front, so no wasted 0-card catalog request.
        assert session.get_calls.count(vinted.BASE_URL) == 1
        assert session.get_calls.count(self.CATALOG) == 1

    def test_zero_cards_forces_clean_reprime_and_retry(self):
        # The token looks fine to us (expiry unreadable) but the server returns
        # 0 cards until the jar is wiped and re-primed.
        def catalog(session):
            return self.CARD if session.cookie_jar.cleared else ""

        pages = {self.CATALOG: catalog, self.ITEM: self.TRUSTED_SELLER}
        s, session = self._scraper(pages)
        session.cookie_jar.cookies = [
            _cookie("access_token_web", "opaque"),
            _cookie("refresh_token_web"),
        ]
        ads = asyncio.run(s.fetch_advertisements(self._cfg()))
        assert [a.id for a in ads] == ["Vinted_111"]
        assert session.get_calls.count(vinted.BASE_URL) == 1
        assert session.get_calls.count(self.CATALOG) == 2

    def test_forced_reprime_not_repeated_right_after_one(self):
        # Two searches that both get 0 cards: only the first wipes + re-primes
        # (and retries); the second, seconds later, doesn't thrash the jar.
        s, session = self._scraper({self.CATALOG: ""})
        asyncio.run(s.fetch_advertisements(self._cfg()))
        asyncio.run(s.fetch_advertisements(self._cfg()))
        assert session.get_calls.count(vinted.BASE_URL) == 1
        assert session.cookie_jar.cleared == 1
        assert session.get_calls.count(self.CATALOG) == 3
