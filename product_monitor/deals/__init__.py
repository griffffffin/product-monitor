"""Deal watchers: new-retail price/promo sources, as opposed to the used-item
marketplace scrapers in product_monitor.scrapers.

They differ from the scrapers in what they track (a price box or a promotion
page, not a listing), in currency (EUR), in when they alert (on price and
page changes, not just on first sight) and in cadence (XiaomiPlanet runs on its own
fast loop), so they keep their own state file (deal-state.json) instead of
going through seen-products.json.
"""

from .state import DealState
from .xiaomiplanet import XiaomiPlanetWatcher

__all__ = ["DealState", "XiaomiPlanetWatcher"]
