from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Advertisement, SearchConfig
from .base import BaseScraper


class VateraScraper(BaseScraper):
    def build_search_url(self, search_config: SearchConfig) -> str:
        search_term = "+".join(search_config.search_terms)
        return f"https://www.vatera.hu/listings/?q={search_term}&sort=end_time"

    def process_ad_item(self, item, search_config: SearchConfig) -> Optional[Advertisement]:
        try:
            ad_id = item.get("data-product-id")
            if not ad_id:
                return None

            title_link = item.find("a", class_="product_link")
            title_element = title_link.find("h3") if title_link else None
            if not title_element:
                return None

            title = title_element.text.strip()
            if not self.matches_search_terms(title, search_config.search_terms):
                return None

            price_element = item.find("span", class_="originalVal")
            if not price_element:
                return None

            price = self.parse_price(price_element.text.strip())
            if price <= 0 or price > search_config.max_price:
                return None

            url = title_link.get("href")
            if not url.startswith("http"):
                url = urljoin("https://www.vatera.hu", url)

            return self.create_advertisement(
                ad_id, title, price, url, "Vatera", search_config.get_search_id()
            )

        except Exception as e:
            self.logger.debug(f"Hiba Vatera hirdetés feldolgozásakor: {e}")
            return None

    async def fetch_advertisements(self, search_config: SearchConfig) -> List[Advertisement]:
        try:
            async with self.session.get(
                self.build_search_url(search_config), timeout=30
            ) as response:
                response.raise_for_status()
                html = await response.text(encoding="utf-8")

            soup = BeautifulSoup(html, "html.parser")
            ad_items = soup.find_all("div", class_="gtm-impression")

            advertisements = []
            for item in ad_items:
                ad = self.process_ad_item(item, search_config)
                if ad:
                    advertisements.append(ad)
                    self.logger.info(f"Vatera: {ad.title} - {f'{ad.price:_}'.replace('_', ' ')} Ft")

            return advertisements

        except Exception as e:
            self.logger.error(f"Hiba Vatera hirdetések lekérésekor: {e}")
            return []
