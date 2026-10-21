import hashlib
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from .config import EMAIL_CONFIG


@dataclass
class SearchConfig:
    """Search configuration for a given site"""

    site: str
    search_terms: List[str]
    max_price: int
    url: Optional[str] = None
    # Optional lower price bound (the max_price counterpart); None = no lower bound.
    min_price: Optional[int] = None
    # Optional exclusion terms: a listing whose title contains any of these
    # (substring, same normalization as search_terms) is dropped. Empty = never excludes.
    exclude_terms: List[str] = field(default_factory=list)
    _search_id: Optional[str] = None

    def get_search_id(self) -> str:
        """Generate a unique identifier for the search (cached)

        Deliberately keyed only on site/url/terms/max_price, NOT on the newer
        min_price/exclude_terms fields: the search_id groups already-seen ads in
        seen-products.json, so changing what it hashes would orphan existing
        entries and re-notify everything. min_price/exclude_terms only tighten
        which listings pass the filter, so a stable id is the safe choice.
        """
        if self._search_id is None:
            search_str = f"{self.site}_{self.url if self.site == 'Moly' else '+'.join(self.search_terms)}_{self.max_price}"
            self._search_id = hashlib.md5(search_str.encode()).hexdigest()[:8]
        return self._search_id


@dataclass
class Advertisement:
    """Listing data"""

    id: str
    title: str
    price: int
    url: str
    site: str = ""
    search_id: str = ""
    first_seen: str = ""
    last_seen: str = ""

    def __hash__(self):
        return hash(self.id)


@dataclass
class XiaomiPlanetConfig:
    """xiaomiplanet.sk price-box watcher (see deals/xiaomiplanet.py).

    search_terms/exclude_terms filter on the price box's product title.
    search_terms are substrings (like every other search here); exclude_terms
    match whole words, so "13" excludes "Redmi Note 13" but not "Redmi Note 130".
    Prices are in EUR, compared against the coupon-applied price."""

    enabled: bool = False
    search_terms: List[str] = field(default_factory=list)
    exclude_terms: List[str] = field(default_factory=list)
    max_price_eur: float = 400.0
    # Lower bound, like the marketplace searches' min_price: phone boxes can
    # carry an accessory variant (a 21 € charger), which would otherwise alert
    # as a deal on the phone itself. None = no lower bound.
    min_price_eur: Optional[float] = None
    # Offers at or under this get a 🔥 in the email subject; None = never.
    highlight_price_eur: Optional[float] = None
    # The latest-articles list is tiny (~1.4 KB) - this is the fast path that
    # catches a new or re-published deal article.
    article_check_seconds: int = 60
    # Re-reading an article that carries a tracked price box costs the site a
    # full render, so it runs less often.
    box_check_seconds: int = 240

    @classmethod
    def from_dict(cls, data: dict) -> "XiaomiPlanetConfig":
        config = cls(
            enabled=data.get("enabled", False),
            search_terms=data.get("search_terms", []),
            exclude_terms=data.get("exclude_terms", []),
            max_price_eur=float(data.get("max_price_eur", 400)),
            min_price_eur=(
                float(data["min_price_eur"]) if data.get("min_price_eur") is not None else None
            ),
            highlight_price_eur=(
                float(data["highlight_price_eur"])
                if data.get("highlight_price_eur") is not None
                else None
            ),
            article_check_seconds=data.get("article_check_seconds", 60),
            box_check_seconds=data.get("box_check_seconds", 240),
        )
        if config.enabled and not any(t.strip() for t in config.search_terms):
            # Without a filter every price box on the site would be tracked,
            # and every article carrying one re-read every few minutes.
            logging.error("XiaomiPlanet: search_terms nélkül nem indítható, kikapcsolva.")
            config.enabled = False
        return config


@dataclass
class DealsConfig:
    """Optional "deals" section: new-retail deal/promo watchers, as opposed to
    the used-item marketplace searches. A missing section disables every watcher."""

    xiaomiplanet: XiaomiPlanetConfig = field(default_factory=XiaomiPlanetConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "DealsConfig":
        return cls(
            xiaomiplanet=XiaomiPlanetConfig.from_dict(data.get("xiaomiplanet", {})),
        )


@dataclass
class MonitorConfig:
    """Main configuration"""

    searches: List[SearchConfig]
    email: Dict[str, Any]
    check_interval: int
    cleanup_days: int
    max_concurrent_requests: int = 5
    request_timeout: int = 60
    log_level: str = "INFO"
    max_log_size_mb: int = 100
    # Daily live health check: run the smoke check in the first cycle at or
    # after this hour (local time), email only if a site is actually broken.
    health_check_enabled: bool = True
    health_check_hour: int = 16
    # Alert only after a site has failed this many consecutive daily checks
    # (each already retried). 2 = don't email on a one-off transient blip; 1 =
    # alert immediately (the old behaviour).
    health_check_min_consecutive: int = 2
    deals: DealsConfig = field(default_factory=DealsConfig)

    @classmethod
    def from_dict(cls, data: dict):
        searches = [
            SearchConfig(
                **{
                    "site": s["site"],
                    "search_terms": s.get("search_terms", []),
                    "max_price": s["max_price"],
                    "url": s.get("url"),
                    "min_price": s.get("min_price"),
                    "exclude_terms": s.get("exclude_terms", []),
                }
            )
            for s in data["searches"]
        ]

        return cls(
            searches=searches,
            email=EMAIL_CONFIG,
            check_interval=data.get("check_interval", 10800),
            cleanup_days=data.get("cleanup_days", 60),
            max_concurrent_requests=data.get("max_concurrent_requests", 5),
            request_timeout=data.get("request_timeout", 60),
            log_level=data.get("log_level", "INFO"),
            max_log_size_mb=data.get("max_log_size_mb", 100),
            health_check_enabled=data.get("health_check_enabled", True),
            health_check_hour=data.get("health_check_hour", 16),
            health_check_min_consecutive=data.get("health_check_min_consecutive", 2),
            deals=DealsConfig.from_dict(data.get("deals", {})),
        )
