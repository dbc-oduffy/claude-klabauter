"""The hand-run `disk-budget-sweep.py` CLI: dry-run default, unbounded apply, optional wall cap.

The CLI lives in DoE (`coordinator/bin`), not in claude-klabauter; the module skips where it is absent.
The end-to-end tests that load the real session-start hook stay in DoE beside that hook.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CLI = _ROOT / "bin" / "disk-budget-sweep.py"

pytestmark = pytest.mark.skipif(not _CLI.is_file(), reason="disk-budget-sweep.py ships from DoE, absent here")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


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


def test_cli_has_no_install_verb(stub):
    cli, _hook = stub
    with pytest.raises(SystemExit):
        cli.main(["install"])
    with pytest.raises(SystemExit):
        cli.main(["--install"])
