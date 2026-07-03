from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Advertisement, SearchConfig
from .base import BaseScraper


class MolyScraper(BaseScraper):
    def process_ad_item(self, item, search_config: SearchConfig) -> Optional[Advertisement]:
        try:
            item_id = item.get("id")
            if not item_id or not item_id.startswith("copy_"):
                return None

            ad_id = item_id.replace("copy_", "")

            right_div = item.find("div", class_="right")
            if not right_div:
                return None

            price_text = right_div.text.strip()
            if "Eladó" not in price_text or price_text.count("★") != 5:
                return None

            price = self.parse_price(price_text)
            if price <= 0 or price > search_config.max_price:
                return None

            book_link = item.find("a", class_="book_selector")
            if not book_link:
                return None

            title = book_link.text.strip()

            example_link = item.find("a", class_="button_icon")
            url = (
                urljoin("https://moly.hu", example_link.get("href"))
                if example_link
                else search_config.url
            )
            if not url:
                return None

            return self.create_advertisement(
                ad_id, title, price, url, "Moly", search_config.get_search_id()
            )

        except Exception as e:
            self.logger.debug(f"Hiba Moly hirdetés feldolgozásakor: {e}")
            return None

    async def fetch_advertisements(self, search_config: SearchConfig) -> List[Advertisement]:
        try:
            if not search_config.url:
                self.logger.error("Moly.hu esetén URL megadása kötelező!")
                return []

            async with self.session.get(search_config.url, timeout=30) as response:
                response.raise_for_status()
                html = await response.text(encoding="utf-8")

            soup = BeautifulSoup(html, "html.parser")
            copy_items = soup.find_all("div", class_="copy")

            advertisements = []
            for item in copy_items:
                ad = self.process_ad_item(item, search_config)
                if ad:
                    advertisements.append(ad)
                    self.logger.info(f"Moly: {ad.title} - {f'{ad.price:_}'.replace('_', ' ')} Ft")

            return advertisements

        except Exception as e:
            self.logger.error(f"Hiba Moly hirdetések lekérésekor: {e}")
            return []
