"""Interpreter-path reading of `/x/...` literals in the drive-root new-entry Bash guard."""

from __future__ import annotations

import os

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import _write_bump_sink_shapes as sinks
from coordinator_core.bash_guards import block_drive_root_new_dir_bash_write as guard

INCIDENT = (
    "cd /f/dev/research/example-retrieval-repo/scip-clang && python3 - <<'EOF'\n"
    "import os\n"
    "os.makedirs('/e/example-retrieval-repo/scratch/warp/x')\n"
    "open('/e/example-retrieval-repo/scratch/warp/x/repomap.md','w')\n"
    "EOF"
)


@pytest.fixture
def fake_fs(monkeypatch):
    """Windows host with an in-memory set of existing drive-root entries."""
    existing = {"e:\\", "f:\\", "e:\\example-retrieval-repo", "e:\\claude-klabauter", "f:\\dev"}  # abs-path-ok: in-memory fake drive table
    real = os.path.lexists

    def fake_lexists(p):
        key = str(p).lower().rstrip("\\")
        if len(key) >= 2 and key[1] == ":":
            return key in existing or key + "\\" in existing
        return real(p)

    monkeypatch.setattr(sinks, "_host_is_windows", lambda: True)
    monkeypatch.setattr(os.path, "lexists", fake_lexists)
    monkeypatch.delenv("COORDINATOR_DRIVE_ROOTS", raising=False)
    monkeypatch.delenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", raising=False)
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    machine_profile.reset_cache()
    yield existing
    machine_profile.reset_cache()


def _payload(cmd, cwd, tool="Bash"):
    return {"tool_name": tool, "tool_input": {"command": cmd}, "cwd": cwd, "env": {}}


def test_incident_command_is_denied(fake_fs):
    res = guard.check(_payload(INCIDENT, "F:\\dev"))
    assert res is not None
    out = res["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "'e'" in out["permissionDecisionReason"]
    assert "<repo>/scratch/" in out["permissionDecisionReason"]


def test_incident_denied_when_cd_is_the_only_drive_change(fake_fs):
    assert guard.check(_payload(INCIDENT, "E:\\claude-klabauter")) is not None


def test_same_command_from_target_drive_is_allowed(fake_fs):
    cmd = INCIDENT.replace("cd /f/dev/research/example-retrieval-repo/scip-clang", "cd /e/claude-klabauter")
    assert guard.check(_payload(cmd, "E:\\claude-klabauter")) is None


def test_bash_only_mkdir_uses_msys_reading(fake_fs):
    assert guard.check(_payload("mkdir -p /e/example-retrieval-repo/x", "F:\\dev")) is None
    assert guard.check(_payload("mkdir -p /e/brand-new/x", "F:\\dev")) is not None
