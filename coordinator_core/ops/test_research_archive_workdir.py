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
import datetime
import errno
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import research_archive_workdir as mod
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Declared, not excused: `_mk_git_worktree` builds a REAL git repo (unlike this
# file's default plain-.git `_mk_worktree`) because `safe_commit_offer.compute_offer`
# genuinely shells out to `git diff`/`git ls-files` to compute the dirty-tree side of
# a session's scope -- no mock stands in for that real diff/ls-files behaviour. Only
# the tests exercising that offer path use it, each via its own tmp_path repo, so
# there is no shared state to hoist. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN_ID = "run-20260722-abc123"
TOPIC = "warp-core-ejection"
WORKDIR_NAME = f"{RUN_ID}-{TOPIC}-workdir"
RUN_NAME = f"{datetime.date.today().isoformat()}-{TOPIC}"
DEST_REL = f"docs/research/{TOPIC}/{RUN_NAME}/workdir"


def _dest(worktree: Path) -> Path:
    return worktree / DEST_REL


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
    dest = _dest(worktree)
    assert result["exit_code"] == 0
    assert result["archived"] is True
    assert result["already_archived"] is False
    assert result["dest"] == str(dest)
    assert not src.exists()
    assert (dest / "synthesis.md").read_text(encoding="utf-8") == "findings\n"
    assert (dest / "phase-1" / "notes.md").exists()
    assert not any((worktree / "docs" / "research" / "archive").iterdir())


def test_topic_param_selects_topic_dir(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    result = _call({"run_id": RUN_ID, "topic": "ue-gas"}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["dest"] == str(
        worktree / "docs" / "research" / "ue-gas" / RUN_NAME / "workdir"
    )
    assert _call({"run_id": RUN_ID, "topic": "Bad Topic"}, worktree / ".git")["exit_code"] == 1


def test_trail_run_dir_wins_and_later_day_rerun_finds_dest(tmp_path):
    """The run dir comes from the close's trail, not today's date: a rerun on
    a later day still resolves the archived dest (already-archived no-op)."""
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    run_dir = worktree / "docs" / "research" / "ue-gas" / "2026-01-02-warp-core-ejection"
    run_dir.mkdir(parents=True)
    (run_dir / "trail.yaml").write_text(f'run_id: "{RUN_ID}"\n', encoding="utf-8")
    first = _call({"run_id": RUN_ID}, worktree / ".git")
    assert first["exit_code"] == 0
    assert first["dest"] == str(run_dir / "workdir")
    assert (run_dir / "workdir" / "synthesis.md").exists()
    second = _call({"run_id": RUN_ID}, worktree / ".git")
    assert second["already_archived"] is True
    assert second["dest"] == str(run_dir / "workdir")
    assert not (worktree / "docs" / "research" / "ue-gas" / RUN_NAME).exists()


def test_legacy_flat_trail_falls_back_to_topic_today_slug(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    legacy = worktree / "docs" / "research" / "trail"
    legacy.mkdir()
    (legacy / f"{RUN_ID}.yaml").write_text(f'run_id: "{RUN_ID}"\n', encoding="utf-8")
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["dest"] == str(_dest(worktree))


def test_legacy_archived_dir_still_reads_as_already_archived(tmp_path):
    worktree = _mk_worktree(tmp_path)
    legacy = worktree / "docs" / "research" / "archive" / f"{RUN_ID}-{TOPIC}"
    legacy.mkdir(parents=True)
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["already_archived"] is True
    assert result["dest"] == str(legacy)


def _mk_trail(worktree: Path) -> None:
    """The trail ``research.close`` leaves in the run dir; a rerun resolves the dest through it."""
    run_dir = _dest(worktree).parent
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "trail.yaml").write_text(f'run_id: "{RUN_ID}"\n', encoding="utf-8")


def test_double_invocation_is_already_archived_noop(tmp_path):
    """CC-4: second identical call is a safe no-op with the documented shape."""
    worktree = _mk_worktree(tmp_path)
    _mk_workdir(worktree)
    _mk_trail(worktree)
    first = _call({"run_id": RUN_ID}, worktree / ".git")
    assert first["archived"] is True
    second = _call({"run_id": RUN_ID}, worktree / ".git")
    dest = _dest(worktree)
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
    stale = _dest(worktree).parent / f".tmp-archive-{RUN_ID}"
    (stale / "partial").mkdir(parents=True)
    (stale / "partial" / "halfcopied.md").write_text("junk", encoding="utf-8")
    result = _call({"run_id": RUN_ID}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["archived"] is True
    assert not stale.exists()
    assert (_dest(worktree) / "synthesis.md").exists()


def test_crash_window_b_content_equal_completes_pending_rmtree(tmp_path):
    """Settlement A1 window (b): both exist + equal → finish rmtree(src)."""
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)
    dest = _dest(worktree)
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
    dest = _dest(worktree)
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
    _mk_trail(worktree)
    dest = _dest(worktree)
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
    stale = _dest(worktree).parent / f".tmp-archive-{RUN_ID}"
    stale.mkdir(parents=True)
    result = _call({"run_id": RUN_ID, "dry_run": True}, worktree / ".git")
    dest = _dest(worktree)
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
    _mk_trail(worktree)
    _call({"run_id": RUN_ID}, worktree / ".git")
    result = _call({"run_id": RUN_ID, "dry_run": True}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["archived"] is False
    assert result["already_archived"] is True
    assert result["would_action"] == "none"


def test_dry_run_pending_cleanup_reports_and_defers(tmp_path):
    worktree = _mk_worktree(tmp_path)
    src = _mk_workdir(worktree)
    dest = _dest(worktree)
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
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
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
    new_rel = f"{DEST_REL}/phase-1/notes.md"
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

    # `touch-record.jsonl` is the live sink -- `touched.txt` is the retired
    # legacy dialect (coordinator_core/ops/session/legacy_touch_corpus_migrate.py);
    # session_scope._TOUCH_RECORD_FILENAME is the same indirection every
    # other test in this suite uses (e.g. test_safe_commit_offer.py) so the
    # filename can move again without a second stale literal to chase.
    touched_path = (
        Path(session_core.session_dir(sid, cwd=str(worktree)))
        / session_scope._TOUCH_RECORD_FILENAME
    )
    before = touched_path.read_text(encoding="utf-8")

    result = _call({"run_id": RUN_ID, "dry_run": True}, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["dry_run"] is True

    after = touched_path.read_text(encoding="utf-8")
    assert after == before

    offer = safe_commit_offer.compute_offer(sid, cwd=str(worktree))
    assert old_rel in offer["safe_paths"]
