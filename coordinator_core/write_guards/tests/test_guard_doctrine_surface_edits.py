"""Behavioral tests for coordinator_core.write_guards.guard_doctrine_surface_edits.check:
which paths are protected, which edits are exempt, and the advisory it returns.

The guard never denies and reads no sentinel file. Protection means an
`additionalContext` advisory; owning-root anchoring decides which paths match.
"""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.session import harness_registry as hr
from coordinator_core.write_guards import guard_doctrine_surface_edits as guard

pytestmark = [pytest.mark.cadence]


def _context(result) -> str:
    assert result is not None
    assert "permissionDecision" not in result["hookSpecificOutput"]
    return result["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# C4 — advisory `gates.repo_identity` recording
# (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md § C4).
#
# DR-277: this guard clears no hard-deny carve-out for the repo-identity gate.
# These tests construct a REAL MISMATCH via real harness-registry files on disk
# and pin the load-bearing property: the verdict is RECORDED, and the guard's
# decision is UNCHANGED by it.
# ---------------------------------------------------------------------------


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


def _write_registry_record(sessions_dir, filename, session_id, pid, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return epoch


def _make_repo(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _patch_pid_env(monkeypatch, pid, create_time=0.0):
    monkeypatch.setattr(
        "coordinator_core.session.core._resolve_claude_pid_from_env",
        lambda: ((pid, create_time), "env-hit"),
    )


def _mismatch_box(tmp_path, monkeypatch, session_id, pid):
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _make_repo(repo_root)
    _make_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    _write_registry_record(sessions_dir, f"{pid}.json", session_id, pid, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _patch_pid_env(monkeypatch, pid)
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))
    return repo_root


def test_advisory_log_records_mismatch_and_decision_is_unchanged(tmp_path, monkeypatch):
    """A real repo-identity MISMATCH is recorded AND `check()`'s decision for an
    unprotected file (None) is unchanged by it."""
    repo_root = _mismatch_box(tmp_path, monkeypatch, "sess-mismatch", 9001)
    unprotected = repo_root / "some_file.py"
    unprotected.write_text("x = 1\n", encoding="utf-8")
    # A real session's hub directory is created by `core.init`; the advisory
    # logger never creates one itself.
    (repo_root / ".git" / "coordinator-sessions" / "sess-mismatch").mkdir(parents=True)

    result = guard.check(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(unprotected)},
            "session_id": "sess-mismatch",
        }
    )

    assert result is None
    log_path = repo_root / ".git" / "coordinator-sessions" / "sess-mismatch" / "repo-identity-gate.log"
    assert log_path.is_file(), "advisory gates.repo_identity fact was not recorded"
    contents = log_path.read_text(encoding="utf-8")
    assert "gates.repo_identity" in contents
    assert "verdict=MISMATCH" in contents


def test_advisory_log_mismatch_does_not_change_the_advisory_on_a_protected_file(tmp_path, monkeypatch):
    repo_root = _mismatch_box(tmp_path, monkeypatch, "sess-mismatch-2", 9002)
    protected = repo_root / "CLAUDE.md"
    protected.write_text("doctrine\n", encoding="utf-8")

    result = guard.check(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(protected)},
            "session_id": "sess-mismatch-2",
        }
    )

    context = _context(result)
    assert "always-loaded doctrine" in context
    assert "gates.repo_identity" not in context


def test_advisory_log_not_written_without_repo_root(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.write_guards.guard_doctrine_surface_edits.compute_repo_identity_gate",
        lambda *a, **k: calls.append((a, k)) or {"verdict": "UNRESOLVED"},
    )
    monkeypatch.setattr(guard, "_git_root", lambda: None)

    guard.check(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/whatever.py"},
            "session_id": "sess-no-root",
        }
    )
    assert calls == []


def test_advisory_log_never_creates_a_session_dir(tmp_path, monkeypatch):
    """An advisory log line must never MINT session state: `live_session_ids`
    enumerates every child of `.git/coordinator-sessions/` as a session."""
    repo_root = _mismatch_box(tmp_path, monkeypatch, "sess-phantom", 9002)
    unprotected = repo_root / "some_file.py"
    unprotected.write_text("x = 1\n", encoding="utf-8")

    result = guard.check(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(unprotected)},
            "session_id": "sess-phantom",
        }
    )

    assert result is None
    assert not (repo_root / ".git" / "coordinator-sessions" / "sess-phantom").exists()


# ---------------------------------------------------------------------------
# Cross-repo anchoring: the two DoE-only surfaces follow the DoE root, not the
# session's cwd (measured 2026-09-17 from a claude-klabauter session).
# ---------------------------------------------------------------------------

_DOE_ONLY_RELATIVES = [
    ("coordinator", "snippets", "em-operating-doctrine.md"),
    ("global-doctrine", "CLAUDE.md"),
]


@pytest.fixture
def two_roots(tmp_path, monkeypatch):
    """A DoE root and an unrelated session repo, with the pointer resolved to
    the former and `_git_root()` to the latter. Clears the module's
    per-process content-root memo before and after."""
    doe = tmp_path / "coordinator-content-repo"
    (doe / "coordinator" / "snippets").mkdir(parents=True)
    (doe / "global-doctrine").mkdir(parents=True)
    (doe / ".git").mkdir()
    session_repo = tmp_path / "project-peer"
    (session_repo / ".git").mkdir(parents=True)

    guard._content_root_memo.clear()
    monkeypatch.setattr(guard, "read_content_root", lambda: str(doe))
    monkeypatch.setattr(guard, "_git_root", lambda: str(session_repo))
    monkeypatch.setattr(
        guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "MATCH", "message": ""}
    )
    yield doe, session_repo
    guard._content_root_memo.clear()


def _verdict(target) -> str:
    """"advisory" for a protected target, "quiet" when the guard returns nothing."""
    result = guard.check(
        {"tool_name": "Edit", "session_id": "t", "tool_input": {"file_path": str(target)}}
    )
    assert result is None or "permissionDecision" not in result["hookSpecificOutput"]
    return "quiet" if result is None else "advisory"


@pytest.mark.parametrize("relative", _DOE_ONLY_RELATIVES)
def test_doe_only_surfaces_are_protected_from_a_foreign_session(two_roots, relative):
    doe, _session_repo = two_roots
    assert _verdict(doe.joinpath(*relative)) == "advisory"


def test_unresolvable_doe_pointer_lands_on_the_per_repo_protected_set(two_roots, monkeypatch):
    """Fail-quiet, not fail-error: an unresolvable pointer drops the DoE-anchored
    entries and leaves the per-repo entries protected."""
    doe, session_repo = two_roots
    guard._content_root_memo.clear()
    monkeypatch.setattr(guard, "read_content_root", lambda: "")

    paths = [path for path, _ in guard._protected_entries(str(session_repo))]
    assert guard._norm(str(doe.joinpath(*_DOE_ONLY_RELATIVES[0]))) not in paths
    assert guard._norm(str(session_repo / "CLAUDE.md")) in paths
    assert guard._norm(str(session_repo / "coordinator.local.md")) in paths
    assert guard._home_claude_md() in paths


@pytest.mark.parametrize("name", ["CLAUDE.md", "coordinator.local.md"])
def test_per_repo_surfaces_still_anchor_on_the_session_repo(two_roots, name):
    _doe, session_repo = two_roots
    assert _verdict(session_repo / name) == "advisory"
    entry_roots = {
        owning
        for path, owning in guard._protected_entries(str(session_repo))
        if path == guard._norm(str(session_repo / name))
    }
    assert entry_roots == {None}


def test_content_root_is_resolved_once_per_process(two_roots, monkeypatch):
    """The memo keeps a registry read plus two file reads off every Write/Edit."""
    doe, session_repo = two_roots
    guard._content_root_memo.clear()
    calls = []

    def _counted():
        calls.append(1)
        return str(doe)

    monkeypatch.setattr(guard, "read_content_root", _counted)
    for _ in range(4):
        guard._protected_entries(str(session_repo))
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Foreign-repo (non-DoE) root doctrine files: another repo's own root
# CLAUDE.md / coordinator.local.md is protected from a session rooted elsewhere.
# ---------------------------------------------------------------------------


@pytest.fixture
def foreign_third_repo(tmp_path, monkeypatch):
    session_repo = tmp_path / "project-rag"
    third_repo = tmp_path / "example-retrieval-repo-ue-addon"
    _make_repo(session_repo)
    _make_repo(third_repo)

    guard._content_root_memo.clear()
    monkeypatch.setattr(guard, "read_content_root", lambda: "")
    monkeypatch.setattr(guard, "_git_root", lambda: str(session_repo))
    monkeypatch.setattr(
        guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "MATCH", "message": ""}
    )
    yield session_repo, third_repo
    guard._content_root_memo.clear()


@pytest.mark.parametrize("name", ["CLAUDE.md", "coordinator.local.md"])
def test_foreign_third_repo_root_doctrine_is_protected(foreign_third_repo, name):
    _session_repo, third_repo = foreign_third_repo
    assert _verdict(third_repo / name) == "advisory"


def test_nested_claude_md_in_a_foreign_repo_stays_unprotected(foreign_third_repo):
    """Exact-match, not substring/basename: a CLAUDE.md below a foreign repo's root does not match."""
    _session_repo, third_repo = foreign_third_repo
    nested = third_repo / "coordinator" / "tests" / "CLAUDE.md"
    nested.parent.mkdir(parents=True)
    assert _verdict(nested) == "quiet"


# ---------------------------------------------------------------------------
# Exemptions and advisory text.
# ---------------------------------------------------------------------------


def _write(target, content, tool="Write"):
    key = "content" if tool == "Write" else "new_string"
    return guard.check(
        {"tool_name": tool, "session_id": "t", "tool_input": {"file_path": str(target), key: content}}
    )


def test_first_time_create_of_missing_claude_md_is_quiet(two_roots):
    _doe, repo = two_roots
    target = repo / "CLAUDE.md"
    target.unlink(missing_ok=True)
    assert _write(target, "# Project\n") is None


def test_write_over_existing_claude_md_advises(two_roots):
    _doe, repo = two_roots
    (repo / "CLAUDE.md").write_text("x", encoding="utf-8")
    assert "always-loaded doctrine" in _context(_write(repo / "CLAUDE.md", "# y\n"))


def test_project_type_only_local_config_write_is_quiet(two_roots):
    _doe, repo = two_roots
    assert _write(repo / "coordinator.local.md", "project_type: general\n") is None


@pytest.mark.parametrize(
    "line", ["fast_test_cmd: pytest", "fast_tier_unscoped_reason: x", "b_post_command: y"]
)
def test_local_config_with_privileged_key_advises(two_roots, line):
    _doe, repo = two_roots
    context = _context(_write(repo / "coordinator.local.md", f"project_type: general\n{line}\n"))
    assert "Tier-U" in context


def test_write_dropping_existing_privileged_key_advises(two_roots):
    _doe, repo = two_roots
    (repo / "coordinator.local.md").write_text("fast_test_cmd: pytest\n", encoding="utf-8")
    assert "Tier-U" in _context(_write(repo / "coordinator.local.md", "project_type: general\n"))


def test_local_config_path_matches_the_protected_entry(two_roots):
    _doe, repo = two_roots
    assert guard._local_config_path(str(repo)) in guard._protected_paths(str(repo))


def test_local_config_path_is_none_without_a_repo_root():
    assert guard._local_config_path(None) is None


def test_advisory_text_carries_no_human_unlock(two_roots):
    _doe, repo = two_roots
    (repo / "CLAUDE.md").write_text("x", encoding="utf-8")
    context = _context(_write(repo / "CLAUDE.md", "# y\n"))
    for forbidden in ("PM", "sentinel", "approve", "waiver"):
        assert forbidden not in context
