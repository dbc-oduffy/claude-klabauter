"""Tests for coordinator_core.ops.resolve_swept_baton (op baton.resolve_swept_in_archive).

Covers: flat cross-repo/archive hit, month-nested archive/handoffs/ hit, mixed
flat+nested archive/completed hit, search-order precedence, not-found shapes
(missing basename param, absent repo_root, no match anywhere), raw frontmatter
pass-through (no vocabulary interpretation, per DR-084), archiving-commit
resolution via git log, and the AC7 double-invocation idempotency proof.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core.ops.resolve_swept_baton import _resolve_swept_baton_in_archive
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Declared, not excused: this file spawns real git because the AC7 archiving-commit
# resolution the module implements genuinely runs `git log` against a real repo --
# no mock stands in for git's own log/blame plumbing. Each test builds its own
# tmp_path repo via the `repo` fixture, so mutation (commit history) needs per-test
# isolation, not a module-scope hoist. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(path), check=True, **no_console_passthrough_kwargs()
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=str(path),
        check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(path),
        check=True,
        **no_console_passthrough_kwargs(),
    )
    return path


def _commit_all(path, message):
    subprocess.run(
        ["git", "add", "-A"], cwd=str(path), check=True, **no_console_passthrough_kwargs()
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(path),
        check=True,
        **no_console_passthrough_kwargs(),
    )


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "repo")


def _call(params, repo_root=None):
    # No pytest-asyncio in this tree — house convention (test_handoff_match.py)
    # is a bare asyncio.run() wrapper over the async handler.
    return asyncio.run(_resolve_swept_baton_in_archive(params, repo_root=repo_root))


def test_flat_cross_repo_archive_hit(repo):
    archive_dir = repo / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    baton = archive_dir / "2026-07-01_memo.md"
    baton.write_text("---\ntitle: memo\nstatus: actioned\n---\nbody\n", encoding="utf-8")
    _commit_all(repo, "add swept memo")

    result = _call({"basename": "2026-07-01_memo.md"}, repo_root=repo / ".git")

    assert result["found"] is True
    assert result["archive_path"] == str(baton)
    assert result["frontmatter"] == {"title": "memo", "status": "actioned"}
    assert result["archiving_commit"] is not None
    assert len(result["archiving_commit"]) == 40


def test_month_nested_archive_handoffs_hit(repo):
    nested_dir = repo / "archive" / "handoffs" / "2026-07"
    nested_dir.mkdir(parents=True)
    baton = nested_dir / "2026-07-15_120000_swept.md"
    baton.write_text("---\ntitle: swept baton\n---\nbody\n", encoding="utf-8")
    _commit_all(repo, "archive nested baton")

    result = _call({"basename": "2026-07-15_120000_swept.md"}, repo_root=repo / ".git")

    assert result["found"] is True
    assert result["archive_path"] == str(baton)
    assert result["frontmatter"]["title"] == "swept baton"


def test_mixed_flat_and_nested_completed_dir(repo):
    completed_dir = repo / "archive" / "completed"
    (completed_dir / "2026-06").mkdir(parents=True)
    flat = completed_dir / "flat.md"
    flat.write_text("---\ntitle: flat\n---\n", encoding="utf-8")
    nested = completed_dir / "2026-06" / "nested.md"
    nested.write_text("---\ntitle: nested\n---\n", encoding="utf-8")
    _commit_all(repo, "mixed layout")

    flat_result = _call({"basename": "flat.md"}, repo_root=repo / ".git")
    nested_result = _call({"basename": "nested.md"}, repo_root=repo / ".git")

    assert flat_result["found"] is True
    assert flat_result["archive_path"] == str(flat)
    assert nested_result["found"] is True
    assert nested_result["archive_path"] == str(nested)


def test_search_order_prefers_cross_repo_archive_first(repo):
    # Same basename present in both cross-repo/archive and archive/handoffs —
    # the fixed search order must resolve to cross-repo/archive/ first.
    cra = repo / "cross-repo" / "archive"
    cra.mkdir(parents=True)
    ah = repo / "archive" / "handoffs"
    ah.mkdir(parents=True)
    (cra / "dup.md").write_text("---\nsrc: cross-repo\n---\n", encoding="utf-8")
    (ah / "dup.md").write_text("---\nsrc: handoffs\n---\n", encoding="utf-8")
    _commit_all(repo, "dup basenames")

    result = _call({"basename": "dup.md"}, repo_root=repo / ".git")

    assert result["archive_path"] == str(cra / "dup.md")
    assert result["frontmatter"]["src"] == "cross-repo"


def test_bare_slug_resolves_to_md(repo):
    """2026-07-28 defect fix — a literal `rglob(basename)` never matches
    `<slug>.md` for a bare (extensionless) basename."""
    archive_dir = repo / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    baton = archive_dir / "2026-07-25-triage-red-tests.md"
    baton.write_text("---\ntitle: swept\n---\nbody\n", encoding="utf-8")
    _commit_all(repo, "add swept baton")

    result = _call({"basename": "2026-07-25-triage-red-tests"}, repo_root=repo / ".git")

    assert result["found"] is True
    assert result["archive_path"] == str(baton)


def test_bare_slug_not_found_stays_not_found(repo):
    result = _call({"basename": "totally-absent-slug"}, repo_root=repo / ".git")

    assert result == {
        "found": False,
        "archive_path": None,
        "frontmatter": {},
        "archiving_commit": None,
    }


def test_bare_slug_same_dir_collision_prefers_extensionless(repo):
    """Finding 4 (2026-07-28 review): a same-dir collision between an
    extensionless file and its `.md` sibling deterministically prefers the
    extensionless form, because its path string is a strict prefix of the
    `.md` sibling's and always sorts first."""
    archive_dir = repo / "archive" / "completed"
    archive_dir.mkdir(parents=True)
    extensionless = archive_dir / "dup-slug"
    extensionless.write_text("---\nsrc: extensionless\n---\n", encoding="utf-8")
    md_sibling = archive_dir / "dup-slug.md"
    md_sibling.write_text("---\nsrc: md\n---\n", encoding="utf-8")
    _commit_all(repo, "same-dir collision")

    result = _call({"basename": "dup-slug"}, repo_root=repo / ".git")

    assert result["found"] is True
    assert result["archive_path"] == str(extensionless)
    assert result["frontmatter"]["src"] == "extensionless"


def test_not_found_anywhere(repo):
    result = _call({"basename": "no-such-file.md"}, repo_root=repo / ".git")

    assert result == {
        "found": False,
        "archive_path": None,
        "frontmatter": {},
        "archiving_commit": None,
    }


def test_missing_basename_param_structured_not_found():
    for bad in ({}, {"basename": ""}, {"basename": 42}):
        result = _call(bad)
        assert result["found"] is False
        assert result["archive_path"] is None


def test_absent_repo_root_returns_not_found():
    result = _call({"basename": "anything.md"}, repo_root=None)

    assert result["found"] is False


def test_malformed_frontmatter_degrades_to_empty_dict(repo):
    archive_dir = repo / "archive" / "completed"
    archive_dir.mkdir(parents=True)
    baton = archive_dir / "broken.md"
    # Unclosed frontmatter fence — no second "---" line.
    baton.write_text("---\ntitle: broken\nbody without closing fence\n", encoding="utf-8")
    _commit_all(repo, "broken frontmatter")

    result = _call({"basename": "broken.md"}, repo_root=repo / ".git")

    assert result["found"] is True
    assert result["frontmatter"] == {}


def test_double_invocation_identical_result(repo):
    """AC7 idempotency proof: pure read — second call with identical inputs
    returns the identical documented shape."""
    archive_dir = repo / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    baton = archive_dir / "idempotent.md"
    baton.write_text("---\ntitle: idempotent\n---\n", encoding="utf-8")
    _commit_all(repo, "idempotent baton")
    params = {"basename": "idempotent.md"}

    first = _call(params, repo_root=repo / ".git")
    second = _call(params, repo_root=repo / ".git")

    assert first["found"] is True
    assert first == second


def test_archiving_commit_git_call_carries_hardening_kwargs(repo, monkeypatch):
    """Review: code-reviewer (F4, P2) — the one subprocess call in this
    module was the odd one out relative to every sibling git-wrapper in
    this wave (missing timeout/creationflags/stdin hardening)."""
    archive_dir = repo / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    baton = archive_dir / "hardening.md"
    baton.write_text("---\ntitle: hardening\n---\n", encoding="utf-8")
    _commit_all(repo, "add hardening baton")

    captured = {}
    real_run = subprocess.run

    def spy_run(cmd, **kwargs):
        if cmd[:1] == ["git"] and "log" in cmd:
            captured["kwargs"] = kwargs
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    _call({"basename": "hardening.md"}, repo_root=repo / ".git")

    assert captured["kwargs"].get("timeout")
    assert captured["kwargs"].get("stdin") is subprocess.DEVNULL
    assert "creationflags" in captured["kwargs"]


def test_archiving_commit_degrades_on_timeout(repo, monkeypatch):
    archive_dir = repo / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    baton = archive_dir / "timeout.md"
    baton.write_text("---\ntitle: timeout\n---\n", encoding="utf-8")
    _commit_all(repo, "add timeout baton")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _call({"basename": "timeout.md"}, repo_root=repo / ".git")

    assert result["found"] is True
    assert result["archiving_commit"] is None


def test_registered_under_op_key():
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("baton.resolve_swept_in_archive") is _resolve_swept_baton_in_archive
