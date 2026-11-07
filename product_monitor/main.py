"""Entry point: runs the startup gates, then MultiMarketplaceMonitor until shutdown.

    python3 -m product_monitor.main           # run
    python3 -m product_monitor.main --check   # run only the startup gates, then exit

Exit codes: 0 = clean exit (with --check: every gate passed), 78 (EX_CONFIG) =
a startup gate failed, 1 = unexpected crash. The service unit has
RestartPreventExitStatus=78, so a configuration error leaves it failed rather
than restarting it every minute.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from . import config
from .models import MonitorConfig
from .monitor import MultiMarketplaceMonitor


@dataclass
class Startup:
    """What the startup gates produced - everything the monitor needs to start."""

    config: MonitorConfig
    data_dir: str


def preflight() -> Startup:
    """The startup gates. A real start and `--check` both call exactly this, so
    the check can't drift from what a start actually requires.

    1. the state directory (config.state_directory): exists, writable;
    2. the mail settings (config.load_email_config): as a service, the systemd
       credential with every required key;
    3. the config file (MonitorConfig.from_file): readable, valid.

    No network, no email, no state written - the only file touched is the
    state directory's write probe, which removes itself. Every gate runs even
    after one fails, so a single run reports every problem. Raises ConfigError.
    """
    errors: List[str] = []

    data_dir = ""
    try:
        data_dir = config.state_directory()
    except config.ConfigError as e:
        errors.append(str(e))

    try:
        config.load_email_config()
    except config.ConfigError as e:
        errors.append(str(e))

    monitor_config: Optional[MonitorConfig] = None
    try:
        monitor_config = MonitorConfig.from_file(config.CONFIG_PATH)
    except config.ConfigError as e:
        errors.append(str(e))

    if errors or monitor_config is None:
        raise config.ConfigError("\n".join(errors))
    return Startup(config=monitor_config, data_dir=data_dir)


def _report_config_error(error: config.ConfigError) -> None:
    # Logging isn't set up yet (its level comes from the config file). As a
    # service, stderr goes to the same log file as stdout.
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for line in str(error).splitlines():
        print(f"{stamp} - product_monitor - ERROR - Beállítási hiba: {line}", file=sys.stderr)
    print(
        f"{stamp} - product_monitor - ERROR - A monitor nem indul (kilépési kód {config.EX_CONFIG}).",
        file=sys.stderr,
        flush=True,
    )


async def _run(startup: Startup) -> None:
    monitor = MultiMarketplaceMonitor(startup.config, startup.data_dir)
    monitor.setup_logging()
    try:
        await monitor.run()
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt.")
    except Exception as e:
        logging.error(f"Váratlan hiba: {e}", exc_info=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="product-monitor")
    parser.add_argument(
        "--check",
        action="store_true",
        help="csak az indulási ellenőrzések (állapot-könyvtár, levelezési adatok, "
        "beállítás-fájl), utána kilépés: 0 = rendben, 78 = beállítási hiba",
    )
    args = parser.parse_args(argv)

    try:
        startup = preflight()
    except config.ConfigError as e:
        _report_config_error(e)
        return config.EX_CONFIG

    if args.check:
        print(f"Indulási ellenőrzés rendben (állapot-könyvtár: {startup.data_dir}).")
        return 0

    try:
        asyncio.run(_run(startup))
    except KeyboardInterrupt:
        print("\nProgram megszakítva.")
    except Exception as e:
        print(f"Kritikus hiba: {e}")
        logging.error(f"Kritikus hiba: {e}", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
