"""Tests for coordinator_core.ops.research_archive_workdir.

Coverage contract (Wave-3 settlement A1 + CC-4/CC-6/CC-7,
docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md): a named
test per crash-window / rerun state is an acceptance criterion —

  - normal archive (src present, dest absent)
  - already-archived no-op (src absent, dest present) + CC-4 double invocation
  - src absent + dest absent → structured error (CC-7)
  - crash window (a): stale ``.tmp-archive-{run_id}`` self-clean before re-copy
  - crash window (b): both exist, content-EQUAL → pending rmtree completed
  - both exist, content-UNEQUAL → structured error, nothing deleted
  - EXDEV fallback: copytree → atomic tmp rename → rmtree(src)
  - CC-6 dry_run: zero writes, mutation flag false, would_action reported

All filesystem work happens in tmp_path throwaway trees; the git common dir is
a plain ``.git`` directory (main_worktree_root only takes ``.parent``) — no git
subprocess anywhere.
"""

from __future__ import annotations

import asyncio
import errno
import os
import subprocess
from pathlib import Path

from coordinator_core.ops import research_archive_workdir as mod
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

RUN_ID = "run-20260722-abc123"
TOPIC = "warp-core-ejection"
WORKDIR_NAME = f"{RUN_ID}-{TOPIC}-workdir"
DEST_NAME = f"{RUN_ID}-{TOPIC}"


def _call(params: dict, repo_root) -> dict:
    return asyncio.run(mod._handler(params, repo_root))


def _mk_worktree(tmp_path: Path) -> Path:
    """A throwaway worktree with a plain .git dir (common_dir = worktree/.git)."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "research" / "archive").mkdir(parents=True)
    return tmp_path


def _mk_workdir(worktree: Path) -> Path:
    src = worktree / "docs" / "research" / WORKDIR_NAME
    (src / "phase-1").mkdir(parents=True)
    (src / "synthesis.md").write_text("findings\n", encoding="utf-8")
    (src / "phase-1" / "notes.md").write_text("notes\n", encoding="utf-8")
    return src


def test_normal_archive_moves_workdir_into_archive(tmp_path):
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    dest = worktree / "docs" / "research" / "archive" / DEST_NAME
    assert result["exit_code"] == 0
    assert result["archived"] is True
    assert result["already_archived"] is False
    assert result["dest"] == str(dest)
    assert not src.exists()
    assert (dest / "synthesis.md").read_text(encoding="utf-8") == "findings\n"
    assert (dest / "phase-1" / "notes.md").exists()


def test_double_invocation_is_already_archived_noop(tmp_path):
    """CC-4: second identical call is a safe no-op with the documented shape."""
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    first = _call({"run_id": RUN_ID}, worktree / ".git")
    assert first["archived"] is True
    second = _call({"run_id": RUN_ID}, worktree / ".git")
    dest = worktree / "docs" / "research" / "archive" / DEST_NAME
    assert second["exit_code"] == 0
    assert second["archived"] is True
    assert second["already_archived"] is True
    assert second["dest"] == str(dest)
    assert dest.is_dir()


def test_src_absent_dest_absent_is_structured_error(tmp_path):
    """CC-7: the unclassifiable nothing-anywhere state fails loud, no guessing."""
    worktree = _mk_worktree(tmp_path)
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 1
    assert "nothing to archive" in result["error"]
    assert RUN_ID in result["error"]


def test_crash_window_a_stale_tmp_self_cleaned_before_recopy(tmp_path):
    """Settlement A1 window (a): a rerun deletes ITS OWN stale tmp first."""
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    archive = worktree / "docs" / "research" / "archive"
    stale = archive / f".tmp-archive-{RUN_ID}"
    (stale / "partial").mkdir(parents=True)
    (stale / "partial" / "halfcopied.md").write_text("junk", encoding="utf-8")
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["archived"] is True
    assert not stale.exists()
    assert (archive / DEST_NAME / "synthesis.md").exists()


def test_crash_window_b_content_equal_completes_pending_rmtree(tmp_path):
    """Settlement A1 window (b): both exist + equal → finish rmtree(src)."""
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)
    dest = worktree / "docs" / "research" / "archive" / DEST_NAME
    # Simulate crash-after-rename-before-rmtree: dest is a full equal copy.
    import shutil
    shutil.copytree(src, dest)
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["archived"] is True
    assert result["resumed_cleanup"] is True
    assert not src.exists()
    assert (dest / "synthesis.md").exists()


def test_both_exist_content_unequal_is_structured_error_nothing_deleted(tmp_path):
    """CC-7 exception boundary: unequal both-exist never clobbers either side."""
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)
    dest = worktree / "docs" / "research" / "archive" / DEST_NAME
    dest.mkdir(parents=True)
    (dest / "different.md").write_text("other content", encoding="utf-8")
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 1
    assert "NOT content-equal" in result["error"]
    # Use the structured src/dest fields, not substring-matching the
    # human-readable `error` message: that message embeds the paths via
    # !r, which on Windows doubles each backslash separator, so a plain
    # str(path) containment check against it spuriously fails.
    assert result["src"] == str(src)
    assert result["dest"] == str(dest)
    assert src.exists() and dest.exists()
    assert (src / "synthesis.md").exists()
    assert (dest / "different.md").exists()


def test_exdev_fallback_copytree_tmp_rename_rmtree(tmp_path, monkeypatch):
    """EXDEV on the direct rename falls back to copy→atomic-rename→rmtree."""
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)
    dest = worktree / "docs" / "research" / "archive" / DEST_NAME
    real_rename = os.rename
    calls = []

    def fake_rename(a, b, *args, **kwargs):
        calls.append((str(a), str(b)))
        if Path(a) == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(a), 17, str(b))
        return real_rename(a, b, *args, **kwargs)

    monkeypatch.setattr(os, "rename", fake_rename)
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["archived"] is True
    assert not src.exists()
    assert (dest / "synthesis.md").read_text(encoding="utf-8") == "findings\n"
    assert (dest / "phase-1" / "notes.md").exists()
    # tmp staging dir removed by the atomic rename into place
    assert not (dest.parent / f".tmp-archive-{RUN_ID}").exists()
    # second call: fallback path also lands in the already-archived no-op
    second = _call({"run_id": RUN_ID}, worktree / ".git")
    assert second["already_archived"] is True


def test_non_exdev_rename_error_is_structured_error(tmp_path, monkeypatch):
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)

    def fake_rename(a, b, *args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied", str(a))

    monkeypatch.setattr(os, "rename", fake_rename)
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 1
    assert "non-EXDEV" in result["error"]
    assert src.exists()


def test_dry_run_normal_state_performs_zero_writes(tmp_path):
    """CC-6: resolve+validate everything, zero writes, mutation flag false."""
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)
    stale = worktree / "docs" / "research" / "archive" / f".tmp-archive-{RUN_ID}"
    stale.mkdir(parents=True)
    result = _call({"run_id": RUN_ID, "dry_run": True}, worktree / ".git")
    dest = worktree / "docs" / "research" / "archive" / DEST_NAME
    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert result["archived"] is False
    assert result["would_action"] == "archive"
    assert result["dest"] == str(dest)
    assert src.exists()
    assert not dest.exists()
    assert stale.exists()  # even the stale-tmp self-clean is deferred on dry_run


def test_dry_run_already_archived_reports_none_action(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    _call({"run_id": RUN_ID}, worktree / ".git")
    result = _call({"run_id": RUN_ID, "dry_run": True}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["archived"] is False
    assert result["already_archived"] is True
    assert result["would_action"] == "none"


def test_dry_run_pending_cleanup_reports_and_defers(tmp_path):
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)
    dest = worktree / "docs" / "research" / "archive" / DEST_NAME
    import shutil
    shutil.copytree(src, dest)
    result = _call({"run_id": RUN_ID, "dry_run": True}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["would_action"] == "complete_pending_cleanup"
    assert src.exists()  # pending rmtree deferred


def test_ambiguous_multiple_workdirs_is_structured_error(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    other = worktree / "docs" / "research" / f"{RUN_ID}-other-topic-workdir"
    other.mkdir(parents=True)
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 1
    assert "ambiguous workdir" in result["error"]


def test_missing_or_unsafe_run_id_rejected(tmp_path):
    worktree = _mk_worktree(tmp_path)
    assert _call({}, worktree / ".git")["exit_code"] == 1
    bad = _call({"run_id": "../escape"}, worktree / ".git")
    assert bad["exit_code"] == 1
    assert "safe path segment" in bad["error"]


def test_repo_root_arg_required(tmp_path):
    result = _call({"run_id": RUN_ID}, None)
    assert result["exit_code"] == 1
    assert "repo_root" in result["error"]


def test_params_repo_root_mismatch_is_structured_error(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    other = tmp_path / "elsewhere"
    other.mkdir()
    result = _call({"run_id": RUN_ID, "repo_root": str(other)}, worktree / ".git")
    assert result["exit_code"] == 1
    assert "does not match" in result["error"]


def test_params_repo_root_match_is_accepted(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    result = _call({"run_id": RUN_ID, "repo_root": str(worktree)}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["archived"] is True


# ---------------------------------------------------------------------------
# Claim restatement (restate_touched_tree wiring)
# ---------------------------------------------------------------------------


def _mk_git_worktree(tmp_path: Path) -> Path:
    """A REAL git repo (unlike ``_mk_worktree``'s plain ``.git`` directory) —
    needed here because ``safe_commit_offer.compute_offer`` shells out to
    ``git diff``/``git ls-files`` to compute the dirty-tree side of a
    session's scope, on top of the touched.txt claim side this test exists
    to exercise.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "docs" / "research" / "archive").mkdir(parents=True)
    return tmp_path


def test_claimed_nested_file_is_claimed_at_new_path_after_archive(
    tmp_path, monkeypatch
):
    """A file this session T-claimed inside the workdir must still be
    claimed — at its NEW path — after the move, per ``compute_offer``: the
    exact regression ``restate_touched_tree`` is wired in to prevent
    (Wire: research_archive_workdir)."""
    worktree = _mk_git_worktree(tmp_path)
    _mk_workdir(worktree)
    sid = "sess-archive-claim"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    session_core.init(sid, cwd=str(worktree))
    old_rel = f"docs/research/{WORKDIR_NAME}/phase-1/notes.md"
    session_scope.touch(sid, old_rel, cwd=str(worktree))

    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["archived"] is True

    # The stale old-path claim deliberately stays standing (per
    # restate_touched_tree's own contract: it writes no R event) for
    # release_committed_claims to retire once the tree's deletion lands in a
    # commit — this test's job is only to confirm the NEW path is claimed,
    # not that the old one vanished.
    new_rel = f"docs/research/archive/{DEST_NAME}/phase-1/notes.md"
    offer = safe_commit_offer.compute_offer(sid, cwd=str(worktree))
    assert new_rel in offer["safe_paths"]


def test_dry_run_writes_no_claim_events(tmp_path, monkeypatch):
    """CC-6 x claim-restatement: a dry run must not mutate touched.txt
    either — restate_touched_tree sits strictly after the dry_run early
    return."""
    worktree = _mk_git_worktree(tmp_path)
    _mk_workdir(worktree)
    sid = "sess-archive-dryrun"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    session_core.init(sid, cwd=str(worktree))
    old_rel = f"docs/research/{WORKDIR_NAME}/phase-1/notes.md"
    session_scope.touch(sid, old_rel, cwd=str(worktree))

    touched_path = Path(session_core.session_dir(sid, cwd=str(worktree))) / "touched.txt"
    before = touched_path.read_text(encoding="utf-8")

    result = _call({"run_id": RUN_ID, "dry_run": True}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["dry_run"] is True

    after = touched_path.read_text(encoding="utf-8")
    assert after == before

    offer = safe_commit_offer.compute_offer(sid, cwd=str(worktree))
    assert old_rel in offer["safe_paths"]
