"""Card-parsing tests for each site's process_ad_item(), against small
hand-built HTML fragments that mirror the real selectors. Network-free -
process_ad_item() takes an already-parsed BeautifulSoup element, it never
touches self.session.
"""

from bs4 import BeautifulSoup

import product_monitor as pm


def soup_fragment(html: str):
    return BeautifulSoup(html, "html.parser").find(True)


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
