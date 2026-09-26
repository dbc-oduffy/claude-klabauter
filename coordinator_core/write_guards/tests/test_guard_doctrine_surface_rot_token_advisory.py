"""Behavioral tests for the B06 rot-token advisory leg of
`coordinator_core.write_guards.guard_doctrine_surface_edits.check`.

Spec: docs/plans/2026-09-26-inbox-blitz-part-b-engine-defects.md, row B06.

On an ALLOWED edit (approval sentinel present and unexpired) to a protected
doctrine surface, `check()` returns an `additionalContext`-only advisory
when the edit nets in a date-, version-, or SHA-shaped token the old text
lacked. It never denies for this reason -- the allow decision is unchanged;
only the return value gains an advisory payload.
"""

from __future__ import annotations

import time

from coordinator_core.write_guards import guard_doctrine_surface_edits as guard

_SENTINEL_NAME = guard._SENTINEL_NAME


def _make_repo(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _approve(repo_root) -> None:
    sentinel = repo_root / _SENTINEL_NAME
    sentinel.write_text("", encoding="utf-8")


def _no_gate(monkeypatch) -> None:
    """Neutralize the C4 repo-identity advisory addendum -- unrelated to
    this leg, and would otherwise spawn git via `compute_repo_identity_gate`.
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


def test_allowed_edit_adding_date_advises(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    _approve(repo_root)
    _no_gate(monkeypatch)
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))

    protected = repo_root / "CLAUDE.md"
    protected.write_text("Doctrine. Old sentence.\n", encoding="utf-8")

    payload = _edit_payload(
        protected,
        "Old sentence.",
        "New sentence, ratified 2026-09-26.",
    )
    result = guard.check(payload)

    assert result is not None
    assert "permissionDecision" not in result["hookSpecificOutput"]
    assert "2026-09-26" in result["hookSpecificOutput"]["additionalContext"]


def test_allowed_edit_moving_existing_sha_does_not_advise(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    _approve(repo_root)
    _no_gate(monkeypatch)
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))

    protected = repo_root / "CLAUDE.md"
    protected.write_text("Doctrine. See abc1234 for detail.\n", encoding="utf-8")

    # The SHA `abc1234` is present in BOTH old_string and new_string -- moved,
    # not added -- so no token is net-added.
    payload = _edit_payload(
        protected,
        "See abc1234 for detail.",
        "See abc1234 in its new home.",
    )
    result = guard.check(payload)

    assert result is None


def test_denied_edit_is_unchanged(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    # No sentinel -> deny-absent. The advisory leg must never run for a
    # denied edit; the deny reason stays the doctrine-surface message.
    _no_gate(monkeypatch)
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))

    protected = repo_root / "CLAUDE.md"
    protected.write_text("Doctrine. Old sentence.\n", encoding="utf-8")

    payload = _edit_payload(
        protected,
        "Old sentence.",
        "New sentence, ratified 2026-09-26.",
    )
    result = guard.check(payload)

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "doctrine" in reason.lower()
    assert "2026-09-26" not in reason


def test_non_protected_path_never_advises(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    _approve(repo_root)
    _no_gate(monkeypatch)
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))

    unprotected = repo_root / "some_file.py"
    unprotected.write_text("x = 1\n", encoding="utf-8")

    payload = _edit_payload(
        unprotected,
        "x = 1",
        "x = 1  # ratified 2026-09-26",
    )
    result = guard.check(payload)

    assert result is None


def test_plain_numeral_does_not_advise(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    _approve(repo_root)
    _no_gate(monkeypatch)
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))

    protected = repo_root / "CLAUDE.md"
    protected.write_text("Doctrine. There are two legs.\n", encoding="utf-8")

    payload = _edit_payload(
        protected,
        "There are two legs.",
        "There are 42 legs, or 3, depending.",
    )
    result = guard.check(payload)

    assert result is None
