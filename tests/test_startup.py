"""Startup gates: the state directory, the mail credential, the config file
path, and `--check` (which runs exactly those gates and nothing else).

The service runs as an unprivileged user with a read-only code directory: the
only writable place is $STATE_DIRECTORY, and the mail settings arrive as a
systemd credential file. Every gate failure must end in exit code 78
(EX_CONFIG) at startup - never at import time, and never with a silent
fallback (the old /tmp fallback would have lost the seen-listings database on
every restart under PrivateTmp=yes).
"""

import asyncio
import os
import smtplib
import socket
import subprocess
import sys
from pathlib import Path

import aiohttp
import pytest

import product_monitor as pm
from product_monitor import config
from product_monitor import main as main_module

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "product_monitor"

PASSWORD = "pw=with #hash \"dq\" 'sq' and spaces $HOME \\n"
GOOD_CREDENTIAL = (
    "# mail settings shared by the host's senders\n"
    "\n"
    "SMTP_SERVER=smtp.example.org\n"
    "SMTP_PORT=587\n"
    "SENDER_EMAIL=sender@example.org\n"
    f"SENDER_PASSWORD={PASSWORD}\n"
    "RECIPIENT_EMAIL=owner@example.org\n"
)
MINIMAL_CONFIG = '{"searches": [], "check_interval": 60, "cleanup_days": 10}'
ENV_VARS = (
    "INVOCATION_ID",
    "STATE_DIRECTORY",
    "CREDENTIALS_DIRECTORY",
    "SMTP_SERVER",
    "SMTP_PORT",
    "SENDER_EMAIL",
    "SENDER_PASSWORD",
    "RECIPIENT_EMAIL",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Start every test in manual mode with no mail settings (a Claude Code /
    systemd-scoped shell has INVOCATION_ID set), and put EMAIL_CONFIG's
    contents back afterwards - load_email_config() refills it in place."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    saved = dict(config.EMAIL_CONFIG)
    yield
    config.EMAIL_CONFIG.clear()
    config.EMAIL_CONFIG.update(saved)


def writable_check_works():
    return os.geteuid() != 0  # root ignores directory permissions


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.creds = tmp_path / "credentials"
        self.creds.mkdir()
        self.code = tmp_path / "code"
        self.code.mkdir()
        self.config_file = self.code / "product-monitor-config.json"
        self.config_file.write_text(MINIMAL_CONFIG, encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_PATH", str(self.config_file))

    def service(self, state=True, creds=True):
        self.monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
        if state:
            self.monkeypatch.setenv("STATE_DIRECTORY", str(self.state))
        if creds:
            self.monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(self.creds))
        return self

    def credential(self, text=GOOD_CREDENTIAL, raw=None):
        path = self.creds / config.CREDENTIAL_NAME
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(text, encoding="utf-8")
        return self

    def manual(self, root):
        self.monkeypatch.setattr(config, "PROJECT_ROOT", str(root))
        return self


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def no_side_effects(monkeypatch):
    """Fail the test on any network connection, email or monitor run."""

    def boom(*args, **kwargs):
        raise AssertionError("the startup check must not do this")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(aiohttp.ClientSession, "__init__", boom)
    monkeypatch.setattr(smtplib, "SMTP", boom)
    monkeypatch.setattr(main_module.asyncio, "run", boom)
    monkeypatch.setattr(main_module, "MultiMarketplaceMonitor", boom)


# --- state directory ------------------------------------------------------


class TestStateDirectory:
    def test_service_mode_uses_state_directory(self, env):
        env.service()
        assert config.state_directory() == str(env.state)
        assert os.listdir(env.state) == []  # the write probe cleaned up after itself

    @pytest.mark.parametrize("value", [None, ""])
    def test_service_mode_without_state_directory_fails(self, env, monkeypatch, value):
        env.service(state=False)
        if value is not None:
            monkeypatch.setenv("STATE_DIRECTORY", value)
        with pytest.raises(config.ConfigError, match="STATE_DIRECTORY"):
            config.state_directory()

    def test_relative_path_fails(self, env, monkeypatch):
        env.service()
        monkeypatch.chdir(env.state.parent)
        monkeypatch.setenv("STATE_DIRECTORY", "state")  # exists relative to the cwd
        with pytest.raises(config.ConfigError, match="abszolút"):
            config.state_directory()

    def test_several_directories_fail(self, env, monkeypatch, tmp_path):
        env.service()
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("STATE_DIRECTORY", f"{env.state}:{other}")
        with pytest.raises(config.ConfigError, match="lista"):
            config.state_directory()

    def test_missing_directory_fails(self, env, monkeypatch):
        env.service()
        monkeypatch.setenv("STATE_DIRECTORY", str(env.state / "missing"))
        with pytest.raises(config.ConfigError, match="nem létezik"):
            config.state_directory()

    def test_a_file_is_not_a_directory(self, env, monkeypatch):
        env.service()
        file = env.state / "file"
        file.write_text("x")
        monkeypatch.setenv("STATE_DIRECTORY", str(file))
        with pytest.raises(config.ConfigError, match="nem létezik"):
            config.state_directory()

    @pytest.mark.skipif(not writable_check_works(), reason="root ignores permissions")
    def test_unwritable_state_directory_fails(self, env):
        env.service()
        env.state.chmod(0o500)
        try:
            with pytest.raises(config.ConfigError, match="nem írható"):
                config.state_directory()
        finally:
            env.state.chmod(0o700)

    def test_manual_mode_uses_the_project_root(self, env, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        env.manual(root)
        assert config.state_directory() == str(root)
        assert os.listdir(root) == []

    @pytest.mark.skipif(not writable_check_works(), reason="root ignores permissions")
    def test_manual_mode_unwritable_root_is_an_error_not_a_fallback(self, env, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        env.manual(root)
        root.chmod(0o500)
        try:
            with pytest.raises(config.ConfigError, match="nem írható"):
                config.state_directory()
        finally:
            root.chmod(0o700)

    def test_project_root_is_the_directory_holding_the_package(self):
        assert config.PROJECT_ROOT == str(REPO_ROOT)

    def test_service_mode_ignores_the_project_root(self, env, tmp_path):
        env.service()
        env.manual(tmp_path / "does-not-exist")
        assert config.state_directory() == str(env.state)


class TestStateFiles:
    def test_every_state_file_lives_in_the_state_directory(self, tmp_path):
        cfg = pm.MonitorConfig.from_dict(json_config())
        m = pm.MultiMarketplaceMonitor(cfg, str(tmp_path))
        for path in (m.seen_ads_file, m.health_state_file, m.deal_state_file):
            assert os.path.dirname(path) == str(tmp_path)
        assert {os.path.basename(p) for p in (m.seen_ads_file, m.health_state_file)} == {
            "seen-products.json",
            "health-check-state.json",
        }
        assert os.path.basename(m.deal_state_file) == "deal-state.json"
        assert os.listdir(tmp_path) == []  # constructing it writes nothing

    def test_oversized_database_is_moved_aside_inside_the_state_directory(
        self, tmp_path, monkeypatch
    ):
        seen = tmp_path / "seen-products.json"
        seen.write_text("{}")
        m = object.__new__(pm.MultiMarketplaceMonitor)
        m.seen_ads_file = str(seen)
        real_getsize = os.path.getsize
        monkeypatch.setattr(
            os.path, "getsize", lambda p: 60 * 1024 * 1024 if p == str(seen) else real_getsize(p)
        )
        assert asyncio.run(m.load_seen_ads()) == {}
        (moved,) = os.listdir(tmp_path)
        assert moved.startswith("seen-products.json.backup-")

    def test_a_failed_save_stays_pending_and_writes_nowhere_else(self, tmp_path, monkeypatch):
        m = object.__new__(pm.MultiMarketplaceMonitor)
        m.data_dir = str(tmp_path)
        m.seen_ads_file = str(tmp_path / "seen-products.json")
        m.seen_ads = {}
        m._ads_changed = True

        def denied(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("product_monitor.monitor.aiofiles.open", denied)
        asyncio.run(m.save_seen_ads())
        assert m._ads_changed is True  # retried next cycle
        assert os.listdir(tmp_path) == []

    def test_the_package_never_mentions_tmp(self):
        offenders = []
        for path in PACKAGE_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in ("/tmp", "tempfile", "gettempdir"):
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
        assert offenders == []


def json_config():
    return {"searches": [], "check_interval": 60, "cleanup_days": 10}


# --- explicit paths ---------------------------------------------------------


class TestExplicitPaths:
    def test_config_and_log_paths_come_from_the_package_location(self):
        assert config.CONFIG_PATH == str(REPO_ROOT / "product-monitor-config.json")
        assert config.LOG_FILE == str(REPO_ROOT / "product-monitor.log")

    def test_the_config_file_is_found_from_any_working_directory(
        self, env, tmp_path, monkeypatch, no_side_effects
    ):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # A decoy that a cwd-relative lookup would pick up (and fail on)
        (elsewhere / "product-monitor-config.json").write_text("not json")
        monkeypatch.chdir(elsewhere)
        env.service().credential()
        assert main_module.main(["--check"]) == 0
        assert os.listdir(elsewhere) == ["product-monitor-config.json"]


# --- the mail credential -----------------------------------------------------


class TestParseCredentials:
    def test_values_are_literal(self):
        values = config.parse_credentials(GOOD_CREDENTIAL)
        assert values["SENDER_PASSWORD"] == PASSWORD
        assert values["SMTP_SERVER"] == "smtp.example.org"

    def test_leading_and_trailing_spaces_are_part_of_the_value(self):
        assert config.parse_credentials("SENDER_PASSWORD=  a b  \n") == {
            "SENDER_PASSWORD": "  a b  "
        }

    def test_crlf_line_endings(self):
        values = config.parse_credentials(GOOD_CREDENTIAL.replace("\n", "\r\n"))
        assert values["SENDER_PASSWORD"] == PASSWORD
        assert values["RECIPIENT_EMAIL"] == "owner@example.org"

    def test_comment_empty_and_malformed_lines_are_skipped(self):
        text = "# SENDER_PASSWORD=from-a-comment\n\n   \nno equals sign\nSENDER_EMAIL=a@b\n"
        assert config.parse_credentials(text) == {"SENDER_EMAIL": "a@b"}

    def test_unknown_keys_are_dropped(self):
        assert config.parse_credentials("FOO=bar\nSMTP_PORT=465\n") == {"SMTP_PORT": "465"}

    @pytest.mark.parametrize(
        "line",
        [
            "XSMTP_SERVER=evil",
            "SMTP_SERVER_OLD=evil",
            "smtp_server=evil",
            " SMTP_SERVER=evil",
            "SMTP_SERVER =evil",
            "export SMTP_SERVER=evil",
        ],
    )
    def test_keys_match_exactly(self, line):
        assert config.parse_credentials(line + "\n") == {}

    def test_surrounding_quotes_are_part_of_the_value(self):
        # The old environment-file style - here the quotes would be part of the password
        assert config.parse_credentials("SENDER_PASSWORD=\"abcd efgh\"\nSMTP_SERVER='s'\n") == {
            "SENDER_PASSWORD": '"abcd efgh"',
            "SMTP_SERVER": "'s'",
        }

    def test_last_occurrence_wins(self):
        assert config.parse_credentials("SMTP_PORT=1\nSMTP_PORT=2\n") == {"SMTP_PORT": "2"}

    def test_last_line_without_newline(self):
        assert config.parse_credentials("SENDER_EMAIL=a@b") == {"SENDER_EMAIL": "a@b"}


class TestLoadEmailConfig:
    def test_service_mode_reads_the_credential(self, env):
        env.service().credential()
        loaded = config.load_email_config()
        assert loaded == {
            "smtp_server": "smtp.example.org",
            "smtp_port": 587,
            "sender_email": "sender@example.org",
            "sender_password": PASSWORD,
            "recipient_email": "owner@example.org",
        }

    def test_a_lone_carriage_return_inside_a_value_survives_the_file_read(self, env):
        raw = GOOD_CREDENTIAL.replace(PASSWORD, "ab\rcd").encode()
        env.service().credential(raw=raw)
        assert config.load_email_config()["sender_password"] == "ab\rcd"

    def test_crlf_file(self, env):
        env.service().credential(raw=GOOD_CREDENTIAL.replace("\n", "\r\n").encode())
        assert config.load_email_config()["sender_password"] == PASSWORD

    def test_the_monitor_config_sees_the_loaded_values(self, env):
        env.service().credential()
        config.load_email_config()
        monitor_config = pm.MonitorConfig.from_dict(json_config())
        assert monitor_config.email is pm.EMAIL_CONFIG
        assert monitor_config.email["sender_password"] == PASSWORD

    def test_service_mode_ignores_the_environment(self, env, monkeypatch):
        monkeypatch.setenv("SENDER_PASSWORD", "from-the-environment")
        env.service().credential()
        assert config.load_email_config()["sender_password"] == PASSWORD

    def test_environment_is_no_fallback_for_a_missing_credential(self, env, monkeypatch):
        for name, value in (
            ("SMTP_SERVER", "s"),
            ("SENDER_EMAIL", "e"),
            ("SENDER_PASSWORD", "p"),
            ("RECIPIENT_EMAIL", "r"),
        ):
            monkeypatch.setenv(name, value)
        env.service()  # no credential file written
        with pytest.raises(config.ConfigError, match="hiányzik a levelezési credential"):
            config.load_email_config()

    def test_no_credentials_directory(self, env):
        env.service(creds=False)
        with pytest.raises(config.ConfigError, match="CREDENTIALS_DIRECTORY"):
            config.load_email_config()

    @pytest.mark.parametrize("key", config.REQUIRED_EMAIL_KEYS)
    def test_a_missing_required_key_is_named_and_no_value_is_shown(self, env, key):
        text = "\n".join(
            line for line in GOOD_CREDENTIAL.splitlines() if not line.startswith(key + "=")
        )
        env.service().credential(text)
        with pytest.raises(config.ConfigError) as excinfo:
            config.load_email_config()
        message = str(excinfo.value)
        assert key in message
        for secret in (PASSWORD, "sender@example.org", "owner@example.org", "smtp.example.org"):
            assert secret not in message

    def test_an_empty_required_key_counts_as_missing(self, env):
        env.service().credential(GOOD_CREDENTIAL.replace(f"={PASSWORD}", "="))
        with pytest.raises(config.ConfigError, match="SENDER_PASSWORD"):
            config.load_email_config()

    def test_a_prefixed_key_does_not_stand_in_for_the_real_one(self, env):
        env.service().credential(GOOD_CREDENTIAL.replace("SMTP_SERVER=", "XSMTP_SERVER="))
        with pytest.raises(config.ConfigError, match="SMTP_SERVER"):
            config.load_email_config()

    def test_missing_port_defaults_to_587(self, env):
        env.service().credential(GOOD_CREDENTIAL.replace("SMTP_PORT=587\n", ""))
        assert config.load_email_config()["smtp_port"] == 587

    @pytest.mark.parametrize("port", ["abc", "0", "70000", "58 7"])
    def test_an_invalid_port_fails_without_showing_it(self, env, port):
        env.service().credential(GOOD_CREDENTIAL.replace("SMTP_PORT=587", f"SMTP_PORT={port}"))
        with pytest.raises(config.ConfigError, match="SMTP_PORT") as excinfo:
            config.load_email_config()
        assert f"={port}" not in str(excinfo.value) and PASSWORD not in str(excinfo.value)

    def test_a_non_utf8_credential_fails_without_quoting_it(self, env):
        env.service().credential(raw=b"SENDER_PASSWORD=\xff\xfesecret\n")
        with pytest.raises(config.ConfigError, match="UTF-8") as excinfo:
            config.load_email_config()
        assert "secret" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__

    def test_manual_mode_uses_the_environment(self, monkeypatch):
        monkeypatch.setenv("SENDER_PASSWORD", "manual-run")
        assert config.load_email_config()["sender_password"] == "manual-run"


# --- --check and the exit code ----------------------------------------------


class TestCheck:
    def test_the_config_error_exit_code_is_sysexits_ex_config(self):
        # The unit's RestartPreventExitStatus=78 depends on this exact number
        assert config.EX_CONFIG == 78

    def test_ok_environment_exits_0_and_touches_nothing(self, env, no_side_effects, capsys):
        env.service().credential()
        assert main_module.main(["--check"]) == 0
        assert os.listdir(env.state) == []  # no seen-products.json, no leftover probe
        assert "rendben" in capsys.readouterr().out

    def test_manual_mode_check(self, env, tmp_path, no_side_effects):
        root = tmp_path / "root"
        root.mkdir()
        env.manual(root)
        assert main_module.main(["--check"]) == 0
        assert os.listdir(root) == []

    @pytest.mark.parametrize(
        "breakage",
        [
            "no_state_directory",
            "relative_state_directory",
            "no_credential",
            "credential_without_password",
            "no_config_file",
            "invalid_config_file",
            "config_without_searches",
        ],
    )
    def test_broken_environment_exits_78(self, env, monkeypatch, no_side_effects, breakage):
        env.service().credential()
        if breakage == "no_state_directory":
            monkeypatch.delenv("STATE_DIRECTORY")
        elif breakage == "relative_state_directory":
            monkeypatch.setenv("STATE_DIRECTORY", "state")
        elif breakage == "no_credential":
            (env.creds / "smtp").unlink()
        elif breakage == "credential_without_password":
            env.credential(GOOD_CREDENTIAL.replace("SENDER_PASSWORD=", "#SENDER_PASSWORD="))
        elif breakage == "no_config_file":
            env.config_file.unlink()
        elif breakage == "invalid_config_file":
            env.config_file.write_text("{not json")
        elif breakage == "config_without_searches":
            env.config_file.write_text('{"check_interval": 60}')
        assert main_module.main(["--check"]) == 78
        assert os.listdir(env.state) == []

    def test_a_real_start_runs_the_same_gates(self, env, monkeypatch, no_side_effects):
        env.service().credential()
        monkeypatch.delenv("STATE_DIRECTORY")
        assert main_module.main([]) == 78

    def test_every_failing_gate_is_reported_and_no_value_is_shown(
        self, env, monkeypatch, no_side_effects, capsys
    ):
        env.service().credential(GOOD_CREDENTIAL.replace("RECIPIENT_EMAIL=", "#"))
        monkeypatch.delenv("STATE_DIRECTORY")
        env.config_file.unlink()
        assert main_module.main(["--check"]) == 78
        err = capsys.readouterr().err
        assert "STATE_DIRECTORY" in err
        assert "RECIPIENT_EMAIL" in err
        assert "beállítás-fájl" in err
        assert PASSWORD not in err and "sender@example.org" not in err

    def test_the_command_line_exit_code_is_78(self, tmp_path):
        """The real `python3 -m product_monitor.main --check` process, in
        service mode with nothing provided."""
        result = run_python(tmp_path, "-m", "product_monitor.main", "--check")
        assert result.returncode == 78, result.stderr
        assert "STATE_DIRECTORY" in result.stderr


class TestImportTime:
    def test_importing_runs_no_gate(self, tmp_path):
        """Service mode with no state directory or credential: importing the
        package (as the tests and scripts/ do) must still succeed - the gates run
        at startup only. -s: no user site-packages, as for a home-less user."""
        result = run_python(tmp_path, "-s", "-c", "import product_monitor.main, product_monitor")
        assert result.returncode == 0, result.stderr


def run_python(cwd, *args):
    environment = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
    environment["INVOCATION_ID"] = "0123456789abcdef"
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
