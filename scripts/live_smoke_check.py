#!/usr/bin/env python3
"""Live check against the real marketplace sites - confirms the scrapers in
product_monitor still match the live page structure.

Thin CLI wrapper around product_monitor.health_check, which holds the actual
logic (and is also what the running service uses for its daily self-check, see
MultiMarketplaceMonitor.maybe_run_health_check). Keeping both on the same code
path means a bug here can't hide a bug there.

Deliberately NOT part of pytest: it needs real network access, is slow, and is
non-deterministic (site inventory changes constantly). Run it by hand whenever
a dependency gets bumped, or to double-check a red CI live-check run (GitHub's
hosted runners get blocked by some of these sites - see CLAUDE.md).

Exits non-zero if any site errored or returned 0 raw cards. A site returning 0
*matches* while its raw card count is fine is not an error - see
product_monitor/health_check.py for why.

Usage: .venv/bin/python scripts/live_smoke_check.py
"""

import asyncio
import sys
import types
from pathlib import Path

# Makes `import product_monitor` work without requiring `pip install -e .`
# first - same mechanism tests/conftest.py uses, since this script is run as a
# plain path (`python3 scripts/live_smoke_check.py`, not `-m`), which only puts
# scripts/ itself on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import product_monitor as pm  # noqa: E402
from product_monitor.health_check import (  # noqa: E402
    format_report,
    is_failure,
    run_health_check,
)


async def main() -> int:
    monitor = object.__new__(pm.MultiMarketplaceMonitor)
    monitor.config = types.SimpleNamespace(max_concurrent_requests=4, request_timeout=30)
    await monitor.create_session()

    try:
        results = await run_health_check(monitor)
    finally:
        await monitor.session.close()

    print(format_report(results))
    return 1 if any(is_failure(r) for r in results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
