"""product_monitor: asyncio scraper for Hungarian secondhand marketplace listings."""

from .config import EMAIL_CONFIG, LOG_FILE
from .models import Advertisement, MonitorConfig, SearchConfig
from .monitor import MultiMarketplaceMonitor
from .scrapers import BaseScraper, HardverAproScraper, JofogasScraper, MolyScraper, VateraScraper

__version__ = "1.0.0"

__all__ = [
    "EMAIL_CONFIG",
    "LOG_FILE",
    "SearchConfig",
    "Advertisement",
    "MonitorConfig",
    "BaseScraper",
    "HardverAproScraper",
    "MolyScraper",
    "VateraScraper",
    "JofogasScraper",
    "MultiMarketplaceMonitor",
]
