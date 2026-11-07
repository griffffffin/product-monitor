"""Shutdown: SIGTERM stops the monitor within seconds, in the middle of a cycle too.

systemd sends SIGKILL 60 s after SIGTERM (TimeoutStopSec=60), and a SIGKILL
skips run()'s cleanup, the final save among them. The old run() only looked at
its shutdown flag in the sleep between cycles, so a stop during the scrape or
the daily check waited for the cycle to end, or for the SIGKILL.

These tests send a real SIGTERM to this process while a fake cycle hangs in a
real HTTP request (to a local server that never answers), and measure how long
run() takes to return after the signal. No outside network, no real email.
"""

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import asdict
from datetime import datetime, timedelta

import pytest

import product_monitor as pm
from product_monitor import email_notifier as email_module

STOP_LIMIT = 3.0  # seconds from the signal to run() returning
GUARD = 8.0  # stop waiting for run() altogether (the old code never returned in time)
STOP_TIMES = {}


@pytest.fixture(autouse=True)
def sigterm_guard():
    """If the code under test installed no handler, a SIGTERM would kill pytest."""
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class StallingServer:
    """Accepts HTTP requests and never answers: a request only cancellation ends."""

    async def __aenter__(self):
        self.handlers = set()
        self.writers = []
        self.requests = 0
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/"
        return self

    async def _handle(self, reader, writer):
        self.handlers.add(asyncio.current_task())
        self.writers.append(writer)
        await reader.readuntil(b"\r\n\r\n")
        self.requests += 1
        await asyncio.Event().wait()

    async def __aexit__(self, *exc):
        for writer in self.writers:
            writer.close()
        for task in self.handlers:
            task.cancel()
        self.server.close()
        await asyncio.wait_for(self.server.wait_closed(), 2)


class FakeNotifier:
    def __init__(self, delay=0.0, hang=False):
        self.sent = []
        self.delay = delay
        self.hang = hang

    async def send_notification(self, subject, body):
        if self.hang:
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay)
        self.sent.append(subject)
        return True


def make_ad(ad_id, last_seen):
    return pm.Advertisement(
        id=ad_id,
        title=f"Tétel {ad_id}",
        price=500,
        url=f"https://example.com/{ad_id}",
        site="Fake",
        search_id="s1",
        first_seen=last_seen,
        last_seen=last_seen,
    )


KNOWN = "Fake_known"
NEW = "Fake_new"
YESTERDAY = (datetime.now() - timedelta(days=1)).isoformat()


class FakeScraper:
    """search_terms[0] picks the behaviour: "stall", "known" or "new"."""

    def __init__(self, session, stall_url):
        self.session = session
        self.stall_url = stall_url

    async def fetch_advertisements(self, search_config):
        mode = search_config.search_terms[0]
        if mode == "stall":
            async with self.session.get(self.stall_url) as response:
                await response.read()
            return []
        ad_id = KNOWN if mode == "known" else NEW
        return [make_ad(ad_id, datetime.now().isoformat())]


def make_monitor(tmp_path, server, modes, notifier=None):
    config = pm.MonitorConfig.from_dict(
        {
            "searches": [{"site": "Fake", "search_terms": [m], "max_price": 1000} for m in modes],
            "health_check_enabled": False,
        }
    )
    (tmp_path / "seen-products.json").write_text(
        json.dumps({KNOWN: asdict(make_ad(KNOWN, YESTERDAY))}), encoding="utf-8"
    )
    m = pm.MultiMarketplaceMonitor(config, str(tmp_path))
    m.email_notifier = notifier or FakeNotifier()

    real_create_session = m.create_session

    async def create_session():
        await real_create_session()
        m.scrapers["Fake"] = FakeScraper(m.session, server.url)

    m.create_session = create_session
    return m


def saved(tmp_path):
    return json.loads((tmp_path / "seen-products.json").read_text(encoding="utf-8"))


async def run_and_signal(m, after, name):
    """run(), with a real SIGTERM `after` seconds in; seconds from signal to return."""
    loop = asyncio.get_running_loop()
    sent = {}

    def fire():
        sent["at"] = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)

    loop.call_later(after, fire)
    await asyncio.wait_for(m.run(), GUARD)
    elapsed = time.monotonic() - sent["at"]
    STOP_TIMES[name] = elapsed
    print(f"\n[stop] {name}: {elapsed * 1000:.0f} ms after SIGTERM")
    return elapsed


class TestShutdownMidCycle:
    def test_during_a_hanging_scraper_request(self, tmp_path, caplog):
        async def scenario():
            async with StallingServer() as server:
                m = make_monitor(tmp_path, server, ["stall"])
                elapsed = await run_and_signal(m, 0.5, "hanging scraper request")
                # run() itself gives the process the default SIGTERM behaviour
                # back (checked while the loop still runs: closing it would
                # remove the handlers anyway)
                restored = signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
                return m, server, elapsed, restored

        with caplog.at_level(logging.INFO):
            m, server, elapsed, restored = asyncio.run(scenario())
        assert server.requests == 1  # the request really was in flight
        assert elapsed < STOP_LIMIT
        assert "Kör megszakítva a leállítás miatt." in caplog.text
        assert caplog.text.rstrip().endswith("Leállítva.")
        assert m.session.closed
        # An interrupted search records nothing: no email, the state as it was
        assert m.email_notifier.sent == []
        assert saved(tmp_path)[KNOWN]["last_seen"] == YESTERDAY
        assert restored

    def test_during_the_daily_check_and_the_pending_change_is_saved(self, tmp_path, caplog):
        async def scenario():
            async with StallingServer() as server:
                m = make_monitor(tmp_path, server, ["known"])

                async def hanging_daily_check():
                    async with m.session.get(server.url) as response:
                        await response.read()

                m.maybe_run_health_check = hanging_daily_check
                elapsed = await run_and_signal(m, 0.5, "hanging daily check")
                return server, elapsed

        with caplog.at_level(logging.INFO):
            server, elapsed = asyncio.run(scenario())
        assert server.requests == 1
        assert elapsed < STOP_LIMIT
        # The scrape updated last_seen without a save of its own (no new
        # listing): only the final save in the cleanup persists it.
        assert saved(tmp_path)[KNOWN]["last_seen"] != YESTERDAY
        assert caplog.text.rstrip().endswith("Leállítva.")

    def test_between_cycles(self, tmp_path, caplog):
        async def scenario():
            async with StallingServer() as server:
                m = make_monitor(tmp_path, server, [])
                return await run_and_signal(m, 0.5, "between cycles")

        with caplog.at_level(logging.INFO):
            elapsed = asyncio.run(scenario())
        assert "Sikeres ciklus." in caplog.text
        assert elapsed < STOP_LIMIT

    def test_during_the_recording_the_save_and_the_email_still_happen(self, tmp_path):
        """Interrupted between the save and the email, a new listing would count
        as seen but never be emailed; and a second save must not race the first."""
        saves = {"active": 0, "max": 0}

        async def scenario():
            async with StallingServer() as server:
                m = make_monitor(tmp_path, server, ["new"], FakeNotifier(delay=0.3))
                real_save = m.save_seen_ads

                async def slow_save():
                    saves["active"] += 1
                    saves["max"] = max(saves["max"], saves["active"])
                    try:
                        await asyncio.sleep(1.0)
                        await real_save()
                    finally:
                        saves["active"] -= 1

                m.save_seen_ads = slow_save
                elapsed = await run_and_signal(m, 0.3, "during the save of a new listing")
                return m, elapsed

        m, elapsed = asyncio.run(scenario())
        assert elapsed < STOP_LIMIT
        assert NEW in saved(tmp_path)
        assert m.email_notifier.sent == ["Hirdetések: 1 db"]
        assert saves["max"] == 1

    def test_an_email_that_never_finishes_holds_the_stop_only_for_the_wait_limit(
        self, tmp_path, caplog
    ):
        async def scenario():
            async with StallingServer() as server:
                m = make_monitor(tmp_path, server, ["new"], FakeNotifier(hang=True))
                m.SHUTDOWN_WAIT_SECONDS = 0.5
                return await run_and_signal(m, 0.5, "email that never finishes (limit 0.5 s)")

        with caplog.at_level(logging.INFO):
            elapsed = asyncio.run(scenario())
        assert NEW in saved(tmp_path)
        assert "1 levél nem ment el időben" in caplog.text
        assert 0.4 < elapsed < 0.5 + STOP_LIMIT

    def test_a_cancel_from_outside_still_cleans_up_and_propagates(self, tmp_path, caplog):
        async def scenario():
            async with StallingServer() as server:
                m = make_monitor(tmp_path, server, ["stall"])
                task = asyncio.create_task(m.run())
                await asyncio.sleep(0.5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                return m

        with caplog.at_level(logging.INFO):
            m = asyncio.run(scenario())
        assert m.session.closed
        assert caplog.text.rstrip().endswith("Leállítva.")


class TestSmtpTimeout:
    def test_the_smtp_connection_has_a_timeout(self, monkeypatch):
        """In a thread, the send can't be cancelled: without a timeout, a stalled
        server would hold the shutdown (asyncio.run waits for the thread)."""
        seen = {}

        class FakeSMTP:
            def __init__(self, host, port, **kwargs):
                seen.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                pass

            def send_message(self, message):
                pass

        monkeypatch.setattr(email_module.smtplib, "SMTP", FakeSMTP)
        notifier = email_module.EmailNotifier(
            {
                "smtp_server": "smtp.example.com",
                "smtp_port": 587,
                "sender_email": "a@example.com",
                "sender_password": "x",
                "recipient_email": "b@example.com",
            }
        )
        assert notifier._send_email_sync("Tárgy", "Szöveg") is True
        assert seen.get("timeout") == email_module.SMTP_TIMEOUT_SECONDS
        assert 0 < email_module.SMTP_TIMEOUT_SECONDS <= 30
