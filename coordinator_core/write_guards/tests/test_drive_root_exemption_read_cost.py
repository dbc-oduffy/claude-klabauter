"""The session-exemption read (registry + holder liveness) runs only when a target would flag."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_drive_root_new_dir_bash_write as bash_guard
from coordinator_core.write_guards import _drive_root, block_drive_root_new_dir_write as write_guard


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "existing").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOTS", str(root))
    monkeypatch.delenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", raising=False)
    calls = []
    monkeypatch.setattr(_drive_root, "_cwd_is_exempt_session", lambda cwd: calls.append(cwd) or False)
    return root, calls


def _write(root, leaf):
    return {"tool_name": "Write", "agent_id": "a", "cwd": str(root / "existing"),
            "tool_input": {"file_path": str(root / leaf / "x.py")}}


def test_clean_write_skips_exemption_read(world):
    root, calls = world
    assert write_guard.check(_write(root, "existing")) is None
    assert calls == []


def test_clean_bash_skips_exemption_read(world):
    root, calls = world
    p = {"tool_name": "Bash", "agent_id": "a", "cwd": str(root / "existing"),
         "tool_input": {"command": f"git status && ls {root / 'existing'}"}}
    assert bash_guard.check(p) is None
    assert calls == []


def test_flagging_write_still_denied_and_reads_exemption_once(world):
    root, calls = world
    assert write_guard.check(_write(root, "newdir")) is not None
    assert len(calls) == 1


def test_exempt_session_still_not_denied(world, monkeypatch):
    root, calls = world
    monkeypatch.setattr(_drive_root, "_cwd_is_exempt_session", lambda cwd: True)
    assert write_guard.check(_write(root, "newdir")) is None
