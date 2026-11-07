"""scripts/check_pinned_deps.py: the requirements.txt pins vs. what is installed.

The pins follow the Pi's apt packages (production runs the system Python, no
venv), and this check is what notices when the two drift apart, e.g. after an
apt upgrade. A check that quietly passes is worse than none, so most of these
tests are about it failing when it should.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_pinned_deps.py"

# scripts/ isn't a package, and the script must stay importable without one.
_spec = importlib.util.spec_from_file_location("check_pinned_deps", SCRIPT)
assert _spec is not None and _spec.loader is not None
cpd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpd)

CURRENT_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
PYTEST_VERSION = pytest.__version__


def lookup_from(installed):
    return lambda name: installed.get(cpd.normalize(name))


class TestParsePins:
    def test_pins_comments_and_blank_lines(self):
        text = "# header\n\naiohttp==3.13.3\nlru-dict==1.4.1  # inline comment\n"
        assert cpd.parse_pins(text) == {"aiohttp": "3.13.3", "lru-dict": "1.4.1"}

    def test_keeps_file_order(self):
        assert list(cpd.parse_pins("b==1\na==2\n")) == ["b", "a"]

    @pytest.mark.parametrize(
        "line",
        [
            "aiohttp>=3.13.3",
            "aiohttp~=3.13",
            "aiohttp",
            "aiohttp==3.13.3; python_version < '3.15'",
            "aiohttp[speedups]==3.13.3",
            "-r requirements-dev.txt",
            "aiohttp==3.13.3,<4",
            "aiohttp===3.13.3",
        ],
    )
    def test_refuses_anything_but_an_exact_pin(self, line):
        with pytest.raises(ValueError, match="line 2"):
            cpd.parse_pins(f"yarl==1.22.0\n{line}\n")

    def test_refuses_a_duplicate_even_with_another_spelling(self):
        with pytest.raises(ValueError, match="already pinned on line 1"):
            cpd.parse_pins("typing_extensions==4.15.0\nTyping-Extensions==4.15.0\n")


class TestPythonVersion:
    def test_first_non_comment_line(self):
        assert cpd.parse_python_version("# CI and the Pi\n\n3.14\n") == "3.14"

    @pytest.mark.parametrize("text", ["", "# only a comment\n", "python3.14\n", "3\n", "3.x\n"])
    def test_refuses_a_missing_or_malformed_version(self, text):
        with pytest.raises(ValueError):
            cpd.parse_python_version(text)

    def test_minor_version_matches_any_micro(self):
        assert cpd.python_matches("3.14", (3, 14, 4, "final", 0))
        assert cpd.python_matches("3.14", (3, 14, 0, "final", 0))

    def test_other_minor_or_major_does_not_match(self):
        assert not cpd.python_matches("3.14", (3, 13, 9, "final", 0))
        assert not cpd.python_matches("3.14", (3, 15, 0, "final", 0))
        assert not cpd.python_matches("3.14", (4, 14, 0, "final", 0))

    def test_micro_version_must_match_exactly(self):
        assert cpd.python_matches("3.14.4", (3, 14, 4, "final", 0))
        assert not cpd.python_matches("3.14.4", (3, 14, 5, "final", 0))


class TestCheckPins:
    def test_all_match(self):
        rows = cpd.check_pins({"aiohttp": "3.13.3"}, lookup_from({"aiohttp": "3.13.3"}))
        assert [row.ok for row in rows] == [True]

    def test_a_different_version_is_drift(self):
        rows = cpd.check_pins({"aiohttp": "3.13.3"}, lookup_from({"aiohttp": "3.13.4"}))
        assert rows == [cpd.Row("aiohttp", "3.13.3", "3.13.4")]
        assert not rows[0].ok

    def test_a_missing_package_is_drift(self):
        rows = cpd.check_pins({"psutil": "7.1.0"}, lookup_from({}))
        assert rows[0].installed is None
        assert not rows[0].ok

    def test_compares_whole_versions_not_prefixes(self):
        rows = cpd.check_pins({"yarl": "1.2"}, lookup_from({"yarl": "1.22.0"}))
        assert not rows[0].ok

    def test_real_lookup_finds_an_installed_distribution(self):
        assert cpd.installed_version("pytest") == PYTEST_VERSION
        assert cpd.installed_version("PyTest") == PYTEST_VERSION
        assert cpd.installed_version("no-such-distribution-xyz") is None

    def test_report_shows_every_package_and_its_status(self):
        rows = [
            cpd.Row("aiohttp", "3.13.3", "3.13.3"),
            cpd.Row("yarl", "1.22.0", "1.25.1"),
            cpd.Row("psutil", "7.1.0", None),
        ]
        report = cpd.format_report(rows, "3.14", (3, 13, 9, "final", 0), "/usr/bin/python3")
        assert "Python 3.13.9 (/usr/bin/python3), expected 3.14: MISMATCH" in report
        lines = report.splitlines()
        assert any(line.split() == ["aiohttp", "3.13.3", "3.13.3", "OK"] for line in lines)
        assert any(line.split() == ["yarl", "1.22.0", "1.25.1", "DIFFERS"] for line in lines)
        assert any(line.split() == ["psutil", "7.1.0", "(missing)", "MISSING"] for line in lines)


class TestMain:
    """The real script as a process, against small requirements files of our own."""

    def run(self, tmp_path, requirements, python_version=CURRENT_PYTHON + "\n"):
        req = tmp_path / "requirements.txt"
        req.write_text(requirements, encoding="utf-8")
        pyver = tmp_path / ".python-version"
        pyver.write_text(python_version, encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--requirements",
                str(req),
                "--python-version-file",
                str(pyver),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_exit_0_when_everything_matches(self, tmp_path):
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "All 1 pins match" in result.stdout

    def test_exit_1_on_a_wrong_version(self, tmp_path):
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}.1\n")
        assert result.returncode == 1
        assert "DIFFERS" in result.stdout
        assert "DRIFT: 1 of 1 packages differ" in result.stdout

    def test_exit_1_on_a_missing_package(self, tmp_path):
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}\nno-such-distribution-xyz==1.0\n")
        assert result.returncode == 1
        assert "MISSING" in result.stdout

    def test_exit_1_on_another_python_version(self, tmp_path):
        other = f"{sys.version_info.major}.{sys.version_info.minor + 1}\n"
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}\n", python_version=other)
        assert result.returncode == 1
        assert "MISMATCH" in result.stdout
        assert "Python is not" in result.stdout

    @pytest.mark.parametrize("requirements", ["pytest>=1.0\n", "", "# nothing pinned\n"])
    def test_exit_2_on_requirements_it_cannot_judge(self, tmp_path, requirements):
        result = self.run(tmp_path, requirements)
        assert result.returncode == 2
        assert result.stderr.startswith("ERROR:")

    def test_exit_2_on_a_missing_file(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--requirements", str(tmp_path / "nope.txt")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 2

    def test_the_repo_files_parse(self):
        pins = cpd.parse_pins(cpd.DEFAULT_REQUIREMENTS.read_text(encoding="utf-8"))
        assert {"aiohttp", "beautifulsoup4", "lru-dict", "aiofiles", "psutil"} <= set(pins)
        cpd.parse_python_version(cpd.DEFAULT_PYTHON_VERSION_FILE.read_text(encoding="utf-8"))

    def test_pyproject_pins_the_same_runtime_versions(self):
        import tomllib

        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = pyproject["project"]
        declared = project["dependencies"] + project["optional-dependencies"]["memory-logging"]
        pins = cpd.parse_pins(cpd.DEFAULT_REQUIREMENTS.read_text(encoding="utf-8"))
        for requirement in declared:
            name, version = requirement.split("==")
            assert pins.get(name) == version, requirement
