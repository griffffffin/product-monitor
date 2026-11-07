"""Runtime settings that come from the process environment rather than from
product-monitor-config.json: where the state lives, where the mail credentials
come from, and the paths derived from the package's own location.

Nothing here reads a file, probes a directory or exits at import time - the
tests and scripts/ import the package. The two startup gates in this module,
state_directory() and load_email_config(), run from main.preflight(), at
startup (and from `--check`, which runs exactly those gates and nothing else).

Two modes, told apart by INVOCATION_ID (systemd sets it for every unit it
starts):
  - service: state in $STATE_DIRECTORY (systemd StateDirectory=), mail
    credentials from the systemd credential named "smtp" (ImportCredential=smtp,
    readable as $CREDENTIALS_DIRECTORY/smtp). Both are mandatory.
  - manual (development) run: state in the project root, mail settings from
    the SMTP_* / SENDER_* / RECIPIENT_EMAIL environment variables.
There is no fallback location in either mode: a missing or unwritable state
directory is a configuration error, not something to route around.
"""

import os
import secrets
from typing import Any, Dict, Mapping

# sysexits.h EX_CONFIG. The service unit has RestartPreventExitStatus=78, so a
# configuration problem leaves the unit failed instead of restarting it every
# minute forever.
EX_CONFIG = 78

# The project root (the directory holding the product_monitor/ package), from
# this file's own location - never from the process's working directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "product-monitor-config.json")
# Manual runs only. As a service, logging goes to stdout and systemd appends it
# to the service log - the code never opens that file itself.
LOG_FILE = os.path.join(PROJECT_ROOT, "product-monitor.log")

# The systemd credential holding the mail settings, shared with the host's
# other mail senders (same file format, same key names).
CREDENTIAL_NAME = "smtp"
EMAIL_KEYS = ("SMTP_SERVER", "SMTP_PORT", "SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL")
# Without any of these no email can go out - and a monitor that cannot email
# still marks every listing as seen, i.e. silently swallows it.
REQUIRED_EMAIL_KEYS = ("SMTP_SERVER", "SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL")


class ConfigError(Exception):
    """A startup gate failed: the monitor must not run (main() exits with EX_CONFIG).

    The message names paths and keys only, never a credential value."""


def is_service_mode() -> bool:
    return bool(os.environ.get("INVOCATION_ID"))


def state_directory() -> str:
    """Startup gate: the one directory the monitor writes to (seen-products.json,
    health-check-state.json, deal-state.json, their .tmp files and the oversized
    file's .backup-<time> copy). Returns it, or raises ConfigError."""
    if is_service_mode():
        path = os.environ.get("STATE_DIRECTORY", "")
        if not path:
            raise ConfigError(
                "szolgáltatásként kötelező a STATE_DIRECTORY (a unit StateDirectory= sora adja), "
                "de nincs megadva"
            )
        if ":" in path:
            # systemd joins several StateDirectory= entries with ':'
            raise ConfigError(f"a STATE_DIRECTORY egyetlen könyvtár legyen, nem lista: {path}")
        if not os.path.isabs(path):
            raise ConfigError(f"a STATE_DIRECTORY nem abszolút út: {path}")
    else:
        path = PROJECT_ROOT
    if not os.path.isdir(path):
        raise ConfigError(f"az állapot-könyvtár nem létezik (vagy nem könyvtár): {path}")
    _check_writable(path)
    return path


def _check_writable(directory: str) -> None:
    """Create, write and remove a probe file - the atomic saves need exactly
    this (a new file next to the target, then a rename in the same directory).
    os.access() alone can be fooled (ACLs, read-only mounts, a full disk)."""
    probe = os.path.join(directory, f".write-check-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        # O_EXCL: never follows or reuses an existing path (symlink included)
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as e:
        raise ConfigError(f"az állapot-könyvtár nem írható: {directory} ({e.strerror})") from None
    try:
        os.write(fd, b"ok\n")
    except OSError as e:
        raise ConfigError(f"az állapot-könyvtár nem írható: {directory} ({e.strerror})") from None
    finally:
        os.close(fd)
        try:
            os.unlink(probe)
        except OSError:
            pass


def parse_credentials(text: str) -> Dict[str, str]:
    """The host's mail-credential format, read as DATA, never as shell code:

    - one KEY=VALUE per line, split at the first '='; the value is literal
      (no quote, '$' or '\\' handling, no 'export') - a password may contain
      '=', '#', quotes or spaces;
    - empty lines and lines starting with '#' are skipped, as are lines with no
      '='; one trailing '\\r' is dropped (CRLF files);
    - only the EMAIL_KEYS are kept, by exact key match; a repeated key's last
      occurrence wins (the same as the host's shell reader).
    """
    values: Dict[str, str] = {}
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key in EMAIL_KEYS:
            values[key] = value
    return values


def _email_config_from_environment() -> Dict[str, Any]:
    """Manual runs: the environment variables, with the same defaults as ever."""
    return {
        "smtp_server": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
        "sender_email": os.environ.get("SENDER_EMAIL", ""),
        "sender_password": os.environ.get("SENDER_PASSWORD", ""),
        "recipient_email": os.environ.get("RECIPIENT_EMAIL", ""),
    }


def _email_config_from_credential() -> Dict[str, Any]:
    """Service mode: the systemd credential, all four required keys non-empty."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not directory:
        raise ConfigError(
            "szolgáltatásként a levelezési adatok a systemd credentialból jönnek "
            f"(ImportCredential={CREDENTIAL_NAME}), de nincs CREDENTIALS_DIRECTORY"
        )
    path = os.path.join(directory, CREDENTIAL_NAME)
    try:
        # newline="": no newline translation, so a lone '\r' inside a value stays put
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except FileNotFoundError:
        raise ConfigError(f"hiányzik a levelezési credential: {path}") from None
    except UnicodeDecodeError:
        # The exception text would quote the offending bytes - not logged
        raise ConfigError(f"a levelezési credential nem UTF-8 szöveg: {path}") from None
    except OSError as e:
        raise ConfigError(f"a levelezési credential nem olvasható: {path} ({e.strerror})") from None
    return _email_config_from_values(parse_credentials(text), path)


def _email_config_from_values(values: Mapping[str, str], source: str) -> Dict[str, Any]:
    missing = [key for key in REQUIRED_EMAIL_KEYS if not values.get(key)]
    if missing:
        raise ConfigError(f"a levelezési credentialból hiányzik vagy üres: {', '.join(missing)}")
    try:
        port = int(values.get("SMTP_PORT") or "587")
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise ConfigError(f"a levelezési credential SMTP_PORT értéke nem érvényes port: {source}")
    return {
        "smtp_server": values["SMTP_SERVER"],
        "smtp_port": port,
        "sender_email": values["SENDER_EMAIL"],
        "sender_password": values["SENDER_PASSWORD"],
        "recipient_email": values["RECIPIENT_EMAIL"],
    }


# One module-level dict for the process's lifetime: MonitorConfig.from_dict()
# hands this very object to EmailNotifier, so load_email_config() refills it in
# place instead of rebinding it. Until then it holds the environment values
# (what tests and scripts see; harmless, nothing is sent from them).
EMAIL_CONFIG: Dict[str, Any] = _email_config_from_environment()


def load_email_config() -> Dict[str, Any]:
    """Startup gate: in service mode the credential must exist and carry every
    required key (else ConfigError); a manual run re-reads the environment."""
    if is_service_mode():
        fresh = _email_config_from_credential()
    else:
        fresh = _email_config_from_environment()
    EMAIL_CONFIG.clear()
    EMAIL_CONFIG.update(fresh)
    return EMAIL_CONFIG
