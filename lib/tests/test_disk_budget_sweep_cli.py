"""The hand-run `disk-budget-sweep.py` CLI: dry-run default, unbounded apply, optional wall cap.

The CLI lives in DoE (`coordinator/bin`), not in claude-klabauter; the module skips where it is absent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CLI = _ROOT / "bin" / "disk-budget-sweep.py"
_OLD = time.time() - 3 * 86400

pytestmark = pytest.mark.skipif(not _CLI.is_file(), reason="disk-budget-sweep.py ships from DoE, absent here")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("XDG_CACHE_HOME", "UV_CACHE_DIR", "HF_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COORDINATOR_DISK_BUDGET_PROJECTS_CAP", "5000")
    agent = home / ".claude" / "projects" / "-repo" / "live" / "subagents" / "agent-a.jsonl"
    agent.parent.mkdir(parents=True)
    agent.write_bytes(b"x" * 20000)
    os.utime(agent, (_OLD, _OLD))
    return {"home": home, "agent": agent, "state": home / ".claude" / "disk-budget"}


class _Hook:
    def __init__(self):
        self.seen = {}

    def run_sweep(self, **kwargs):
        self.seen = kwargs
        return {"freed_bytes": 0, "per_root": {}, "log": [], "still_over": False}


@pytest.fixture
def stub(monkeypatch):
    cli = _load("disk_budget_sweep_cli_stub_under_test", _CLI)
    hook = _Hook()
    monkeypatch.setattr(cli, "_load_hook", lambda: hook)
    return cli, hook


def test_dry_run_deletes_nothing_and_writes_no_log(box, capsys):
    cli = _load("disk_budget_sweep_cli_dry_under_test", _CLI)
    assert cli.main(["--dry-run"]) == 0
    assert "would free" in capsys.readouterr().out
    assert box["agent"].exists()
    assert not (box["state"] / "sweeps.jsonl").exists()
    assert not (box["state"] / "last-sweep.json").exists()


def test_apply_deletes_and_logs(box, capsys):
    cli = _load("disk_budget_sweep_cli_apply_under_test", _CLI)
    assert cli.main(["--apply", "--ignore-throttle"]) == 0
    assert "freed" in capsys.readouterr().out
    assert not box["agent"].exists()
    assert json.loads((box["state"] / "sweeps.jsonl").read_text().splitlines()[-1])["freed_bytes"] > 0


def test_apply_passes_unbounded_deadline(stub):
    cli, hook = stub
    assert cli.main(["--apply"]) == 0
    assert hook.seen["apply"] is True and hook.seen["deadline_s"] is None


def test_dry_run_keeps_the_policy_deadline(stub):
    cli, hook = stub
    assert cli.main([]) == 0
    assert hook.seen["apply"] is False and "deadline_s" not in hook.seen


def test_max_wall_s_caps_the_sweep(stub):
    cli, hook = stub
    assert cli.main(["--apply", "--max-wall-s", "7"]) == 0
    assert hook.seen["deadline_s"] == 7


def test_throttled_apply_reports_throttled(box, capsys):
    cli = _load("disk_budget_sweep_cli_throttle_under_test", _CLI)
    assert cli.main(["--apply"]) == 0
    capsys.readouterr()
    assert cli.main(["--apply"]) == 0
    assert "throttled" in capsys.readouterr().out


def test_cli_has_no_install_verb(stub):
    cli, _hook = stub
    with pytest.raises(SystemExit):
        cli.main(["install"])
    with pytest.raises(SystemExit):
        cli.main(["--install"])
