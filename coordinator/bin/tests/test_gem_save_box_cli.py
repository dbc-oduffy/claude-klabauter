"""gem-save-box forwarder: refuses without live G-EM standing and runs nothing; with standing it runs
the group-em tool script with argv verbatim and returns its exit code; an absent script is named."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

TOOLS_DIR = Path(__file__).resolve().parents[1]
SCRIPT = TOOLS_DIR / "gem-save-box.py"
_STUB = (
    "import json, sys, pathlib\n"
    "pathlib.Path(__file__).with_name('ran.json').write_text(json.dumps(sys.argv[1:]))\n"
    "sys.exit(7)\n"
)


def _lib():
    sys.path.insert(0, str(TOOLS_DIR / "lib"))
    try:
        import gem_save_box

        return gem_save_box
    finally:
        sys.path.remove(str(TOOLS_DIR / "lib"))


def _run(tmp_path: Path, *args: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def stub_home(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    tool = home / "group-em" / "tools" / "gem-save-box"
    tool.mkdir(parents=True)
    (tool / "gem_save_box.py").write_text(_STUB, encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return tool


def test_help_exits_zero_and_skips_the_standing_check(tmp_path):
    result = _run(tmp_path, "--help")
    assert result.returncode == 0 and "kill --pid" in result.stdout


@pytest.mark.parametrize(
    "argv", [["survey", "--help"], ["kill", "--pid", "4242", "--reason", "box-saturating"]]
)
def test_standing_forwards_argv_verbatim_and_returns_the_exit_code(stub_home, argv):
    code = _lib().run(argv, standing=lambda cwd: True, err=io.StringIO())
    assert code == 7
    assert (stub_home / "ran.json").read_text(encoding="utf-8") == json.dumps(argv)


def test_no_standing_exits_2_and_never_runs_the_script(stub_home):
    err = io.StringIO()
    assert _lib().run(["kill", "--pid", "4242", "--reason", "x"], standing=lambda cwd: False, err=err) == 2
    assert "not the live G-EM" in err.getvalue()
    assert not (stub_home / "ran.json").exists()


def test_no_standing_through_the_real_entry_script(stub_home, tmp_path):
    result = _run(tmp_path, "survey", "--help")
    assert result.returncode == 2
    assert not (stub_home / "ran.json").exists()


def test_absent_script_exits_2_naming_the_path(tmp_path, monkeypatch):
    home = tmp_path / "empty-settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    err = io.StringIO()
    assert _lib().run(["survey"], standing=lambda cwd: True, err=err) == 2
    assert str(home / "group-em" / "tools" / "gem-save-box" / "gem_save_box.py") in err.getvalue()


def test_a_standing_check_failure_names_its_cause_not_non_holder(stub_home):
    def boom(cwd):
        raise ImportError("no session core")

    err = io.StringIO()
    assert _lib().run(["survey"], standing=boom, err=err) == 2
    assert "standing check failed: ImportError: no session core" in err.getvalue()
    assert "not the live G-EM" not in err.getvalue()
    assert not (stub_home / "ran.json").exists()


def test_live_gem_check_delegates_to_the_nomination_read(monkeypatch, tmp_path):
    from coordinator_core.bash_guards import _gem_cross_repo_commit as gcc

    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-gem")
    seen = []
    monkeypatch.setattr(
        gcc, "session_holds_live_gem_standing", lambda sid, root: seen.append((sid, root)) or sid == "sess-gem"
    )
    assert _lib()._live_gem(str(tmp_path)) is True
    assert seen == [("sess-gem", str(tmp_path))]
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-other")
    assert _lib()._live_gem(str(tmp_path)) is False
