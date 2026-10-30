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


_RULE = "enforce the rule in code or a tripwire, not doctrine prose"


def test_em_edit_of_any_claude_md_carries_the_strongest_warning(tmp_path, monkeypatch):
    _root, protected = _protected(tmp_path, monkeypatch, "Doctrine. Old sentence.\n")
    result = guard.check(_edit_payload(protected, "Old sentence.", "Better sentence."))
    context = _context(result)
    assert _RULE in context
    assert "changelog-shaped" not in context


def test_dated_heading_addition_is_louder(tmp_path, monkeypatch):
    _root, protected = _protected(tmp_path, monkeypatch, "Doctrine.\n")
    result = guard.check(
        _edit_payload(protected, "Doctrine.", "Doctrine.\n\n## Capacity (PM 2026-10-30)\nText.")
    )
    context = _context(result)
    assert _RULE in context
    assert "changelog-shaped" in context


def test_quoted_verbatim_block_is_louder(tmp_path, monkeypatch):
    _root, protected = _protected(tmp_path, monkeypatch, "Doctrine.\n")
    result = guard.check(_edit_payload(protected, "Doctrine.", "Doctrine.\n> PM said so.\n"))
    assert "changelog-shaped" in _context(result)


def test_moved_dated_heading_is_not_changelog_shaped(tmp_path, monkeypatch):
    _root, protected = _protected(tmp_path, monkeypatch, "## Old (2026-10-30)\nA.\n")
    result = guard.check(_edit_payload(protected, "A.", "B."))
    assert "changelog-shaped" not in _context(result)


def test_non_claude_md_surface_has_no_strongest_warning(tmp_path, monkeypatch):
    repo_root, _p = _protected(tmp_path, monkeypatch, "x\n")
    local = repo_root / "coordinator.local.md"
    local.write_text("fast_test_cmd: a\n", encoding="utf-8")
    result = guard.check(_edit_payload(local, "fast_test_cmd: a", "fast_test_cmd: b"))
    assert _RULE not in _context(result)


def _as_subagent(payload):
    return {**payload, "agent_id": "deadbeef0123", "agent_type": "coordinator:executor"}


def test_subagent_edit_of_claude_md_is_denied(tmp_path, monkeypatch):
    _root, protected = _protected(tmp_path, monkeypatch, "Doctrine. Old sentence.\n")
    result = guard.check(_as_subagent(_edit_payload(protected, "Old sentence.", "New.")))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_subagent_edit_of_non_claude_md_surface_is_not_denied(tmp_path, monkeypatch):
    repo_root, _p = _protected(tmp_path, monkeypatch, "x\n")
    local = repo_root / "coordinator.local.md"
    local.write_text("fast_test_cmd: a\n", encoding="utf-8")
    result = guard.check(_as_subagent(_edit_payload(local, "fast_test_cmd: a", "fast_test_cmd: b")))
    assert "permissionDecision" not in result["hookSpecificOutput"]


def test_subagent_deny_is_registered_and_survives_every_level(monkeypatch):
    from coordinator_core import deny_registry as dr
    from coordinator_core import machine_profile as mp

    assert dr.guard_deny_class("guard-doctrine-surface-edits", subagent=True) in dr.GUARD_CLASSES
    assert dr.guard_deny_class("guard-doctrine-surface-edits") is None
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        mp.reset_cache()
        assert mp.resolved_level("guard-doctrine-surface-edits", subagent=True) == "floor"
        assert mp.resolved_level("guard-doctrine-surface-edits") == "log"


def test_guard_exposes_no_sentinel_reader():
    assert not hasattr(guard, "_sentinel_state")
    assert not hasattr(guard, "pm_approval_fresh")
