"""check_memory_usage(): optional diagnostics that must never cost a cycle.

It runs at the start of every cycle, before the scrape. An exception escaping
from it would skip that cycle, and if the timestamp weren't advanced, every
retry after it too (the loop retries 60 s after an error) - no scraping, no
daily check, no alert email. psutil may be missing, and the sandbox may hide
what it reads.
"""

import logging
import sys
import types

import product_monitor as pm
from product_monitor import monitor as monitor_module

INTERVAL = 10800


def bare_monitor(last_check):
    m = object.__new__(pm.MultiMarketplaceMonitor)
    m.last_memory_check = last_check
    m.memory_check_interval = INTERVAL
    return m


def fake_psutil(rss=None, error=None):
    calls = []

    class Process:
        def __init__(self):
            calls.append("Process")
            if error is not None:
                raise error

        def memory_info(self):
            return types.SimpleNamespace(rss=rss)

    return types.SimpleNamespace(Process=Process), calls


def patch_clock(monkeypatch, now):
    monkeypatch.setattr(monitor_module.time, "time", lambda: now)


def count_gc(monkeypatch):
    collected = []
    monkeypatch.setattr(monitor_module.gc, "collect", lambda: collected.append(1))
    return collected


class TestMemoryCheck:
    def test_not_due_yet_does_nothing(self, monkeypatch):
        psutil, calls = fake_psutil(rss=500 * 1024 * 1024)
        monkeypatch.setitem(sys.modules, "psutil", psutil)
        patch_clock(monkeypatch, 1000.0 + INTERVAL)
        m = bare_monitor(last_check=1000.0)
        m.check_memory_usage()
        assert calls == []
        assert m.last_memory_check == 1000.0

    def test_high_usage_warns_and_collects(self, monkeypatch, caplog):
        psutil, _ = fake_psutil(rss=250 * 1024 * 1024)
        monkeypatch.setitem(sys.modules, "psutil", psutil)
        collected = count_gc(monkeypatch)
        patch_clock(monkeypatch, 50000.0)
        m = bare_monitor(last_check=0.0)
        with caplog.at_level(logging.WARNING):
            m.check_memory_usage()
        assert "Magas memória használat: 250.0MB" in caplog.text
        assert collected == [1]
        assert m.last_memory_check == 50000.0

    def test_normal_usage_is_silent(self, monkeypatch, caplog):
        psutil, calls = fake_psutil(rss=50 * 1024 * 1024)
        monkeypatch.setitem(sys.modules, "psutil", psutil)
        collected = count_gc(monkeypatch)
        patch_clock(monkeypatch, 50000.0)
        m = bare_monitor(last_check=0.0)
        with caplog.at_level(logging.WARNING):
            m.check_memory_usage()
        assert calls == ["Process"]
        assert caplog.text == ""
        assert collected == []
        assert m.last_memory_check == 50000.0

    def test_missing_psutil_is_silent_and_waits_an_interval(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "psutil", None)  # -> ImportError on import
        patch_clock(monkeypatch, 50000.0)
        m = bare_monitor(last_check=0.0)
        with caplog.at_level(logging.WARNING):
            m.check_memory_usage()
        assert caplog.text == ""
        assert m.last_memory_check == 50000.0

    def test_a_psutil_failure_is_logged_not_raised(self, monkeypatch, caplog):
        psutil, _ = fake_psutil(error=FileNotFoundError(2, "No such file", "/proc/stat"))
        monkeypatch.setitem(sys.modules, "psutil", psutil)
        patch_clock(monkeypatch, 50000.0)
        m = bare_monitor(last_check=0.0)
        with caplog.at_level(logging.WARNING):
            m.check_memory_usage()  # must not raise
        assert "Memóriahasználat nem mérhető: FileNotFoundError" in caplog.text
        assert m.last_memory_check == 50000.0

    def test_after_a_failure_the_next_try_waits_a_full_interval(self, monkeypatch):
        psutil, calls = fake_psutil(error=PermissionError("denied"))
        monkeypatch.setitem(sys.modules, "psutil", psutil)
        m = bare_monitor(last_check=0.0)
        patch_clock(monkeypatch, 50000.0)
        m.check_memory_usage()
        patch_clock(monkeypatch, 50060.0)  # the loop's 60 s retry after an error
        m.check_memory_usage()
        assert calls == ["Process"]
        patch_clock(monkeypatch, 50000.0 + INTERVAL + 1)
        m.check_memory_usage()
        assert calls == ["Process", "Process"]
