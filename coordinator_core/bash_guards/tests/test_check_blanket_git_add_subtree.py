"""Tests for the C3 subtree expansion in
``coordinator_core.bash_guards.dispatch_checks.check_blanket_git_add``
(docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md, chunk C3).

Pre-C3, ``check_blanket_git_add`` matched only root-anchored blankets
(``-A``, ``-u``, ``-U``, ``.``, ``./``, ``:/``, ``:/.``, an absolute path
resolving to the repo root) -- a genuinely scoped SUBTREE pathspec
(``git add src/``) was deliberately left unmatched, the "root, not a
subtree" asymmetry the module's own ``_find_is_root_anchor`` docstring
names. This file pins the new behaviour: a subtree pathspec is now
expanded (via a single ``git add --dry-run`` spawn, only for a token that
resolves to an existing directory) and denied if the expansion reaches a
path this session's touch-record claims do not cover.

NEGATIVE SPEC: a literal single-file pathspec (``git add path/to/file``)
never reaches the new branch at all -- ``_bt_add_resolve_subtree_dir_token``
returns ``None`` for anything that is not an existing directory, so the
common case costs no extra ``git`` spawn (DR-344). Pinned by
``test_single_file_add_is_not_denied_and_spawns_no_extra_git``.

Spec backlink: coordinator_core/bash_guards/dispatch_checks.py
(``check_blanket_git_add``, ``_bt_add_resolve_subtree_dir_token``,
``_bt_add_subtree_foreign_paths``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.bash_guards import dispatch_checks as guard


def _wire_hazard(monkeypatch, *, is_hazard=True):
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda root: is_hazard)


def _wire_git(monkeypatch, root, *, dry_run_lines=None, dry_run_rc=0):
    """Responds to BOTH ``git`` calls this guard now makes: the pre-existing
    ``rev-parse --show-toplevel`` root probe, and the new
    ``add --dry-run -- <dir>`` subtree-expansion probe -- a single fixture
    covering both shapes, since C3 is what makes the second call exist at
    all."""
    calls = []

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        calls.append(list(args))
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, root + "\n"
        if args[:2] == ["add", "--dry-run"]:
            if dry_run_lines is None:
                return dry_run_rc, ""
            return dry_run_rc, "\n".join(dry_run_lines) + "\n"
        raise AssertionError("unexpected git invocation: %r" % (args,))

    monkeypatch.setattr(guard, "_run_git", _fake_run_git)
    return calls


def _wire_claims(monkeypatch, claimed_paths):
    """Short-circuits the touch-record ownership read this chunk reuses
    from ``_rm_peer_claim_of``'s own import site, so the subtree test
    doesn't need a real ``.git/coordinator-sessions`` tree on disk."""
    fake_projection = SimpleNamespace(
        claims={p: object() for p in claimed_paths}, degraded=False
    )

    class _FakeTouchRecord:
        @staticmethod
        def sink_path(claimant_dir):
            return claimant_dir

        @staticmethod
        def project_live_claims(*sink_paths, cwd=None):
            return fake_projection

    monkeypatch.setitem(
        __import__("sys").modules,
        "coordinator_core.session.touch_record",
        _FakeTouchRecord,
    )


def test_subtree_add_with_foreign_path_is_denied(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    subtree = root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(
        monkeypatch,
        str(root),
        dry_run_lines=["add 'src/mine.py'", "add 'src/peer.py'"],
    )
    _wire_claims(monkeypatch, {"src/mine.py"})

    result = guard.check_blanket_git_add(
        "git add src/", session_id="sess1", hook_payload={}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "src/peer.py" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_subtree_add_all_own_paths_is_allowed(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    subtree = root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(
        monkeypatch,
        str(root),
        dry_run_lines=["add 'src/mine.py'"],
    )
    _wire_claims(monkeypatch, {"src/mine.py"})

    result = guard.check_blanket_git_add(
        "git add src/", session_id="sess1", hook_payload={}
    )

    assert result is None


def test_single_file_add_is_not_denied_and_spawns_no_extra_git(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _wire_hazard(monkeypatch)
    calls = _wire_git(monkeypatch, str(root))

    result = guard.check_blanket_git_add(
        "git add src/mine.py", session_id="sess1", hook_payload={}
    )

    assert result is None
    # Only the pre-existing root probe fired -- a literal file token never
    # resolves to an existing directory, so `_bt_add_resolve_subtree_dir_
    # token` returns None before any `add --dry-run` spawn happens.
    assert calls == [["rev-parse", "--show-toplevel"]]


def test_subtree_add_no_session_id_fails_open(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    subtree = root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(
        monkeypatch,
        str(root),
        dry_run_lines=["add 'src/peer.py'"],
    )

    result = guard.check_blanket_git_add("git add src/", session_id="", hook_payload={})

    assert result is None


def test_subtree_add_dry_run_failure_fails_open(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    subtree = root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(root)
    _wire_hazard(monkeypatch)
    _wire_git(monkeypatch, str(root), dry_run_rc=1, dry_run_lines=None)
    _wire_claims(monkeypatch, set())

    result = guard.check_blanket_git_add(
        "git add src/", session_id="sess1", hook_payload={}
    )

    assert result is None
