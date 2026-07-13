import asyncio
import gc
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

import aiofiles
import aiohttp

from . import health_check
from .config import LOG_FILE
from .email_notifier import EmailNotifier
from .models import Advertisement, MonitorConfig
from .scrapers import BaseScraper, HardverAproScraper, JofogasScraper, MolyScraper, VateraScraper


class MultiMarketplaceMonitor:
    def __init__(self, config_path: str):
        self.config = self.load_config(config_path)
        self.setup_logging()
        self.email_notifier = EmailNotifier(self.config.email)

        # Set up the data directory
        self.data_dir = self._get_data_directory()
        self.seen_ads_file = os.path.join(self.data_dir, "seen-products.json")

        self.seen_ads: Dict[str, Advertisement] = {}
        self.seen_ads_set: Set[str] = set()

        # Memory monitoring
        self.last_memory_check = time.time()
        self.memory_check_interval = 10800  # 3 hours

        # Daily health check - the date it last ran, so it fires once a day
        self.last_health_check_date: Optional[date] = None

        # Async session
        self.session: Optional[aiohttp.ClientSession] = None
        self.scrapers: Dict[str, BaseScraper] = {}

    def _get_data_directory(self) -> str:
        """Determine the appropriate data directory"""
        # Service mode
        if os.getenv("INVOCATION_ID"):
            data_dir = "/opt/product-monitor"
        else:
            # Dev mode - the project root, not the product_monitor/ package
            # directory (__file__ here is monitor.py's own path, one level
            # deeper than when this was still product-monitor.py at the
            # project root)
            data_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Create the directory if it doesn't exist
        os.makedirs(data_dir, exist_ok=True)

        # Check write permissions
        if not os.access(data_dir, os.W_OK):
            # Fall back to /tmp
            data_dir = "/tmp/product-monitor"
            os.makedirs(data_dir, exist_ok=True)
            logging.warning(f"Data könyvtár nem írható, fallback használata: {data_dir}")

        logging.info(f"Data könyvtár: {data_dir}")
        return data_dir

    def load_config(self, config_path: str) -> MonitorConfig:
        """Load the configuration"""
        with open(config_path, "r", encoding="utf-8") as f:
            return MonitorConfig.from_dict(json.load(f))

    async def load_seen_ads(self) -> Dict[str, Advertisement]:
        """Async load of already-seen listings"""
        try:
            if not os.path.exists(self.seen_ads_file):
                # Look for a backup file
                backup_file = "/tmp/product-monitor-backup/seen-products.json"
                if os.path.exists(backup_file):
                    logging.info("Backup fájl találva, betöltés...")
                    self.seen_ads_file = backup_file
                else:
                    logging.info("Új adatbázis létrehozása")
                    return {}

            file_size = os.path.getsize(self.seen_ads_file)
            if file_size > 50 * 1024 * 1024:  # 50MB limit
                logging.warning(
                    f"Túl nagy seen_ads fájl ({file_size / 1024 / 1024:.1f}MB), újrakezdés"
                )
                backup_name = f"{self.seen_ads_file}.backup-{int(time.time())}"
                os.rename(self.seen_ads_file, backup_name)
                return {}

            async with aiofiles.open(self.seen_ads_file, "r", encoding="utf-8") as f:
                content = await f.read()
                data = json.loads(content)

                ads = {ad_id: Advertisement(**ad_data) for ad_id, ad_data in data.items()}
                self.seen_ads_set = set(ads.keys())
                logging.info(f"Betöltve {len(ads)} hirdetés.")
                return ads

        except (json.JSONDecodeError, OSError) as e:
            logging.warning(f"Hiba látott hirdetések betöltésekor: {e}")
            return {}
        except Exception as e:
            logging.error(f"Váratlan hiba betöltéskor: {e}")
            return {}

    async def save_seen_ads(self):
        """Save in batches"""
        try:
            # Only save if something actually changed
            if not hasattr(self, "_ads_changed") or not self._ads_changed:
                return

            # Check that the directory is writable
            if not os.access(self.data_dir, os.W_OK):
                logging.error(f"Data könyvtár nem írható: {self.data_dir}")
                return

            # Save in smaller batches
            batch_size = 1000
            temp_file = f"{self.seen_ads_file}.tmp"

            # Create the temp file safely
            try:
                async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
                    await f.write("{\n")

                    items = list(self.seen_ads.items())
                    for i in range(0, len(items), batch_size):
                        batch = items[i : i + batch_size]
                        batch_data = {}

                        for ad_id, ad in batch:
                            batch_data[ad_id] = asdict(ad)

                        batch_json = json.dumps(batch_data, ensure_ascii=False, indent=2)
                        # Remove the leading and trailing curly braces
                        batch_json = batch_json.strip()[1:-1]

                        await f.write(batch_json)
                        if i + batch_size < len(items):
                            await f.write(",\n")

                    await f.write("\n}")

                # Atomic file swap
                os.replace(temp_file, self.seen_ads_file)
                self._ads_changed = False
                logging.debug(f"Mentés sikeres: {len(self.seen_ads)} hirdetés")

            except PermissionError as e:
                logging.error(f"Jogosultsági hiba mentés közben: {e}")
                # Delete the temp file
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass
                # Try an alternative save location
                await self._try_backup_save()

        except Exception as e:
            logging.error(f"Hiba látott hirdetések mentésekor: {e}")
            if "temp_file" in locals() and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    async def _try_backup_save(self):
        """Backup save to /tmp"""
        try:
            backup_dir = "/tmp/product-monitor-backup"
            os.makedirs(backup_dir, exist_ok=True)

            backup_file = os.path.join(backup_dir, "seen-products.json")

            # Simple JSON save
            data = {ad_id: asdict(ad) for ad_id, ad in self.seen_ads.items()}

            async with aiofiles.open(backup_file, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))

            logging.info(f"Backup mentés sikeres: {backup_file}")

        except Exception as e:
            logging.error(f"Backup mentés is sikertelen: {e}")

    def cleanup_old_ads(self):
        """Delete old listings"""
        try:
            cutoff_date = datetime.now() - timedelta(days=self.config.cleanup_days)

            # Batch deletion
            ads_to_remove = []
            for ad_id, ad in self.seen_ads.items():
                if self._is_ad_old(ad, cutoff_date):
                    ads_to_remove.append(ad_id)

            # Batch removal from the dict and the set
            for ad_id in ads_to_remove:
                del self.seen_ads[ad_id]
                self.seen_ads_set.discard(ad_id)

            if ads_to_remove:
                logging.info(f"Törölve {len(ads_to_remove)} régi hirdetés.")

        except Exception as e:
            logging.error(f"Hiba régi hirdetések törlésekor: {e}")

    def _is_ad_old(self, ad: Advertisement, cutoff_date: datetime) -> bool:
        """Check the listing's age"""
        try:
            return datetime.fromisoformat(ad.last_seen) < cutoff_date
        except (ValueError, TypeError):
            return True

    def check_memory_usage(self):
        """Check memory usage"""
        current_time = time.time()
        if current_time - self.last_memory_check > self.memory_check_interval:
            try:
                import psutil

                process = psutil.Process()
                memory_mb = process.memory_info().rss / 1024 / 1024
                if memory_mb > 200:  # over 200MB
                    logging.warning(f"Magas memória használat: {memory_mb:.1f}MB")
                    gc.collect()

                self.last_memory_check = current_time
            except ImportError:
                pass  # psutil isn't installed

    async def create_session(self):
        """Create the async session"""
        connector = aiohttp.TCPConnector(
            limit=self.config.max_concurrent_requests,
            limit_per_host=2,
            ttl_dns_cache=300,
            use_dns_cache=True,
        )

        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)

        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux aarch64; rv:91.0) Gecko/20100101 Firefox/91.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "hu-HU,hu;q=0.8,en-US;q=0.5,en;q=0.3",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        self.scrapers = {
            "HardverApro": HardverAproScraper(self.session),
            "Jofogas": JofogasScraper(self.session),
            "Vatera": VateraScraper(self.session),
            "Moly": MolyScraper(self.session),
        }

    async def check_new_advertisements(self) -> List[Advertisement]:
        """Async check for new listings"""
        new_ads = []

        # Concurrent processing bounded by a semaphore
        semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)

        async def fetch_with_semaphore(search_config):
            async with semaphore:
                try:
                    scraper = self.scrapers.get(search_config.site)
                    if not scraper:
                        logging.warning(f"Ismeretlen oldal: {search_config.site}")
                        return []

                    return await scraper.fetch_advertisements(search_config)
                except Exception as e:
                    logging.error(f"Hiba {search_config.site} ellenőrzésekor: {e}")
                    return []

        # Run every search concurrently
        tasks = [fetch_with_semaphore(config) for config in self.config.searches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process the results
        for result in results:
            if isinstance(result, BaseException):
                logging.error(f"Hiba a keresés során: {result}")
                continue

            if result:
                site_new_ads = self._process_advertisements(result)
                if site_new_ads:
                    new_ads.extend(site_new_ads)

        if new_ads:
            await self.save_seen_ads()
            gc.collect()

        return new_ads

    def _process_advertisements(self, advertisements: List[Advertisement]) -> List[Advertisement]:
        """Process listings"""
        new_ads = []
        now = datetime.now().isoformat()
        changed = False

        for ad in advertisements:
            if ad.id not in self.seen_ads_set:
                # New listing
                new_ads.append(ad)
                self.seen_ads[ad.id] = ad
                self.seen_ads_set.add(ad.id)
                changed = True
            else:
                # Update an existing listing
                existing_ad = self.seen_ads[ad.id]
                if existing_ad.last_seen != now:
                    existing_ad.last_seen = now
                    changed = True

                if existing_ad.price != ad.price:
                    logging.info(
                        f"Árváltozás: {ad.title} - {f'{existing_ad.price:_}'.replace('_', ' ')} Ft -> {f'{ad.price:_}'.replace('_', ' ')} Ft"
                    )
                    existing_ad.price = ad.price
                    changed = True

        if changed:
            self._ads_changed = True

        return new_ads

    def setup_logging(self):
        from logging.handlers import RotatingFileHandler

        logger = logging.getLogger()
        logger.setLevel(getattr(logging, self.config.log_level.upper()))

        # Clear existing handlers to avoid duplicates
        logger.handlers.clear()

        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        # Use the service log file
        log_file = LOG_FILE if "LOG_FILE" in globals() else "product-monitor.log"

        if os.getenv("INVOCATION_ID"):
            # Service mode: stdout only, redirected by systemd
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        else:
            # Development mode: file + console
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=self.config.max_log_size_mb * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

    @staticmethod
    def build_notification(ads: List[Advertisement]) -> tuple[str, str]:
        """Build the email subject and body (doesn't send anything, pure text generation)"""
        # Group by site
        ads_by_site = defaultdict(list)
        for ad in ads:
            ads_by_site[ad.site].append(ad)

        subject = f"Hirdetések: {len(ads)} db"

        # StringBuilder pattern
        body_parts = ["-" * 148, "\n\n"]

        for site, site_ads in ads_by_site.items():
            body_parts.extend(
                ["=" * 14, " " * 4, site, f" ({len(site_ads)} db)", " " * 4, "=" * 14, "\n\n"]
            )

            for i, ad in enumerate(site_ads, 1):
                body_parts.extend(
                    [
                        f"{i}. {ad.title}\n",
                        f"   Ár: {f'{ad.price:_}'.replace('_', ' ')} Ft\n" f"   Link: {ad.url}\n\n",
                    ]
                )

            body_parts.extend(["-" * 148, "\n\n"])

        return subject, "".join(body_parts)

    def send_notification(self, ads: List[Advertisement]):
        """Email notification"""
        if not ads:
            return

        subject, body = self.build_notification(ads)

        # Async email send
        asyncio.create_task(self.email_notifier.send_notification(subject, body))
        logging.info(f"Email küldés {len(ads)} hirdetésről.")

    async def maybe_run_health_check(self):
        """Once a day, in the first cycle at or after health_check_hour, verify
        the scrapers still work against the live sites and email ONLY if one is
        actually broken (fetch error, or 0 raw cards = selector likely dead).

        A site with 0 matches but a healthy raw card count is silent on purpose:
        "nothing for sale right now" is the normal state, not an incident.

        This exists because a site redesign breaks a scraper *silently* - it
        just returns no listings forever, which is indistinguishable from a
        quiet market unless something actually checks the page structure (this
        is exactly how the Jofogas scraper stayed broken unnoticed, see
        CLAUDE.md).
        """
        if not self.config.health_check_enabled:
            return

        now = datetime.now()
        if now.hour < self.config.health_check_hour:
            return
        if self.last_health_check_date == now.date():
            return

        self.last_health_check_date = now.date()
        logging.info("Napi ellenőrzés indítása...")

        try:
            results = await health_check.run_health_check(self)
        except Exception as e:
            # Never let the health check take the monitoring cycle down with it
            logging.error(f"Napi ellenőrzés sikertelen: {e}", exc_info=True)
            return

        logging.info("Napi ellenőrzés eredménye:\n%s", health_check.format_report(results))

        failed = [r for r in results if health_check.is_failure(r)]
        if not failed:
            logging.info("Napi ellenőrzés: minden oldal rendben.")
            return

        sites = ", ".join(r["site"] for r in failed)
        logging.error(f"Napi ellenőrzés: hibás oldal(ak): {sites}.")
        subject, body = health_check.build_health_alert(results)
        asyncio.create_task(self.email_notifier.send_notification(subject, body))
        logging.info(f"Email küldés {len(failed)} hibás oldalról.")

    async def run(self):
        """Async continuous monitoring"""
        logging.info("Product Monitor indítása...")

        # Initialize the session and scrapers
        await self.create_session()

        # Load already-seen listings
        self.seen_ads = await self.load_seen_ads()
        self._ads_changed = False

        # Shutdown flag
        self.shutdown = False

        # Signal handlers
        def signal_handler(signum, frame):
            logging.info(f"Signal {signum} fogadva.")
            logging.info("Leállítás...")
            self.shutdown = True

        import signal

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            while not self.shutdown:
                try:
                    self.cleanup_old_ads()
                    self.check_memory_usage()

                    new_ads = await self.check_new_advertisements()
                    if new_ads:
                        self.send_notification(new_ads)
                    else:
                        logging.info("Nincs új hirdetés.")

                    await self.maybe_run_health_check()

                    logging.info("Sikeres ciklus.")
                    logging.info(f"Várakozás {self.config.check_interval / 3600:.0f} órát...")

                    # Interruptible sleep - check for shutdown every 5 seconds
                    for _ in range(0, self.config.check_interval, 5):
                        if self.shutdown:
                            break
                        await asyncio.sleep(min(5, self.config.check_interval))

                except asyncio.CancelledError:
                    logging.info("Async task megszakítva.")
                    logging.info("Kilépés...")
                    break
                except KeyboardInterrupt:
                    logging.info("Keyboard interrupt fogadva.")
                    logging.info("Kilépés...")
                    break
                except Exception as e:
                    logging.error(f"Hiba a figyelés során: {e}", exc_info=True)
                    # Shorter sleep on error, but still interruptible
                    for _ in range(0, 60, 5):
                        if self.shutdown:
                            break
                        await asyncio.sleep(5)

        except KeyboardInterrupt:
            logging.info("Keyboard interrupt.")
            logging.info("Kilépés...")
        finally:
            # Cleanup
            if hasattr(self, "_ads_changed") and self._ads_changed:
                try:
                    await self.save_seen_ads()
                except Exception as e:
                    logging.error(f"Hiba a mentés során: {e}")

            if self.session:
                try:
                    await self.session.close()
                except Exception as e:
                    logging.error(f"Hiba session lezárásakor: {e}")

            logging.info("Leállítva.")
