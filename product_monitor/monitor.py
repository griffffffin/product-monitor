import asyncio
import gc
import json
import logging
import os
import signal
import socket
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

import aiofiles
import aiohttp

from . import health_check
from .config import LOG_FILE, is_service_mode
from .deals import AsusPromoWatcher, DealState, XiaomiPlanetWatcher
from .email_notifier import EmailNotifier
from .models import Advertisement, MonitorConfig
from .scrapers import (
    BaseScraper,
    HardverAproScraper,
    JofogasScraper,
    MolyScraper,
    VateraScraper,
    VintedScraper,
)


class MultiMarketplaceMonitor:
    # How long the cleanup after a shutdown signal waits for the step that
    # records a finished scrape, and then for emails already handed over.
    # systemd sends SIGKILL 60 s after SIGTERM (TimeoutStopSec=60), and the
    # host installer's stop test allows 30 s; normally both take well under 1 s.
    SHUTDOWN_WAIT_SECONDS = 10

    def __init__(self, config: MonitorConfig, data_dir: str):
        """config and data_dir come from main.preflight(), whose startup gates
        have already checked them (data_dir exists and is writable). Nothing
        here touches the filesystem; main() calls setup_logging() itself."""
        self.config = config
        self.email_notifier = EmailNotifier(self.config.email)

        # The state directory - every file this process writes lives here
        self.data_dir = data_dir
        self.seen_ads_file = os.path.join(self.data_dir, "seen-products.json")

        self.seen_ads: Dict[str, Advertisement] = {}
        self.seen_ads_set: Set[str] = set()

        # Memory monitoring
        self.last_memory_check = time.time()
        self.memory_check_interval = 10800  # 3 hours

        # Daily health check - the date it last ran, so it fires once a day
        self.last_health_check_date: Optional[date] = None
        # Persisted across restarts: which sites failed the previous daily check,
        # so we can require N consecutive failures before alerting (see
        # maybe_run_health_check / health_check_min_consecutive).
        self.health_state_file = os.path.join(self.data_dir, "health-check-state.json")

        # Deal watchers (config "deals" section) - created in run()
        self.deal_state_file = os.path.join(self.data_dir, "deal-state.json")
        self.xiaomiplanet_watcher: Optional[XiaomiPlanetWatcher] = None
        self.asuspromo_watcher: Optional[AsusPromoWatcher] = None
        # References to in-flight email tasks, so they can't be garbage-collected
        # before the send finishes
        self._email_tasks: Set[asyncio.Task] = set()
        # The step of check_new_advertisements() that records a finished scrape
        # (runs shielded from a shutdown; run()'s cleanup waits for it)
        self._record_task: Optional[asyncio.Task] = None
        # The running cycle loop, which a shutdown signal cancels
        self._cycle_task: Optional[asyncio.Task] = None

        # Async session
        self.session: Optional[aiohttp.ClientSession] = None
        self.scrapers: Dict[str, BaseScraper] = {}

    async def load_seen_ads(self) -> Dict[str, Advertisement]:
        """Async load of already-seen listings"""
        try:
            if not os.path.exists(self.seen_ads_file):
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
                # No alternative location on purpose: an unwritable state
                # directory is a configuration error, caught by the startup gate
                # (config.state_directory). Mid-run, _ads_changed stays set, so
                # the next cycle retries the save.
                logging.error(f"Jogosultsági hiba mentés közben: {e}")
                # Delete the temp file
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass

        except Exception as e:
            logging.error(f"Hiba látott hirdetések mentésekor: {e}")
            if "temp_file" in locals() and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

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
        """Warn (and collect garbage) above 200 MB, at most once per interval.

        Optional diagnostics, so it must never cost a monitoring cycle: it runs
        before the scrape, and an exception escaping from here would skip the
        whole cycle - and, with the timestamp not advanced, every retry after
        it. psutil may be missing, and the sandbox may hide what it reads
        (it already hides /proc/stat; Process().memory_info() only
        needs /proc/self), so any failure is logged and the next try waits a
        full interval.
        """
        current_time = time.time()
        if current_time - self.last_memory_check <= self.memory_check_interval:
            return
        self.last_memory_check = current_time
        try:
            import psutil
        except ImportError:
            return  # psutil isn't installed
        try:
            memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
        except Exception as e:
            logging.warning(f"Memóriahasználat nem mérhető: {type(e).__name__}: {e}")
            return
        if memory_mb > 200:  # over 200MB
            logging.warning(f"Magas memória használat: {memory_mb:.1f}MB")
            gc.collect()

    async def create_session(self):
        """Create the async session"""
        # IPv4 only: the Pi has no IPv6 route (just a link-local address).
        # aiohttp's default resolver there is c-ares (aiodns), which asks for A
        # and AAAA together and, when the A answer comes late, returns only the
        # IPv6 addresses - the connect then fails at once with "Network is
        # unreachable", and ttl_dns_cache below keeps serving that answer for
        # 5 minutes: every request to the host fails until it expires (seen
        # 2026-10-24..31: a XiaomiPlanet failure like this lasted 6 of its 1-minute
        # ticks in 53 of 59 cases, and all 8 Moly/Vinted/Vatera searches failed together).
        # With AF_INET only A records are asked, and a late answer ends in a DNS
        # error, which isn't cached, so the next request asks again.
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
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
            "Vinted": VintedScraper(self.session),
        }

    async def check_new_advertisements(self) -> List[Advertisement]:
        """Run every search, then record the results: new listings are marked
        seen, saved and emailed.

        A shutdown can interrupt the searches (they are only network requests;
        nothing found so far counts as seen, so the next start finds it again),
        but not the recording that follows: interrupted after the save and
        before the email, a listing would count as seen without ever having
        been emailed, and a half-done save would race the final save in run()'s
        cleanup on the same .tmp file. So the recording runs as its own task,
        shielded, and the cleanup waits for it (self._record_task).
        """
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

        self._record_task = asyncio.ensure_future(self._record_results(results))
        return await asyncio.shield(self._record_task)

    async def _record_results(self, results: list) -> List[Advertisement]:
        """The searches' results -> seen, saved, emailed. See check_new_advertisements."""
        new_ads: List[Advertisement] = []
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
            self.send_notification(new_ads)
        else:
            logging.info("Nincs új hirdetés.")

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

        if is_service_mode():
            # Service mode: stdout only - systemd appends it to the service log
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        else:
            # Development mode: file + console
            file_handler = RotatingFileHandler(
                LOG_FILE,
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

        # Async email send (tracked, so a shutdown waits for it)
        self._send_email(subject, body)
        logging.info(f"Email küldés {len(ads)} hirdetésről.")

    def _send_email(self, subject: str, body: str) -> None:
        task = asyncio.create_task(self.email_notifier.send_notification(subject, body))
        self._email_tasks.add(task)
        task.add_done_callback(self._email_tasks.discard)

    def create_deal_watchers(self) -> None:
        """Instantiate the watchers the config enables (needs the session)."""
        deals = self.config.deals
        if not (deals.xiaomiplanet.enabled or deals.asuspromo.enabled):
            return
        state = DealState(self.deal_state_file)
        state.load()
        if deals.xiaomiplanet.enabled:
            self.xiaomiplanet_watcher = XiaomiPlanetWatcher(
                self.session, deals.xiaomiplanet, state, self._send_email
            )
        if deals.asuspromo.enabled:
            self.asuspromo_watcher = AsusPromoWatcher(self.session, state, self._send_email)

    async def check_asuspromo(self) -> None:
        """Runs in the regular cycle - the site's promotions last weeks."""
        if not self.asuspromo_watcher:
            return
        try:
            await self.asuspromo_watcher.check()
        except Exception as e:
            logging.error(f"AsusPromo ellenőrzés sikertelen: {health_check._format_error(e)}")

    async def _xiaomiplanet_loop(self) -> None:
        """XiaomiPlanet's own fast loop, independent of the 3-hourly cycle:
        coupon stock can sell out within ~15 minutes of being published. A
        failing tick is logged and retried next interval; the daily health
        check is what emails if the site stays broken."""
        watcher = self.xiaomiplanet_watcher
        if watcher is None:
            return
        interval = max(15, watcher.config.article_check_seconds)
        failures = 0
        logging.info(f"XiaomiPlanet figyelés indítása ({interval} mp-enként).")
        while not self.shutdown:
            deadline = time.monotonic() + interval
            try:
                await watcher.tick()
                if failures:
                    logging.info(f"XiaomiPlanet: újra elérhető ({failures} sikertelen kör után).")
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                # First failure and then hourly-ish, not every minute of an outage
                if failures == 1 or failures % 60 == 0:
                    logging.warning(
                        f"XiaomiPlanet ellenőrzés sikertelen ({failures}. egymás után): "
                        f"{health_check._format_error(e)}"
                    )
            while not self.shutdown and time.monotonic() < deadline:
                await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

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

        # Alert only after a site fails N consecutive daily checks (each already
        # retried inside check_site). This kills the transient-blip false alarms
        # the production emails were - a one-off "broken pipe"/timeout no longer
        # emails; a genuine break, which persists, still does (a day later).
        now_failed = health_check.failing_sites(results)
        prev_counts = self._load_health_failures()
        new_counts = {site: prev_counts.get(site, 0) + 1 for site in now_failed}
        self._save_health_failures(new_counts)

        if not now_failed:
            logging.info("Napi ellenőrzés: minden oldal rendben.")
            return

        min_consecutive = max(1, getattr(self.config, "health_check_min_consecutive", 2))
        confirmed = sorted(s for s in now_failed if new_counts[s] >= min_consecutive)
        not_yet = sorted(s for s in now_failed if new_counts[s] < min_consecutive)

        if not_yet:
            logging.warning(
                "Napi ellenőrzés: hibás, de még nem éri el a %d egymást követő "
                "küszöböt (nem riasztok): %s.",
                min_consecutive,
                ", ".join(not_yet),
            )

        if not confirmed:
            return

        logging.error(f"Napi ellenőrzés: tartósan hibás oldal(ak): {', '.join(confirmed)}.")
        subject, body = health_check.build_health_alert(results, confirmed)
        self._send_email(subject, body)
        logging.info(f"Email küldés {len(confirmed)} tartósan hibás oldalról.")

    def _load_health_failures(self) -> Dict[str, int]:
        """Per-site consecutive-failure counts from the last daily check. Any
        problem (missing/corrupt file) -> empty, which just means 'no history',
        so at worst a real break is alerted one day later, never a false alarm."""
        if not getattr(self, "health_state_file", None):
            return {}
        try:
            with open(self.health_state_file, encoding="utf-8") as f:
                data = json.load(f)
            counts = data.get("consecutive_failures", {})
            return {str(k): int(v) for k, v in counts.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_health_failures(self, counts: Dict[str, int]) -> None:
        """Persist the per-site consecutive-failure counts (only currently
        failing sites; a recovered site is simply absent -> its count resets)."""
        if not getattr(self, "health_state_file", None):
            return
        try:
            with open(self.health_state_file, "w", encoding="utf-8") as f:
                json.dump({"consecutive_failures": counts}, f)
        except OSError as e:
            logging.warning(f"Health-check állapot mentése sikertelen: {e}")

    async def run(self):
        """Continuous monitoring until SIGTERM/SIGINT.

        A signal cancels the cycle wherever it is - a scraper's HTTP request, the
        daily check, the wait between cycles - not just between cycles: systemd
        sends SIGKILL 60 s after SIGTERM, and a SIGKILL skips the cleanup below
        (the final save among them). Only the cycle task is cancelled, not run()
        itself, so the cleanup awaits normally.
        """
        logging.info("Product Monitor indítása...")
        logging.info(f"Állapot-könyvtár: {self.data_dir}")

        self.shutdown = False
        self._cycle_task = None
        deal_task: Optional[asyncio.Task] = None
        loop = asyncio.get_running_loop()

        def on_signal(signum: int) -> None:
            logging.info(f"Signal {signum} fogadva.")
            logging.info("Leállítás...")
            self.shutdown = True
            if self._cycle_task is not None:
                self._cycle_task.cancel()

        signals = (signal.SIGTERM, signal.SIGINT)
        for signum in signals:
            loop.add_signal_handler(signum, on_signal, int(signum))
        try:
            try:
                await self.create_session()
                self.seen_ads = await self.load_seen_ads()
                self._ads_changed = False

                self.create_deal_watchers()
                if self.xiaomiplanet_watcher:
                    deal_task = asyncio.create_task(self._xiaomiplanet_loop())

                if not self.shutdown:
                    self._cycle_task = asyncio.create_task(self._cycle_loop())
                    try:
                        await self._cycle_task
                    except asyncio.CancelledError:
                        if not self.shutdown:
                            raise  # cancelled from outside, not by a signal
                        logging.info("Kör megszakítva a leállítás miatt.")
            finally:
                await self._shutdown_cleanup(deal_task)
        finally:
            # Only now: a second signal during the cleanup is just logged
            for signum in signals:
                loop.remove_signal_handler(signum)

    async def _cycle_loop(self) -> None:
        """The regular cycle, forever - run() cancels it on a shutdown signal."""
        while True:
            try:
                self.cleanup_old_ads()
                self.check_memory_usage()

                await self.check_new_advertisements()

                await self.check_asuspromo()

                await self.maybe_run_health_check()

                logging.info("Sikeres ciklus.")
                logging.info(f"Várakozás {self.config.check_interval / 3600:.0f} órát...")
                await asyncio.sleep(self.config.check_interval)

            except Exception as e:
                logging.error(f"Hiba a figyelés során: {e}", exc_info=True)
                await asyncio.sleep(60)  # shorter wait after an error

    async def _shutdown_cleanup(self, deal_task: Optional[asyncio.Task]) -> None:
        if deal_task:
            deal_task.cancel()
            try:
                await deal_task
            except (asyncio.CancelledError, Exception):
                pass

        # A finished scrape's recording still running (shielded, see
        # check_new_advertisements): let it end before the final save, which
        # would otherwise write the same .tmp file at the same time.
        record = self._record_task
        if record is not None and not record.done():
            logging.info("Leállítás: a megkezdett mentés befejezése...")
            try:
                await asyncio.wait_for(asyncio.shield(record), self.SHUTDOWN_WAIT_SECONDS)
            except Exception as e:
                logging.error(f"Hiba a megkezdett mentés befejezésekor: {e!r}")

        if getattr(self, "_ads_changed", False) and (record is None or record.done()):
            try:
                await self.save_seen_ads()
            except Exception as e:
                logging.error(f"Hiba a mentés során: {e}")

        # Emails already handed over (their listings already count as seen):
        # give them a bounded chance to go out before the loop stops.
        pending = [task for task in self._email_tasks if not task.done()]
        if pending:
            logging.info(f"Leállítás: várakozás {len(pending)} levél elküldésére...")
            _, not_sent = await asyncio.wait(pending, timeout=self.SHUTDOWN_WAIT_SECONDS)
            if not_sent:
                logging.error(f"Leállítás: {len(not_sent)} levél nem ment el időben.")

        if self.session:
            try:
                await self.session.close()
            except Exception as e:
                logging.error(f"Hiba session lezárásakor: {e}")

        logging.info("Leállítva.")
