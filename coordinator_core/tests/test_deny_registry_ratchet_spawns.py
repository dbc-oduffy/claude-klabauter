"""Ratchet: nothing denies recoverable work unless deny_registry names its irreversible class."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from coordinator_core import deny_registry as dr
from coordinator_core import machine_profile as mp

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ROOT = Path(dr.__file__).resolve().parent
# Discovery covers only modules declaring CLASS = "hard-deny"; a deny emitted elsewhere is
# held by the apply_guard_level clamp tests below, not by this scan.
_CLASS_HARD_DENY = re.compile(r'^CLASS\s*=\s*["\']hard-deny["\']', re.M)


@pytest.fixture(autouse=True)
def _empty_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    d = tmp_path / "reg"
    d.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    for key in list(os.environ):
        if key.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(key)
    mp.reset_cache()
    yield
    mp.reset_cache()


@pytest.mark.parametrize("cmd", ["python -m pytest $FILES", "pytest $F -q", "python -m pytest $(cat list.txt)"])
def test_unscheduled_suite_run_with_variable_held_paths_denies_at_every_level(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cmd) -> None:
    import subprocess

    from coordinator_core.bash_guards import check_test_suite_invocation as guard
    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags())
    assert dr.GUARD_DENY_CLASS["check-test-suite-invocation"] == dr.BOX_DEGRADING
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s", "cwd": str(tmp_path)}
    env = guard.check(payload)
    assert env is not None
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        mp.reset_cache()
        assert mp.apply_guard_level("check-test-suite-invocation", env) is env, level
