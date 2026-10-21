import json
import logging
import os
from typing import Any, Dict, Optional


class DealState:
    """deal-state.json: one small JSON object, one section per watcher.

    Written atomically (temp file + os.replace) after every change - the file
    is a few KB, and losing a write would mean a duplicate alert or a missed
    "already seen" mark after a restart."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self.data: Dict[str, Any] = {}

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as e:
            # Worst case: XiaomiPlanet re-sends its first-run summary once.
            logging.warning(f"Deal állapot betöltése sikertelen, újrakezdés: {e}")
            self.data = {}

    def section(self, name: str) -> Dict[str, Any]:
        return self.data.setdefault(name, {})

    def save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            logging.error(f"Deal állapot mentése sikertelen: {e}")
