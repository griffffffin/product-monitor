import logging
import re
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional

import aiohttp
import lru

from ..models import Advertisement, SearchConfig


class BaseScraper:
    """Base scraper class"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.logger = logging.getLogger(__name__)
        self.price_regex = re.compile(r"[^\d]")
        self.current_time = datetime.now().isoformat()

        # LRU cache for text normalization
        self.normalize_cache: "lru.LRU[str, str]" = lru.LRU(1000)

        # Pre-compiled search term cache
        self.search_terms_cache: Dict[tuple, List[str]] = {}

    def normalize_text(self, text: str) -> str:
        """Normalize text using the LRU cache"""
        if text in self.normalize_cache:
            return self.normalize_cache[text]

        normalized = unicodedata.normalize("NFD", text)
        result = "".join(c for c in normalized if unicodedata.category(c) != "Mn").lower()

        self.normalize_cache[text] = result
        return result

    def parse_price(self, price_text: str) -> int:
        """Price parsing"""
        try:
            # Clean up the string
            price_clean = self.price_regex.sub("", price_text.replace(" ", ""))
            return int(price_clean) if price_clean else 0
        except (ValueError, TypeError):
            return 0

    def matches_search_terms(self, title: str, search_terms: List[str]) -> bool:
        """Check the search criteria"""
        if not search_terms:
            return True

        # Cached search terms
        cache_key = tuple(search_terms)
        if cache_key not in self.search_terms_cache:
            self.search_terms_cache[cache_key] = [
                self.normalize_text(term) for term in search_terms if term
            ]

        normalized_terms = self.search_terms_cache[cache_key]
        title_normalized = self.normalize_text(title)

        return all(term in title_normalized for term in normalized_terms)

    def create_advertisement(
        self, ad_id: str, title: str, price: int, url: str, site: str, search_id: str
    ) -> Advertisement:
        """Shared Advertisement object creation"""
        return Advertisement(
            id=f"{site}_{ad_id}",
            title=title,
            price=price,
            url=url,
            site=site,
            search_id=search_id,
            first_seen=self.current_time,
            last_seen=self.current_time,
        )

    def extract_text_safe(
        self,
        element,
        selector: Optional[str] = None,
        attr: Optional[str] = None,
        default: str = "Ismeretlen",
    ) -> str:
        """Safe text extraction"""
        try:
            if selector:
                element = (
                    element.find(*selector.split(",", 1))
                    if "," in selector
                    else element.find(selector)
                )
            if not element:
                return default
            return element.get(attr) if attr else element.get_text(strip=True)
        except (AttributeError, TypeError):
            return default

    async def fetch_advertisements(self, search_config: SearchConfig) -> List[Advertisement]:
        """Async fetch of listings"""
        raise NotImplementedError
