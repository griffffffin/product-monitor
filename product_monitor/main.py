"""Entry point: starts MultiMarketplaceMonitor and runs it until shutdown."""

import asyncio
import logging

from .monitor import MultiMarketplaceMonitor


async def _run() -> None:
    monitor = MultiMarketplaceMonitor("product-monitor-config.json")
    try:
        await monitor.run()
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt.")
    except Exception as e:
        logging.error(f"Váratlan hiba: {e}", exc_info=True)


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nProgram megszakítva.")
    except Exception as e:
        print(f"Kritikus hiba: {e}")
        logging.error(f"Kritikus hiba: {e}", exc_info=True)


if __name__ == "__main__":
    main()
