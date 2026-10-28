"""Behavioral tests for the rot-token advisory leg of
`coordinator_core.write_guards.guard_doctrine_surface_edits.check`.

Spec: docs/plans/2026-09-26-inbox-blitz-part-b-engine-defects.md, row B06.

An edit to a protected doctrine surface lands with the surface advisory; when
the edit nets in a date-, version-, or SHA-shaped token the old text lacked,
the same `additionalContext` also carries the rot-token advisory. Never a deny.
"""

from __future__ import annotations

from coordinator_core.write_guards import guard_doctrine_surface_edits as guard


def _make_repo(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _no_gate(monkeypatch) -> None:
    """Neutralize the repo-identity advisory addendum -- unrelated to this
    leg, and would otherwise spawn git via `compute_repo_identity_gate`.
    """
    monkeypatch.setattr(
        guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "UNRESOLVED", "message": ""}
    )


def _edit_payload(file_path, old_string, new_string, session_id="sess-b06"):
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(file_path),
            "old_string": old_string,
            "new_string": new_string,
        },
        "session_id": session_id,
    }


def _context(result) -> str:
    assert result is not None
    assert "permissionDecision" not in result["hookSpecificOutput"]
    return result["hookSpecificOutput"]["additionalContext"]


def _protected(tmp_path, monkeypatch, text):
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    _no_gate(monkeypatch)
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))
    protected = repo_root / "CLAUDE.md"
    protected.write_text(text, encoding="utf-8")
    return repo_root, protected


def test_edit_adding_date_advises(tmp_path, monkeypatch):
    _root, protected = _protected(tmp_path, monkeypatch, "Doctrine. Old sentence.\n")
    result = guard.check(
        _edit_payload(protected, "Old sentence.", "New sentence, ratified 2026-09-26.")
    )
    context = _context(result)
    assert "2026-09-26" in context
    assert "always-loaded doctrine" in context


def test_edit_moving_existing_sha_carries_only_the_surface_advisory(tmp_path, monkeypatch):
    _root, protected = _protected(tmp_path, monkeypatch, "Doctrine. See abc1234 for detail.\n")
    # `abc1234` is in BOTH old_string and new_string -- moved, not added.
    result = guard.check(
        _edit_payload(protected, "See abc1234 for detail.", "See abc1234 in its new home.")
    )
    context = _context(result)
    assert "abc1234" not in context
    assert "always-loaded doctrine" in context


def test_non_protected_path_never_advises(tmp_path, monkeypatch):
    repo_root, _protected_path = _protected(tmp_path, monkeypatch, "Doctrine.\n")
    unprotected = repo_root / "some_file.py"
    unprotected.write_text("x = 1\n", encoding="utf-8")
    result = guard.check(_edit_payload(unprotected, "x = 1", "x = 1  # ratified 2026-09-26"))
    assert result is None


def test_plain_numeral_adds_no_rot_advisory(tmp_path, monkeypatch):
    _root, protected = _protected(tmp_path, monkeypatch, "Doctrine. There are two legs.\n")
    result = guard.check(
        _edit_payload(protected, "There are two legs.", "There are 42 legs, or 3, depending.")
    )
    assert "date-, version-" not in _context(result)


def test_guard_exposes_no_sentinel_reader():
    assert not hasattr(guard, "_sentinel_state")
    assert not hasattr(guard, "pm_approval_fresh")
