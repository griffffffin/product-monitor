import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import product_monitor as pm
from product_monitor import health_check, monitor as monitor_module

OK = {"site": "Vatera", "raw": 50, "matched": 12}
ZERO_MATCHES = {"site": "Moly", "raw": 16, "matched": 0}
NO_CARDS = {"site": "Jofogas", "raw": 0, "matched": 0}
FETCH_ERROR = {"site": "HardverApro", "error": "403, message='Forbidden'"}


class TestIsFailure:
    def test_fetch_error_is_a_failure(self):
        assert health_check.is_failure(FETCH_ERROR)

    def test_zero_raw_cards_is_a_failure(self):
        assert health_check.is_failure(NO_CARDS)

    def test_zero_matches_with_raw_cards_is_not_a_failure(self):
        # "Nothing for sale right now" is the normal state - must stay silent
        assert not health_check.is_failure(ZERO_MATCHES)

    def test_healthy_result_is_not_a_failure(self):
        assert not health_check.is_failure(OK)


class TestBuildHealthAlert:
    def test_subject_counts_only_failed_sites(self):
        subject, _ = health_check.build_health_alert([OK, ZERO_MATCHES, NO_CARDS, FETCH_ERROR])
        assert subject == "Product Monitor: hibás oldal (2 db)"

    def test_body_names_the_failed_sites_and_includes_the_full_report(self):
        _, body = health_check.build_health_alert([OK, NO_CARDS, FETCH_ERROR])
        assert "Jofogas" in body
        assert "HardverApro" in body
        assert "403" in body
        assert "Vatera" in body  # the full table is included, healthy sites too


class TestFormatReport:
    def test_marks_each_status(self):
        report = health_check.format_report([OK, NO_CARDS, FETCH_ERROR])
        lines = {line.split()[0]: line for line in report.splitlines()[2:]}
        assert "OK" in lines["Vatera"]
        assert "WARN" in lines["Jofogas"]
        assert "ERROR" in lines["HardverApro"]


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send_notification(self, subject, body):
        self.sent.append((subject, body))
        return True


def make_monitor(hour=17, enabled=True, last_date=None):
    m = object.__new__(pm.MultiMarketplaceMonitor)
    m.config = SimpleNamespace(health_check_enabled=enabled, health_check_hour=hour)
    m.last_health_check_date = last_date
    m.email_notifier = FakeNotifier()
    return m


def run_with_clock(m, monkeypatch, now, results):
    """Run maybe_run_health_check() with a frozen clock and canned results."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    async def fake_run_health_check(_monitor):
        return results

    monkeypatch.setattr(monitor_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(health_check, "run_health_check", fake_run_health_check)

    async def go():
        await m.maybe_run_health_check()
        # send_notification is fired as a background task - let it run
        await asyncio.sleep(0)

    asyncio.run(go())


class TestMaybeRunHealthCheck:
    def test_does_not_run_before_the_configured_hour(self, monkeypatch):
        m = make_monitor(hour=17)
        run_with_clock(m, monkeypatch, datetime(2026, 7, 13, 16, 59), [FETCH_ERROR])
        assert m.last_health_check_date is None
        assert m.email_notifier.sent == []

    def test_runs_at_the_configured_hour_and_emails_on_failure(self, monkeypatch):
        m = make_monitor(hour=17)
        run_with_clock(m, monkeypatch, datetime(2026, 7, 13, 17, 0), [OK, NO_CARDS])
        assert m.last_health_check_date == date(2026, 7, 13)
        assert len(m.email_notifier.sent) == 1
        subject, _ = m.email_notifier.sent[0]
        assert subject == "Product Monitor: hibás oldal (1 db)"

    def test_no_email_when_every_site_is_healthy(self, monkeypatch):
        m = make_monitor(hour=17)
        run_with_clock(m, monkeypatch, datetime(2026, 7, 13, 18, 0), [OK, ZERO_MATCHES])
        assert m.last_health_check_date == date(2026, 7, 13)
        assert m.email_notifier.sent == []

    def test_runs_only_once_a_day(self, monkeypatch):
        m = make_monitor(hour=17, last_date=date(2026, 7, 13))
        run_with_clock(m, monkeypatch, datetime(2026, 7, 13, 20, 0), [FETCH_ERROR])
        assert m.email_notifier.sent == []

    def test_runs_again_the_next_day(self, monkeypatch):
        m = make_monitor(hour=17, last_date=date(2026, 7, 12))
        run_with_clock(m, monkeypatch, datetime(2026, 7, 13, 17, 30), [FETCH_ERROR])
        assert m.last_health_check_date == date(2026, 7, 13)
        assert len(m.email_notifier.sent) == 1

    def test_disabled_by_config(self, monkeypatch):
        m = make_monitor(hour=17, enabled=False)
        run_with_clock(m, monkeypatch, datetime(2026, 7, 13, 18, 0), [FETCH_ERROR])
        assert m.last_health_check_date is None
        assert m.email_notifier.sent == []

    def test_a_crashing_health_check_does_not_propagate(self, monkeypatch):
        """The monitoring cycle must survive a broken health check."""
        m = make_monitor(hour=17)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 7, 13, 18, 0)

        async def boom(_monitor):
            raise RuntimeError("network is on fire")

        monkeypatch.setattr(monitor_module, "datetime", FrozenDatetime)
        monkeypatch.setattr(health_check, "run_health_check", boom)

        asyncio.run(m.maybe_run_health_check())  # must not raise
        assert m.email_notifier.sent == []
