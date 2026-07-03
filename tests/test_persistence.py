"""Tests for the seen-ads persistence/dedup/cleanup logic. These construct a
bare MultiMarketplaceMonitor via object.__new__() to skip __init__ (which
would call load_config()/setup_logging() and touch real files/loggers) and
only set the handful of attributes each method actually needs.
"""

import asyncio
import os
import types
from datetime import datetime, timedelta

import product_monitor as pm


def bare_monitor(**attrs):
    monitor = object.__new__(pm.MultiMarketplaceMonitor)
    for key, value in attrs.items():
        setattr(monitor, key, value)
    return monitor


def make_ad(ad_id="Vatera_1", price=1000, last_seen=None, title="Cím"):
    return pm.Advertisement(
        id=ad_id,
        title=title,
        price=price,
        url="https://example.com/1",
        site="Vatera",
        search_id="abc123",
        first_seen=last_seen or datetime.now().isoformat(),
        last_seen=last_seen or datetime.now().isoformat(),
    )


class TestCleanupOldAds:
    def test_removes_ads_older_than_cutoff(self):
        old_ad = make_ad("old", last_seen=(datetime.now() - timedelta(days=100)).isoformat())
        recent_ad = make_ad("recent", last_seen=datetime.now().isoformat())
        monitor = bare_monitor(
            seen_ads={"old": old_ad, "recent": recent_ad},
            seen_ads_set={"old", "recent"},
            config=types.SimpleNamespace(cleanup_days=60),
        )
        monitor.cleanup_old_ads()
        assert "old" not in monitor.seen_ads
        assert "old" not in monitor.seen_ads_set
        assert "recent" in monitor.seen_ads

    def test_malformed_last_seen_counts_as_old(self):
        monitor = bare_monitor()
        bad_ad = make_ad("bad", last_seen="not-a-date")
        assert monitor._is_ad_old(bad_ad, datetime.now()) is True


class TestProcessAdvertisements:
    def test_new_ad_is_returned_and_stored(self):
        monitor = bare_monitor(seen_ads={}, seen_ads_set=set())
        ad = make_ad("Vatera_1")
        new_ads = monitor._process_advertisements([ad])
        assert new_ads == [ad]
        assert "Vatera_1" in monitor.seen_ads
        assert monitor._ads_changed is True

    def test_already_seen_ad_is_not_returned_again(self):
        ad = make_ad("Vatera_1")
        monitor = bare_monitor(seen_ads={"Vatera_1": ad}, seen_ads_set={"Vatera_1"})
        new_ads = monitor._process_advertisements([make_ad("Vatera_1", price=1000)])
        assert new_ads == []

    def test_price_change_updates_existing_ad(self):
        ad = make_ad("Vatera_1", price=1000)
        monitor = bare_monitor(seen_ads={"Vatera_1": ad}, seen_ads_set={"Vatera_1"})
        monitor._process_advertisements([make_ad("Vatera_1", price=800)])
        assert monitor.seen_ads["Vatera_1"].price == 800
        assert monitor._ads_changed is True


class TestSeenAdsRoundTrip:
    def test_save_then_load_round_trip(self, tmp_path):
        seen_ads_file = tmp_path / "seen-products.json"
        ad = make_ad("Vatera_1", price=1234)

        saver = bare_monitor(
            seen_ads={"Vatera_1": ad},
            seen_ads_file=str(seen_ads_file),
            data_dir=str(tmp_path),
            _ads_changed=True,
        )
        asyncio.run(saver.save_seen_ads())

        assert seen_ads_file.exists()
        assert saver._ads_changed is False

        loader = bare_monitor(seen_ads_file=str(seen_ads_file))
        loaded = asyncio.run(loader.load_seen_ads())

        assert loaded == {"Vatera_1": ad}
        assert loader.seen_ads_set == {"Vatera_1"}

    def test_save_is_a_noop_when_nothing_changed(self, tmp_path):
        seen_ads_file = tmp_path / "seen-products.json"
        monitor = bare_monitor(
            seen_ads={"Vatera_1": make_ad("Vatera_1")},
            seen_ads_file=str(seen_ads_file),
            data_dir=str(tmp_path),
            _ads_changed=False,
        )
        asyncio.run(monitor.save_seen_ads())
        assert not seen_ads_file.exists()

    def test_load_missing_file_returns_empty_dict(self, tmp_path):
        backup_file = "/tmp/product-monitor-backup/seen-products.json"
        if os.path.exists(backup_file):
            import pytest

            pytest.skip(
                "global backup file exists on this machine, would make the test order-dependent"
            )

        loader = bare_monitor(seen_ads_file=str(tmp_path / "does-not-exist.json"))
        loaded = asyncio.run(loader.load_seen_ads())
        assert loaded == {}
