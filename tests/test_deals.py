"""Deal watchers (product_monitor/deals): XiaomiPlanet price boxes.
Network-free - the watchers get a fake session that
serves canned pages by URL and records every GET (injected via the
constructor, no monkeypatching, same as the Vinted tests).

The HTML fragments mirror the live markup (verified 2026-10-21).
"""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import product_monitor as pm
from product_monitor import health_check
from product_monitor.deals import xiaomiplanet
from product_monitor.deals.state import DealState
from product_monitor.models import DealsConfig, XiaomiPlanetConfig


class _FakeResponse:
    def __init__(self, text):
        self._text = text

    async def __aenter__(self):
        if isinstance(self._text, Exception):
            raise self._text
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def text(self, encoding=None):
        return self._text


class _FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.get_calls = []

    def get(self, url, timeout=None, headers=None):
        self.get_calls.append(url)
        page = self.pages.get(url, "")
        return _FakeResponse(page() if callable(page) else page)


# --- XiaomiPlanet fixtures -------------------------------------------------


def panel(
    var,
    price,
    *,
    old=None,
    card=None,
    coupon="DEALOCT50",
    shop="DHGate",
    updated="5. 10. 2026 o 15:40",
    history=None,
    cta="https://xiaomiplanet.sk/coupon-redirect/abc",
):
    card_html = (
        f'<span class="cp-pbox__cardfee"><svg></svg><span>Pri platbe kartou cena {card} '
        "(+4,5 %)</span></span>"
        if card
        else ""
    )
    old_html = (
        f'<div class="cp-pbox__oldrow"><span class="cp-pbox__old">{old}</span>'
        '<span class="cp-eyebrow cp-pbox__save">Ušetríš</span></div>'
        if old
        else ""
    )
    code_html = (
        '<div class="cp-pbox__coupons"><div class="cp-pbox__coupon-row">'
        '<span class="cp-eyebrow cp-pbox__coupon-label">Kupón</span>'
        f'<span class="cp-pbox__code cp-disp" data-ppb-copy="{coupon}">{coupon}'
        '<span class="cp-pbox__copied">Skopírované!</span></span>'
        f'<button class="cp-pbox__copy" data-ppb-copy="{coupon}"></button></div></div>'
        if coupon
        else ""
    )
    history_html = (
        '<dialog class="cp-pbox-modal cp-pbox-modal--history"><div class="cp-pbox-modal__body">'
        f'<script type="application/json" data-ppb-history-json>{json.dumps(history)}</script>'
        "</div></dialog>"
        if history
        else ""
    )
    price_html = f'<div class="cp-pbox__price cp-disp">{price}{card_html}</div>' if price else ""
    return (
        f'<div class="cp-pbox__panel" data-ppb-panel="0" data-ppb-var="{var}" '
        f'data-ppb-shop="{shop}"><div class="cp-pbox__main"><div class="cp-pbox__info">'
        f"{old_html}{price_html}{code_html}</div></div>{history_html}"
        f'<div class="cp-pbox__cta-row"><a class="cp-pbox__cta cp-disp" href="{cta}" '
        "data-ppb-out>VYUŽIŤ ZĽAVU →</a></div>"
        f'<div class="cp-pbox__updated"><svg></svg><span>Aktualizované {updated}</span></div>'
        "</div>"
    )


def box(box_id, title, variants):
    """variants: (var, label, ship, panel_html) tuples."""
    buttons = "".join(
        f'<button type="button" class="cp-pbox__variant" data-ppb-var="{var}">'
        f'<span class="cp-pbox__variant-in"><span class="cp-disp">{label}</span> '
        '<span class="cp-pbox__variant-price">x</span></span>'
        f'<span class="cp-pbox__variant-ship"><span class="cp-pbox__variant-ship-txt">{ship}'
        "</span></span></button>"
        for var, label, ship, _ in variants
    )
    panels = "".join(p for *_, p in variants)
    return (
        f'<div class="cp-pbox" data-ppb-box data-ppb-box-id="{box_id}">'
        '<div class="cp-pbox__head"><span class="cp-pbox__eyebrow">Ponuka XiaomiPlanet</span>'
        f'<h3 class="cp-pbox__title">{title}</h3></div>'
        f'<div class="cp-pbox__variants">{buttons}</div>{panels}</div>'
    )


def note17_pro_box(
    price_256="389,00 €",
    price_512="455,00 €",
    extra=(),
    coupon="DEALOCT50",
    updated="5. 10. 2026 o 15:40",
):
    return box(
        "7108",
        "REDMI Note 17 Pro Globálna Verzia",
        [
            (
                "1201",
                "ES sklad, 12 + 256 GB",
                "ES sklad",
                panel(
                    "1201",
                    price_256,
                    old="629,90 €",
                    coupon=coupon,
                    updated=updated,
                    history=[[1, 430.0], [2, 400.89]],
                ),
            ),
            (
                "1202",
                "ES sklad, 12 + 512 GB",
                "ES sklad",
                panel("1202", price_512, coupon=coupon, updated=updated),
            ),
            *extra,
        ],
    )


def note13_box():
    return box(
        "7101",
        "Redmi Note 13 Globálna Verzia",
        [("1001", "Španielsky sklad, 12 + 512 GB", "ES sklad", panel("1001", "239,90 €"))],
    )


def note16_box_with_charger(updated="18. 7. 2026 o 08:10"):
    return box(
        "7105",
        "REDMI Note 16 Globálna Verzia",
        [
            (
                "1101",
                "EÚ sklad, 12 + 256 GB",
                "ES sklad",
                panel("1101", "284,90 €", coupon=None, updated=updated),
            ),
            (
                "1102",
                "SK sklad, Xiaomi 67 W Charging Combo",
                "SK sklad",
                panel("1102", "19,90 €", coupon=None, shop="Smarty", updated=updated),
            ),
        ],
    )


def roundup(note17_pro_html):
    """A long article carrying both the Note 17 Pro and the Note 16 box."""
    return roundup_with_note16(note16_box_with_charger(), note17_pro_html)


def roundup_with_note16(note16_html, note17_pro_html=None):
    return "<p>" + "x" * 5000 + "</p>" + (note17_pro_html or note17_pro_box()) + note16_html


def article(post_id, html, slug=None):
    slug = slug or f"post-{post_id}"
    return json.dumps(
        {"id": post_id, "link": f"https://xiaomiplanet.sk/{slug}/", "content": {"rendered": html}}
    )


def post_list(*posts):
    """posts: (id, title, date_gmt[, modified_gmt]) tuples"""
    return json.dumps(
        [
            {
                "id": pid,
                "date_gmt": date,
                "modified_gmt": modified[0] if modified else date,
                "link": f"https://xiaomiplanet.sk/post-{pid}/",
                "title": {"rendered": title},
            }
            for pid, title, date, *modified in posts
        ]
    )


NOW = datetime(2026, 10, 21, 12, 0).timestamp()
CONFIG = dict(
    enabled=True,
    search_terms=["redmi note"],
    exclude_terms=["13", "14"],
    max_price_eur=400,
    min_price_eur=150,
    highlight_price_eur=300,
)


class Harness:
    """A watcher wired to a fake site: post 1 is a small Note 17 Pro article, post 2
    a Note 13 article (excluded model), post 3 a long roundup also carrying the Note
    17 Pro box, post 4 an unrelated title (never read by discovery)."""

    def __init__(self, tmp_path, state=None, **config_overrides):
        self.pages = {
            xiaomiplanet.discovery_url(["redmi note"]): post_list(
                (1, "REDMI Note 17 Pro s kupónom", "2026-10-01T10:00:00"),
                (2, "Redmi Note 13 za 199 €", "2026-09-05T10:00:00"),
                (3, "Veľký prehľad: REDMI Note 17 Pro, Note 16 a ďalšie", "2026-09-01T10:00:00"),
                (4, "Xiaomi S800 váha", "2026-10-19T10:00:00"),
            ),
            xiaomiplanet.latest_url(): post_list((4, "Xiaomi S800 váha", "2026-10-19T10:00:00")),
            xiaomiplanet.article_url(1): article(1, note17_pro_box()),
            xiaomiplanet.article_url(2): article(2, note13_box()),
            xiaomiplanet.article_url(3): article(3, roundup(note17_pro_box())),
            xiaomiplanet.article_url(4): article(4, "<p>no boxes</p>"),
        }
        self.session = _FakeSession(self.pages)
        self.sent = []
        self.state = state or DealState(str(tmp_path / "deal-state.json"))
        config = XiaomiPlanetConfig(**{**CONFIG, **config_overrides})
        self.watcher = xiaomiplanet.XiaomiPlanetWatcher(
            self.session, config, self.state, lambda s, b: self.sent.append((s, b))
        )
        self.watcher.fetch_gap = 0
        self.now = NOW
        self.watcher.clock = lambda: self.now

    def set_note17_pro(self, **box_args):
        """Change the Note 17 Pro box. Box data lives outside the posts (verified
        live: a box renders identically in every article embedding it), so the
        change shows in both articles carrying it."""
        html = note17_pro_box(**box_args)
        self.pages[xiaomiplanet.article_url(1)] = article(1, html)
        self.pages[xiaomiplanet.article_url(3)] = article(3, roundup(html))

    def tick(self, advance=0):
        self.now += advance
        self.session.get_calls.clear()
        asyncio.run(self.watcher.tick())
        return self.sent


class TestParsing:
    def test_parse_eur_formats(self):
        assert xiaomiplanet.parse_eur("579,90 €") == 579.90
        assert xiaomiplanet.parse_eur("1 049,00 €") == 1049.00
        assert xiaomiplanet.parse_eur("1 049,00 €") == 1049.00
        assert xiaomiplanet.parse_eur("577.92 €") == 577.92
        assert xiaomiplanet.parse_eur("Pri platbe kartou cena 689,83 € (+4,5 %)") == 689.83
        assert xiaomiplanet.parse_eur("no price") is None

    def test_parse_updated(self):
        assert xiaomiplanet.parse_updated("Aktualizované 21. 10. 2026 o 14:05") == (
            "2026-10-21T14:05:00"
        )
        assert xiaomiplanet.parse_updated("nothing") is None

    def test_multi_variant_box(self):
        offers = xiaomiplanet.parse_price_boxes(note17_pro_box(), "https://xiaomiplanet.sk/a/")
        assert [o.key for o in offers] == ["7108:1201", "7108:1202"]
        first = offers[0]
        assert first.product == "REDMI Note 17 Pro Globálna Verzia"
        assert first.variant == "ES sklad, 12 + 256 GB"
        assert first.warehouse == "ES sklad"
        assert first.price == 389.0
        assert first.old_price == 629.90
        assert first.coupon == "DEALOCT50"
        assert first.shop == "DHGate"
        assert first.updated == "2026-10-05T15:40:00"
        assert first.history_min == 400.89
        assert first.buy_url == "https://xiaomiplanet.sk/coupon-redirect/abc"
        assert first.article_url == "https://xiaomiplanet.sk/a/"

    def test_card_surcharge_is_separated_from_the_price(self):
        html = box(
            "7112",
            "REDMI Note 18 Pro Globálna Verzia",
            [
                (
                    "1301",
                    "Bezcolné dodanie, 12+256 GB",
                    "GShopper",
                    panel("1301", "589,00 €", card="615,51 €", shop="Gshopper"),
                )
            ],
        )
        (offer,) = xiaomiplanet.parse_price_boxes(html)
        assert offer.price == 589.00
        assert offer.card_price == 615.51

    def test_single_offer_box_without_variant_tabs(self):
        html = (
            '<div class="cp-pbox" data-ppb-box-id="5635"><div class="cp-pbox__head">'
            '<h3 class="cp-pbox__title">Mijia Night Light 4</h3>'
            '<div class="cp-pbox__title-variant">Gshopper</div></div>'
            '<div class="cp-pbox__panel" data-ppb-var="10421" data-ppb-shop="Gshopper">'
            '<span class="cp-pbox__shop-tag"><span class="cp-eyebrow">CZ sklad</span></span>'
            '<div class="cp-pbox__price cp-disp">20,00 €</div></div></div>'
        )
        (offer,) = xiaomiplanet.parse_price_boxes(html)
        assert offer.variant == "Gshopper"
        assert offer.warehouse == "CZ sklad"
        assert offer.coupon is None

    def test_panel_without_a_price_is_skipped(self):
        html = box("1", "REDMI Note 18 Pro", [("2", "x", "y", panel("2", None))])
        assert xiaomiplanet.parse_price_boxes(html) == []

    def test_title_matching(self):
        excl = ["13", "14"]
        terms = ["redmi note"]
        assert xiaomiplanet.title_matches("REDMI Note 17 Pro Globálna Verzia", terms, excl)
        assert not xiaomiplanet.title_matches("Redmi 15C Globálna Verzia", terms, excl)
        assert not xiaomiplanet.title_matches("Redmi Note 13 Pro Globálna Verzia", terms, excl)
        # whole-word exclusion: "13" must not knock out a "130"
        assert xiaomiplanet.title_matches("Redmi Note 130", terms, excl)
        assert not xiaomiplanet.title_matches("Redmi Note 13", terms, excl)


class TestOfferEmail:
    def record(self, price=389.0, **overrides):
        offer = xiaomiplanet.parse_price_boxes(note17_pro_box(), "https://xiaomiplanet.sk/a/")[0]
        record = {**offer.__dict__, "price": price}
        record.update(overrides)
        return record

    def test_single_offer_subject(self):
        subject, _ = xiaomiplanet.build_offer_email([(self.record(), "új ajánlat")], 400, 300)
        assert subject == "REDMI Note 17 Pro – 389,00 € (DHGate)"

    def test_highlight_adds_fire_to_subject_and_item(self):
        subject, body = xiaomiplanet.build_offer_email([(self.record(289.0), "x")], 400, 300)
        assert subject.startswith("🔥 REDMI Note 17 Pro – 289,00 €")
        assert "1. 🔥 REDMI Note 17 Pro" in body

    def test_body_details(self):
        _, body = xiaomiplanet.build_offer_email(
            [(self.record(), "árcsökkenés (409,00 € → 389,00 €)")], 400, 300
        )
        assert "Ár: 389,00 €  (kupon nélkül: 629,90 €)" in body
        assert "Kupon: DEALOCT50  |  Bolt: DHGate  |  Raktár: ES sklad" in body
        assert "Miért most: árcsökkenés (409,00 € → 389,00 €)" in body
        assert "Eddigi legalacsonyabb ár: 400,89 €" in body
        assert "Doboz frissítve: 2026.10.05. 15:40" in body
        assert "Vásárlás: https://xiaomiplanet.sk/coupon-redirect/abc" in body
        assert "Cikk: https://xiaomiplanet.sk/a/" in body

    def test_new_low_is_called_out(self):
        _, body = xiaomiplanet.build_offer_email([(self.record(400.0), "x")], 400, None)
        assert "ez a mélypont" in body

    def test_multiple_offers_subject_names_the_cheapest(self):
        subject, body = xiaomiplanet.build_offer_email(
            [(self.record(395.0), "x"), (self.record(310.0, product="REDMI Note 16"), "y")],
            400,
            300,
        )
        assert subject == "XiaomiPlanet: 2 ajánlat (legolcsóbb: REDMI Note 16 – 310,00 €)"
        assert body.index("REDMI Note 16") < body.index("REDMI Note 17 Pro")  # sorted by price

    def test_summary_subject(self):
        subject, body = xiaomiplanet.build_offer_email([(self.record(), "")], 400, 300, True)
        assert subject == "XiaomiPlanet: 1 db"
        assert "Első futás" in body


class TestXiaomiPlanetWatcher:
    def test_first_tick_sends_one_summary_of_qualifying_offers(self, tmp_path):
        h = Harness(tmp_path)
        sent = h.tick()
        assert len(sent) == 1
        subject, body = sent[0]
        # Note 17 Pro 12/256 (389 €): 12/512 is over the limit, Note 13 is excluded,
        # the 19.90 € charger in the Note 16 box is under the price floor - and the
        # Note 16 itself (285 €) is there too, highlighted.
        assert subject == "XiaomiPlanet: 2 db"
        assert "REDMI Note 17 Pro – ES sklad, 12 + 256 GB" in body
        assert "REDMI Note 16 – EÚ sklad, 12 + 256 GB" in body
        assert "12 + 512 GB" not in body
        assert "Note 13" not in body
        assert "Charging Combo" not in body
        assert h.watcher.initialized
        saved = json.loads((tmp_path / "deal-state.json").read_text(encoding="utf-8"))
        assert saved["xiaomiplanet"]["initialized"] is True

    def test_discovery_skips_unrelated_titles(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        # post 4 is read via the latest list, never via discovery
        assert h.session.get_calls.count(xiaomiplanet.article_url(4)) == 1

    def test_unchanged_data_sends_nothing(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.tick(advance=300)
        assert len(h.sent) == 1

    def test_drop_of_at_least_two_percent_alerts_smaller_does_not(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.set_note17_pro(price_256="379,00 €")
        h.tick(advance=300)
        assert len(h.sent) == 2
        assert "árcsökkenés (389,00 € → 379,00 €)" in h.sent[1][1]
        h.set_note17_pro(price_256="375,00 €")
        h.tick(advance=300)
        assert len(h.sent) == 2  # 1% below the last alert: exchange-rate noise

    def test_dearer_deal_after_a_cheaper_one_alerts_too(self, tmp_path):
        # Missing the cheap deal must not silence the next one that is still under
        # the limit - every deal under the limit counts, not only new lows
        h = Harness(tmp_path)
        h.tick()
        h.set_note17_pro(price_256="359,00 €")
        h.tick(advance=300)
        h.set_note17_pro(price_256="379,00 €")
        h.tick(advance=300)
        assert len(h.sent) == 3
        assert h.sent[2][0] == "REDMI Note 17 Pro – 379,00 € (DHGate)"
        assert "drágult, de még a limit alatt van (359,00 € → 379,00 €)" in h.sent[2][1]
        h.set_note17_pro(price_256="382,00 €")
        h.tick(advance=300)
        assert len(h.sent) == 3  # under 2% up from the last alert: drift

    def test_coupon_change_alerts_even_at_the_same_price(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.set_note17_pro(coupon="DEALNOV50")
        h.tick(advance=300)
        assert len(h.sent) == 2
        assert "kuponkód változott (DEALOCT50 → DEALNOV50)" in h.sent[1][1]
        assert "Kupon: DEALNOV50" in h.sent[1][1]

    def test_box_resaved_at_the_same_price_and_code_alerts(self, tmp_path):
        # A restock of the same coupon can't be told apart from a re-check
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.article_url(3)] = article(
            3, roundup_with_note16(note16_box_with_charger(updated="21. 10. 2026 o 09:00"))
        )
        h.tick(advance=300)
        assert len(h.sent) == 2
        subject, body = h.sent[1]
        assert subject == "🔥 REDMI Note 16 – 284,90 € (DHGate)"
        assert "Miért most: frissítették az ajánlatot (ár és kupon változatlan)" in body
        assert "Charging Combo" not in body  # its stamp moved too, but it's under the floor
        h.tick(advance=300)
        assert len(h.sent) == 2  # once per new stamp

    def test_unreadable_stamp_coming_back_is_no_refresh(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        for updated in ("neuvedené", "18. 7. 2026 o 08:10"):  # a glitch, then the old stamp
            h.pages[xiaomiplanet.article_url(3)] = article(
                3, roundup_with_note16(note16_box_with_charger(updated=updated))
            )
            h.tick(advance=300)
        assert len(h.sent) == 1

    def test_box_resaved_with_a_small_price_change_alerts(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.set_note17_pro(price_256="391,00 €")  # under 2%, no new stamp: drift
        h.tick(advance=300)
        assert len(h.sent) == 1
        h.set_note17_pro(price_256="392,00 €", updated="21. 10. 2026 o 09:00")
        h.tick(advance=300)
        assert len(h.sent) == 2
        assert "frissítették az ajánlatot (391,00 € → 392,00 €)" in h.sent[1][1]

    def test_offer_crossing_under_the_limit_alerts(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.set_note17_pro(price_512="395,00 €")
        h.tick(advance=300)
        assert len(h.sent) == 2
        subject, body = h.sent[1]
        assert subject == "REDMI Note 17 Pro – 395,00 € (DHGate)"
        assert "a limit alá került (455,00 € → 395,00 €)" in body

    def test_going_over_and_back_under_alerts_again(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.set_note17_pro(price_256="420,00 €")
        h.tick(advance=300)
        h.set_note17_pro(price_256="389,00 €")
        h.tick(advance=300)
        assert len(h.sent) == 2
        assert "a limit alá került (420,00 € → 389,00 €)" in h.sent[1][1]

    def test_new_variant_alerts(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        extra = (("9999", "EU sklad, 8 + 256 GB", "ES sklad", panel("9999", "349,00 €")),)
        h.set_note17_pro(extra=extra)
        h.tick(advance=300)
        assert len(h.sent) == 2
        assert "Miért most: új ajánlat" in h.sent[1][1]

    def test_smallest_article_is_the_carrier(self, tmp_path):
        h = Harness(tmp_path)
        # The roundup (post 3) carries only the Note 17 Pro box here, so it is no
        # box's carrier and must never be re-read
        h.pages[xiaomiplanet.article_url(3)] = article(
            3, "<p>" + "x" * 5000 + "</p>" + note17_pro_box()
        )
        h.tick()
        assert h.watcher.carriers["7108"][0] == 1
        h.tick(advance=300)
        assert xiaomiplanet.article_url(1) in h.session.get_calls
        assert xiaomiplanet.article_url(3) not in h.session.get_calls

    def test_long_unchanged_box_is_reread_as_often_as_any_other(self, tmp_path):
        # The Note 16 box was last updated in July - a surprise price can still land
        # there without its article being re-published
        h = Harness(tmp_path)
        h.tick()
        h.tick(advance=200)
        assert xiaomiplanet.article_url(3) not in h.session.get_calls
        h.tick(advance=40)  # box_check_seconds (240) after its last read
        assert xiaomiplanet.article_url(3) in h.session.get_calls
        h.pages[xiaomiplanet.article_url(3)] = article(
            3, note16_box_with_charger().replace("284,90 €", "219,00 €")
        )
        h.tick(advance=240)
        assert h.sent[-1][0] == "🔥 REDMI Note 16 – 219,00 € (DHGate)"

    def test_box_removed_from_its_carrier_moves_to_another_article(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.article_url(1)] = article(1, "<p>box removed</p>")
        h.tick(advance=300)
        assert h.watcher.carriers["7108"][0] == 3  # the roundup still carries it

    def test_box_removed_everywhere_is_dropped(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.article_url(1)] = article(1, "<p>box removed</p>")
        h.pages[xiaomiplanet.article_url(3)] = article(3, note16_box_with_charger())
        h.tick(advance=300)
        assert "7108" not in h.watcher.carriers

    def test_republished_article_is_reread_unchanged_one_is_not(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.article_url(6)] = article(6, "<p>no boxes</p>")
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (6, "Xiaomi tyčový vysávač", "2026-10-20T10:00:00"),
            (4, "Xiaomi S800 váha", "2026-10-19T10:00:00"),
        )
        h.tick(advance=60)
        assert xiaomiplanet.article_url(4) not in h.session.get_calls
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (4, "Xiaomi S800 váha", "2026-10-21T09:00:00"),  # re-dated
            (6, "Xiaomi tyčový vysávač", "2026-10-20T10:00:00"),
        )
        h.tick(advance=60)
        assert xiaomiplanet.article_url(4) in h.session.get_calls
        # moved to the end, so evicting the oldest entries never drops a live one
        assert list(h.watcher.posts) == ["6", "4"]

    def test_republished_article_alerts_its_unchanged_offers(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (1, "REDMI Note 17 Pro s kupónom", "2026-10-21T11:59:00"),  # re-dated, same box
            (4, "Xiaomi S800 váha", "2026-10-19T10:00:00"),
        )
        h.tick(advance=60)
        assert len(h.sent) == 2
        subject, body = h.sent[1]
        assert subject == "REDMI Note 17 Pro – 389,00 € (DHGate)"
        assert "Miért most: újra kiírták (friss cikk az oldalon)" in body
        h.tick(advance=60)
        assert len(h.sent) == 2  # once per re-publication

    def test_edited_article_is_not_a_repost(self, tmp_path):
        h = Harness(tmp_path)
        title = "REDMI Note 17 Pro s kupónom"
        h.pages[xiaomiplanet.latest_url()] = post_list((1, title, "2026-10-20T10:00:00"))
        h.tick()
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (1, title, "2026-10-20T10:00:00", "2026-10-21T11:59:00")  # modified only
        )
        h.tick(advance=60)
        assert xiaomiplanet.article_url(1) in h.session.get_calls
        assert len(h.sent) == 1

    def test_older_post_moving_up_the_list_is_not_a_repost(self, tmp_path):
        # A newer post got deleted, so an old one slid into the latest ten
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (4, "Xiaomi S800 váha", "2026-10-19T10:00:00"),
            (1, "REDMI Note 17 Pro s kupónom", "2026-10-01T10:00:00"),
        )
        h.tick(advance=60)
        assert len(h.sent) == 1

    def test_new_article_with_a_known_box_is_a_repost(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.article_url(6)] = article(6, note16_box_with_charger())
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (6, "REDMI Note 16 opäť v akcii", "2026-10-21T11:59:00"),
            (4, "Xiaomi S800 váha", "2026-10-19T10:00:00"),
        )
        h.tick(advance=60)
        assert len(h.sent) == 2
        assert h.sent[1][0] == "🔥 REDMI Note 16 – 284,90 € (DHGate)"
        assert "újra kiírták" in h.sent[1][1]
        assert "Cikk: https://xiaomiplanet.sk/post-6/" in h.sent[1][1]

    def test_offer_reposted_in_two_articles_is_listed_once(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (1, "REDMI Note 17 Pro s kupónom", "2026-10-21T11:59:00"),
            (3, "Veľký prehľad: REDMI Note 17 Pro, Note 16", "2026-10-21T11:58:00"),
            (4, "Xiaomi S800 váha", "2026-10-19T10:00:00"),
        )
        h.tick(advance=60)
        assert len(h.sent) == 2
        subject, body = h.sent[1]
        assert subject.startswith("🔥 XiaomiPlanet: 2 ajánlat")  # Note 17 Pro + Note 16
        assert body.count("REDMI Note 17 Pro – ES sklad, 12 + 256 GB") == 1

    def test_failed_read_of_a_reposted_article_is_retried(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (1, "REDMI Note 17 Pro s kupónom", "2026-10-21T11:59:00"),
        )
        h.pages[xiaomiplanet.article_url(1)] = RuntimeError("timeout")
        try:
            h.tick(advance=60)
        except RuntimeError:
            pass
        assert len(h.sent) == 1
        h.set_note17_pro()
        h.tick(advance=60)
        assert len(h.sent) == 2
        assert "újra kiírták" in h.sent[1][1]

    def test_new_article_with_a_tracked_box_alerts_within_one_tick(self, tmp_path):
        h = Harness(tmp_path)
        h.tick()
        new_box = box(
            "5000",
            "REDMI Note 18 Globálna Verzia",
            [("1", "EÚ sklad, 12+256 GB", "ES sklad", panel("1", "299,00 €"))],
        )
        h.pages[xiaomiplanet.article_url(5)] = article(5, new_box)
        h.pages[xiaomiplanet.latest_url()] = post_list(
            (5, "REDMI Note 18 je tu", "2026-10-21T11:59:00"),
            (4, "Xiaomi S800 váha", "2026-10-19T10:00:00"),
        )
        h.tick(advance=60)
        assert len(h.sent) == 2
        assert h.sent[1][0] == "🔥 REDMI Note 18 – 299,00 € (DHGate)"

    def test_restart_with_saved_state_does_not_resend_the_summary(self, tmp_path):
        Harness(tmp_path).tick()
        state = DealState(str(tmp_path / "deal-state.json"))
        state.load()
        h = Harness(tmp_path, state=state)
        h.tick()
        assert h.sent == []

    def test_failed_first_tick_still_summarises_later(self, tmp_path):
        h = Harness(tmp_path)
        h.pages[xiaomiplanet.latest_url()] = RuntimeError("boom")
        try:
            h.tick()
        except RuntimeError:
            pass
        assert h.sent == [] and not h.watcher.initialized
        h.pages[xiaomiplanet.latest_url()] = post_list()
        h.tick(advance=60)
        assert len(h.sent) == 1 and h.sent[0][0] == "XiaomiPlanet: 2 db"


class TestXiaomiPlanetHealthProbe:
    def run(self, posts):
        session = _FakeSession({xiaomiplanet.health_url(): json.dumps(posts)})
        return asyncio.run(xiaomiplanet.health_probe(session))

    def test_boxes_with_prices_are_healthy(self):
        posts = [
            {"id": 1, "link": "l", "content": {"rendered": note17_pro_box()}},
            {"id": 2, "link": "l", "content": {"rendered": "<p>news</p>"}},
        ]
        result = self.run(posts)
        assert result == {"site": "XiaomiPlanet", "raw": 1, "matched": 2}
        assert not health_check.is_failure(result)

    def test_no_boxes_is_a_failure(self):
        result = self.run([{"id": 1, "link": "l", "content": {"rendered": "<p>x</p>"}}])
        assert health_check.is_failure(result)

    def test_boxes_without_readable_prices_is_a_failure(self):
        html = box("1", "REDMI Note 18 Pro", [("2", "x", "y", panel("2", None))])
        result = self.run([{"id": 1, "link": "l", "content": {"rendered": html}}])
        assert "error" in result


# --- config + monitor wiring -------------------------------------------------


class TestDealsConfig:
    def test_missing_section_disables_every_watcher(self):
        config = pm.MonitorConfig.from_dict({"searches": []})
        assert not config.deals.xiaomiplanet.enabled

    def test_section_is_parsed(self):
        config = pm.MonitorConfig.from_dict(
            {
                "searches": [],
                "deals": {
                    "xiaomiplanet": {
                        "enabled": True,
                        "search_terms": ["redmi note"],
                        "exclude_terms": ["13"],
                        "max_price_eur": 400,
                        "min_price_eur": 150,
                        "highlight_price_eur": 300,
                        "box_check_seconds": 240,
                    },
                },
            }
        )
        xp = config.deals.xiaomiplanet
        assert xp.enabled and xp.search_terms == ["redmi note"] and xp.exclude_terms == ["13"]
        assert (xp.max_price_eur, xp.min_price_eur, xp.highlight_price_eur) == (400, 150, 300)
        assert xp.article_check_seconds == 60

    def test_xiaomiplanet_without_search_terms_is_disabled(self):
        xp = XiaomiPlanetConfig.from_dict({"enabled": True, "search_terms": [" "]})
        assert not xp.enabled


class TestHealthCheckSelection:
    def monitor(self, deals):
        return SimpleNamespace(config=SimpleNamespace(deals=deals))

    def test_only_enabled_watchers_are_probed(self):
        deals = DealsConfig(xiaomiplanet=XiaomiPlanetConfig(enabled=True, search_terms=["x"]))
        sites = [c["site"] for c in health_check._deal_checks(self.monitor(deals))]
        assert sites == ["XiaomiPlanet"]
        assert health_check._deal_checks(self.monitor(DealsConfig())) == []

    def test_config_without_deals_probes_everything(self):
        monitor = SimpleNamespace(config=SimpleNamespace(max_concurrent_requests=4))
        assert health_check._deal_checks(monitor) == health_check.DEAL_CHECKS


class TestMonitorWiring:
    def bare_monitor(self, tmp_path, deals):
        m = object.__new__(pm.MultiMarketplaceMonitor)
        m.config = SimpleNamespace(deals=deals)
        m.session = None
        m.deal_state_file = str(tmp_path / "deal-state.json")
        m.xiaomiplanet_watcher = None
        m._email_tasks = set()
        return m

    def test_watchers_follow_the_config(self, tmp_path):
        m = self.bare_monitor(
            tmp_path,
            DealsConfig(xiaomiplanet=XiaomiPlanetConfig(enabled=True, search_terms=["redmi note"])),
        )
        m.create_deal_watchers()
        assert m.xiaomiplanet_watcher is not None

        m = self.bare_monitor(tmp_path, DealsConfig())
        m.create_deal_watchers()
        assert m.xiaomiplanet_watcher is None

    def test_a_failing_tick_does_not_escape_the_loop(self, tmp_path):
        m = self.bare_monitor(tmp_path, DealsConfig())
        m.shutdown = False

        async def failing_tick():
            m.shutdown = True  # stop after this one tick
            raise RuntimeError("site down")

        m.xiaomiplanet_watcher = SimpleNamespace(
            config=XiaomiPlanetConfig(article_check_seconds=60), tick=failing_tick
        )
        asyncio.run(m._xiaomiplanet_loop())  # must not raise
