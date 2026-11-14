"""Growth advisory for always-loaded surfaces listed in the content root's byte baseline.

An Edit/Write that grows a listed surface lands with an advisory; an equal-size or
shrinking edit, an unlisted file and a missing baseline emit nothing. Never a deny.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.write_guards import guard_doctrine_surface_edits as guard

_SURFACE = "coordinator/snippets/surface.md"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "coordinator" / "snippets").mkdir(parents=True)
    monkeypatch.setattr(guard, "_git_root", lambda: str(root))
    monkeypatch.setattr(
        guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "UNRESOLVED", "message": ""}
    )
    return root


def _baseline(root, ceilings=None):
    path = root / "coordinator" / "tests" / "baselines" / "always-loaded-surface-bytes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ceilings": ceilings or {_SURFACE: 10}}), encoding="utf-8")


def _surface(root, text="0123456789"):
    path = root / _SURFACE
    path.write_text(text, encoding="utf-8")
    return path


def _edit(path, old, new):
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": old, "new_string": new},
        "session_id": "s",
    }


def _write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}, "session_id": "s"}


def test_growing_edit_emits_one_advisory_and_no_deny(repo):
    _baseline(repo)
    path = _surface(repo)

    result = guard.check(_edit(path, "9", "9999"))

    out = result["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert out["additionalContext"].count("grows always-loaded surface") == 1
    assert _SURFACE in out["additionalContext"]
    assert "hook, skill or wiki page" in out["additionalContext"]


def test_growing_write_emits_advisory(repo):
    _baseline(repo)
    path = _surface(repo)

    result = guard.check(_write(path, "0123456789abc"))

    assert "10 -> 13 bytes" in result["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("new", ["abcdefghij", "012"])
def test_equal_or_shrinking_write_emits_nothing(repo, new):
    _baseline(repo)
    path = _surface(repo)

    assert guard.check(_write(path, new)) is None


def test_unlisted_file_emits_nothing(repo):
    _baseline(repo)
    other = repo / "coordinator" / "snippets" / "other.md"
    other.write_text("x", encoding="utf-8")

    assert guard.check(_write(other, "x" * 500)) is None


def test_missing_baseline_is_no_advisory_and_no_crash(repo):
    path = _surface(repo)

    assert guard.check(_write(path, "x" * 500)) is None


def test_malformed_baseline_is_no_advisory(repo):
    path = repo / "coordinator" / "tests" / "baselines" / "always-loaded-surface-bytes.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    surface = _surface(repo)

    assert guard.check(_write(surface, "x" * 500)) is None
