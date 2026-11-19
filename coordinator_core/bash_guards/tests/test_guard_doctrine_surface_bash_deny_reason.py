"""The Bash-plane class-write deny names the absolute git dir and the human ``!`` touch; the dispatched deny does not."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.write_guards._doctrine_edit_sentinel import SENTINEL_BASENAME

COMMAND = "echo x >> CLAUDE.md"


@pytest.fixture(autouse=True)
def _gate_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", raising=False)
    machine_profile.reset_cache()
    yield
    machine_profile.reset_cache()


def _reason(cwd: Path, **extra) -> str:
    (cwd / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
    payload = {"tool_name": "Bash", "tool_input": {"command": COMMAND}, "cwd": str(cwd), **extra}
    result = envelope_of(guard.check(payload, None))
    hso = result["hookSpecificOutput"]
    assert hso.get("permissionDecision") == "deny"
    return hso["permissionDecisionReason"]


def test_em_class_write_deny_names_git_dir_and_human_touch(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    reason = _reason(tmp_path)
    marker = (git_dir / SENTINEL_BASENAME).resolve().as_posix()
    assert f'! touch "{marker}"' in reason.replace("\\", "/")


def test_dispatched_class_write_deny_stays_without_touch(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    reason = _reason(tmp_path, agent_id="a1")
    assert "! touch" not in reason
    assert "Report the change you need" in reason
