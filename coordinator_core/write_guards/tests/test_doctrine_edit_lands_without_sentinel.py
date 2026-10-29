"""An EM or engine run editing a doctrine surface lands with no sentinel and gets an advisory, never a deny."""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import _gem_doctrine_carve_out as co
from coordinator_core.write_guards import guard_doctrine_surface_edits as guard

_SURFACES = [
    ("coordinator", "snippets", "em-operating-doctrine.md"),
    ("global-doctrine", "CLAUDE.md"),
]


@pytest.fixture
def doe(tmp_path, monkeypatch):
    root = tmp_path / "coordinator-content-repo"
    for parts in _SURFACES:
        (root.joinpath(*parts)).parent.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    session_repo = tmp_path / "project-peer"
    (session_repo / ".git").mkdir(parents=True)
    guard._content_root_memo.clear()
    monkeypatch.setattr(guard, "read_content_root", lambda: str(root))
    monkeypatch.setattr(guard, "_git_root", lambda: str(session_repo))
    monkeypatch.setattr(guard, "gem_admitted", lambda *a, **k: False)
    monkeypatch.setattr(co, "read_content_root", lambda: str(root))
    monkeypatch.setattr(
        guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "MATCH", "message": ""}
    )
    monkeypatch.setattr(guard, "_write_repo_identity_advisory_log", lambda *a, **k: None)
    yield root
    guard._content_root_memo.clear()


def _envelope(target, **extra):
    """The default PreToolUse envelope: no transcript, no approval state of any kind."""
    return {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "tool_input": {"file_path": str(target), "old_string": "a", "new_string": "b"},
        **extra,
    }


@pytest.mark.parametrize("parts", _SURFACES)
@pytest.mark.parametrize("audience", [{}, {"agent_id": "engine-run-1", "agent_type": "executor"}])
def test_doctrine_edit_lands_with_an_advisory_and_no_sentinel(doe, parts, audience):
    target = doe.joinpath(*parts)
    result = guard.check(_envelope(target, **audience))
    assert result is not None
    out = result["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert "advisory" in out["additionalContext"]
    assert "byte budget" in out["additionalContext"]


def test_unprotected_path_in_a_protected_directory_passes_silently(doe):
    result = guard.check(_envelope(doe / "global-doctrine" / "notes.md"))
    assert result is None


def test_guard_is_advisory_class_and_reads_no_sentinel(doe):
    assert guard.CLASS == "advisory"
    assert not hasattr(guard, "_sentinel_state")
    assert not hasattr(guard, "_SENTINEL_NAME")
