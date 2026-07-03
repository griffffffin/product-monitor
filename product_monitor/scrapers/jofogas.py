import json
from typing import List, Optional

from bs4 import BeautifulSoup

from ..models import Advertisement, SearchConfig
from .base import BaseScraper


class JofogasScraper(BaseScraper):
    """2026-07-03: jofogas.hu moved to a Next.js/React frontend - the old
    static markup (div.general-item, id="listid_*") no longer exists in the
    page at all (replaced by hashed Emotion/MUI css-* classes), which made
    this scraper silently return 0 results (no exception, filtering just
    saw an empty page). The listing data is server-rendered into a
    __NEXT_DATA__ JSON blob instead (props.pageProps.adList.ads), so this
    parses that JSON directly rather than scraping the DOM."""

    def build_search_url(self, search_config: SearchConfig) -> str:
        search_term = "+".join(search_config.search_terms)
        return f"https://www.jofogas.hu/magyarorszag?q={search_term}"

    def process_ad_item(self, item: dict, search_config: SearchConfig) -> Optional[Advertisement]:
        """item is one entry from the __NEXT_DATA__ JSON props.pageProps.adList.ads list"""
        try:
            ad_id = item.get("list_id")
            if not ad_id:
                return None

            title = (item.get("subject") or "").strip()
            if not title:
                return None
            if not self.matches_search_terms(title, search_config.search_terms):
                return None

            price = (item.get("price") or {}).get("value")
            if not isinstance(price, (int, float)) or price <= 0 or price > search_config.max_price:
                return None

            url = item.get("url")
            if not url:
                return None

            return self.create_advertisement(
                str(ad_id), title, int(price), url, "Jofogas", search_config.get_search_id()
            )

        except Exception as e:
            self.logger.debug(f"Hiba Jófogás hirdetés feldolgozásakor: {e}")
            return None

    async def fetch_advertisements(self, search_config: SearchConfig) -> List[Advertisement]:
        try:
            async with self.session.get(
                self.build_search_url(search_config), timeout=30
            ) as response:
                response.raise_for_status()
                html = await response.text()

            soup = BeautifulSoup(html, "html.parser")
            # find() on a tag selector realistically always returns a Tag (or
            # None), never a bare NavigableString - bs4's types can't express that.
            next_data_script = soup.find("script", id="__NEXT_DATA__")
            if not next_data_script or not next_data_script.string:  # type: ignore[union-attr]
                self.logger.error(
                    "Hiba Jófogás hirdetések lekérésekor: __NEXT_DATA__ script nem található"
                )
                return []

            data = json.loads(next_data_script.string)  # type: ignore[union-attr]
            ad_items = data.get("props", {}).get("pageProps", {}).get("adList", {}).get("ads", [])

            advertisements = []
            for item in ad_items:
                ad = self.process_ad_item(item, search_config)
                if ad:
                    advertisements.append(ad)
                    self.logger.info(
                        f"Jofogas: {ad.title} - {f'{ad.price:_}'.replace('_', ' ')} Ft"
                    )

            return advertisements

        except Exception as e:
            self.logger.error(f"Hiba Jófogás hirdetések lekérésekor: {e}")
            return []
