"""Behavior pins for the unimatrix control key's forced command: `launch` and `wake` only."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

CONTROL = Path(__file__).resolve().parents[1] / "unimatrix-control.py"


def run_main(cmd, tmp_path, argv=()):
    env = {"COORDINATOR_SETTINGS_HOME": str(tmp_path), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    if cmd is not None:
        env["SSH_ORIGINAL_COMMAND"] = cmd
    return subprocess.run([sys.executable, str(CONTROL), *argv], capture_output=True, text=True, timeout=30,
                          env=env, stdin=subprocess.DEVNULL)


def test_main_argv_form_runs_without_ssh_original_command(tmp_path):
    r = run_main(None, tmp_path, argv=["launch", "nosuchrepo"])
    assert r.returncode == 1
    assert json.loads(r.stdout) == {"ok": False, "error": "unknown repo key nosuchrepo"}


def test_main_argv_wins_over_ssh_original_command(tmp_path):
    r = run_main("roster", tmp_path, argv=["launch", "nosuchrepo"])
    assert json.loads(r.stdout)["error"] == "unknown repo key nosuchrepo"


@pytest.mark.parametrize("argv", [["roster", "x"], ["launch", "/abs"], ["launch", "a b"], ["wake", "a;id"]])
def test_main_argv_refuses_other_modes_with_usage(argv, tmp_path):
    r = run_main(None, tmp_path, argv=argv)
    assert r.returncode != 0 and r.stdout == "" and "usage:" in r.stderr


def test_main_explicit_prefixed_ssh_original_command_reaches_the_launch_path(tmp_path):
    r = run_main('"/p/python3" "/h/unimatrix/unimatrix-control.py" launch nosuchrepo', tmp_path)
    assert json.loads(r.stdout) == {"ok": False, "error": "unknown repo key nosuchrepo"}


def test_main_no_argv_and_no_original_command_is_usage(tmp_path):
    r = run_main(None, tmp_path)
    assert r.returncode != 0 and "usage:" in r.stderr


@pytest.mark.parametrize("cmd", ["roster", "send 1", "launch /x", "put drop", "launch a b"])
def test_main_refuses_other_modes_with_usage(cmd, tmp_path):
    r = run_main(cmd, tmp_path)
    assert r.returncode != 0 and r.stdout == "" and "usage:" in r.stderr


def test_main_unknown_repo_key_replies_json_error_and_exits_nonzero(tmp_path):
    r = run_main("launch nosuchrepo", tmp_path)
    assert r.returncode == 1
    assert json.loads(r.stdout) == {"ok": False, "error": "unknown repo key nosuchrepo"}
