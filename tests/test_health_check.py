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
    def test_subject_counts_the_alerted_sites(self):
        subject, _ = health_check.build_health_alert(
            [OK, ZERO_MATCHES, NO_CARDS, FETCH_ERROR], ["Jofogas", "HardverApro"]
        )
        assert subject == "Product Monitor: hibás oldal (2 db)"

    def test_body_names_the_alerted_sites_and_includes_the_full_report(self):
        _, body = health_check.build_health_alert([OK, NO_CARDS, FETCH_ERROR], ["Jofogas"])
        assert "Jofogas" in body
        assert "HardverApro" in body  # the full table is included...
        assert "403" in body
        assert "Vatera" in body  # ...healthy sites too


class TestFormatError:
    def test_empty_message_falls_back_to_exception_type(self):
        # The Sept 2026 Moly emails showed a bare "ERROR:" - an exception whose
        # str() is empty. We surface the type name instead.
        class SomeTimeout(Exception):
            pass

        assert health_check._format_error(SomeTimeout()) == "SomeTimeout"

    def test_message_is_kept_with_a_type_prefix(self):
        assert health_check._format_error(ValueError("nope")) == "ValueError: nope"


class TestFailingSites:
    def test_returns_only_the_failing_site_names(self):
        assert health_check.failing_sites([OK, ZERO_MATCHES, NO_CARDS, FETCH_ERROR]) == {
            "Jofogas",
            "HardverApro",
        }


class TestCheckSiteRetry:
    CHECK = {"site": "X", "raw_counter": lambda h: 1, "search_config": None}

    def test_retries_a_transient_failure_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        async def flaky(_monitor, _check):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"site": "X", "error": "Broken pipe"}
            return {"site": "X", "raw": 5, "matched": 2}

        monkeypatch.setattr(health_check, "_check_site_once", flaky)
        monkeypatch.setattr(health_check, "_HEALTH_RETRY_DELAY", 0)
        result = asyncio.run(health_check.check_site(None, self.CHECK))
        assert result == {"site": "X", "raw": 5, "matched": 2}
        assert calls["n"] == 2  # failed once, retried, succeeded

    def test_gives_up_and_reports_failure_after_max_attempts(self, monkeypatch):
        calls = {"n": 0}

        async def always_fail(_monitor, _check):
            calls["n"] += 1
            return {"site": "X", "error": "still broken"}

        monkeypatch.setattr(health_check, "_check_site_once", always_fail)
        monkeypatch.setattr(health_check, "_HEALTH_RETRY_DELAY", 0)
        result = asyncio.run(health_check.check_site(None, self.CHECK))
        assert "error" in result
        assert calls["n"] == health_check._HEALTH_RETRY_ATTEMPTS


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


def make_monitor(hour=17, enabled=True, last_date=None, min_consecutive=1, state_file=None):
    m = object.__new__(pm.MultiMarketplaceMonitor)
    m.config = SimpleNamespace(
        health_check_enabled=enabled,
        health_check_hour=hour,
        health_check_min_consecutive=min_consecutive,
    )
    m.last_health_check_date = last_date
    m.health_state_file = state_file
    m.email_notifier = FakeNotifier()
    m._email_tasks = set()
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


class TestConsecutiveFailureGate:
    """With health_check_min_consecutive=2, a one-off failure is recorded but
    NOT emailed - only a site failing two consecutive daily checks alerts. This
    is what kills the transient-blip false alarms from production."""

    def _state_file(self, tmp_path):
        return str(tmp_path / "health-check-state.json")

    def test_first_failure_is_recorded_but_not_emailed(self, monkeypatch, tmp_path):
        sf = self._state_file(tmp_path)
        m = make_monitor(hour=17, min_consecutive=2, state_file=sf)
        run_with_clock(m, monkeypatch, datetime(2026, 7, 13, 17, 0), [OK, FETCH_ERROR])
        assert m.email_notifier.sent == []
        assert m._load_health_failures().get("HardverApro") == 1

    def test_second_consecutive_failure_emails(self, monkeypatch, tmp_path):
        sf = self._state_file(tmp_path)
        first = make_monitor(hour=17, min_consecutive=2, state_file=sf)
        run_with_clock(first, monkeypatch, datetime(2026, 7, 13, 17, 0), [OK, FETCH_ERROR])
        assert first.email_notifier.sent == []

        second = make_monitor(hour=17, min_consecutive=2, state_file=sf)
        run_with_clock(second, monkeypatch, datetime(2026, 7, 14, 17, 0), [OK, FETCH_ERROR])
        assert len(second.email_notifier.sent) == 1
        subject, _ = second.email_notifier.sent[0]
        assert subject == "Product Monitor: hibás oldal (1 db)"

    def test_recovery_resets_the_count(self, monkeypatch, tmp_path):
        sf = self._state_file(tmp_path)
        broke = make_monitor(hour=17, min_consecutive=2, state_file=sf)
        run_with_clock(broke, monkeypatch, datetime(2026, 7, 13, 17, 0), [FETCH_ERROR])

        healed = make_monitor(hour=17, min_consecutive=2, state_file=sf)
        run_with_clock(healed, monkeypatch, datetime(2026, 7, 14, 17, 0), [OK])
        assert healed._load_health_failures() == {}

        # A failure the day after recovery is "first" again - no email.
        again = make_monitor(hour=17, min_consecutive=2, state_file=sf)
        run_with_clock(again, monkeypatch, datetime(2026, 7, 15, 17, 0), [FETCH_ERROR])
        assert again.email_notifier.sent == []
