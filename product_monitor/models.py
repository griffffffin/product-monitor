import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

from .config import EMAIL_CONFIG


@dataclass
class SearchConfig:
    """Search configuration for a given site"""

    site: str
    search_terms: List[str]
    max_price: int
    url: Optional[str] = None
    _search_id: Optional[str] = None

    def get_search_id(self) -> str:
        """Generate a unique identifier for the search (cached)"""
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

    @classmethod
    def from_dict(cls, data: dict):
        searches = [
            SearchConfig(
                **{
                    "site": s["site"],
                    "search_terms": s.get("search_terms", []),
                    "max_price": s["max_price"],
                    "url": s.get("url"),
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
        )
