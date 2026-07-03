from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Advertisement, SearchConfig
from .base import BaseScraper


class HardverAproScraper(BaseScraper):
    def build_search_url(self, search_config: SearchConfig) -> str:
        if search_config.url:
            return search_config.url
        search_term = "+".join(search_config.search_terms)
        return f"https://hardverapro.hu/aprok/keres.php?stext={search_term}&stprice={search_config.max_price}"

    def process_ad_item(self, item, search_config: SearchConfig) -> Optional[Advertisement]:
        """Listing processing"""
        try:
            ad_id = item.get("data-uadid")
            if not ad_id:
                return None

            title_element = item.find("h1")
            title_link = title_element.find("a") if title_element else None
            if not title_link:
                return None

            title = title_link.get_text(strip=True)
            if not self.matches_search_terms(title, search_config.search_terms):
                return None

            price_element = item.find("div", class_="uad-price")
            price_span = price_element.find("span", class_="text-nowrap") if price_element else None
            if not price_span:
                return None

            price = self.parse_price(price_span.get_text(strip=True))
            if price <= 0 or price > search_config.max_price:
                return None

            url = urljoin("https://hardverapro.hu", title_link.get("href"))

            return self.create_advertisement(
                ad_id, title, price, url, "HardverApro", search_config.get_search_id()
            )

        except Exception as e:
            self.logger.debug(f"Hiba HardverApro hirdetés feldolgozásakor: {e}")
            return None

    async def fetch_advertisements(self, search_config: SearchConfig) -> List[Advertisement]:
        """Async fetch of listings"""
        try:
            async with self.session.get(
                self.build_search_url(search_config), timeout=30
            ) as response:
                response.raise_for_status()
                html = await response.text(encoding="utf-8")

            soup = BeautifulSoup(html, "html.parser")
            ad_items = soup.find_all("li", class_="media")

            # Process listings
            advertisements = []
            for item in ad_items:
                ad = self.process_ad_item(item, search_config)
                if ad:
                    advertisements.append(ad)
                    self.logger.info(
                        f"HardverApro: {ad.title} - {f'{ad.price:_}'.replace('_', ' ')} Ft"
                    )

            return advertisements

        except Exception as e:
            self.logger.error(f"Hiba HardverApro hirdetések lekérésekor: {e}")
            return []
