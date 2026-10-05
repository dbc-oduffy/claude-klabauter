"""coordinator_core.consolidate_assemble.tests.test_cherry_pick_and_delete —
coverage for `apply._dispatch_cherry_pick_and_delete` and its conflict
cleanup (`apply._clean_cherry_pick_conflict`).

Slice s7 review (state/subagent-share/a3d742ff-223c-4133-aedd-ed60ce61b558/
amp-review-s7.md) flagged this call site as having ZERO test coverage of any
kind — not the batched happy path, not the empty-`shas` guard, not the
failure path. Every fixture here uses a REAL git repo under `tmp_path` (never
a stub of `_run_git`) because the defect under test — `--quit` leaving
unmerged index entries and live `<<<<<<<` conflict markers in the working
tree — is a property of real git state that a stubbed `_run_git` cannot
reproduce.

Would each test fail against the pre-fix code (`git cherry-pick --quit` with
no follow-up cleanup)?
    - `test_conflict_cleanup_leaves_a_clean_tree_scoped_to_conflicted_paths`:
      yes — pre-fix, `git status --porcelain` after the failed cherry-pick
      still reports `UU conflict.py` and the file still contains
      `<<<<<<<` markers; this test asserts both are gone.
    - `test_conflict_cleanup_never_touches_an_unrelated_dirty_file`: this is
      the negative control for the review's OWN forbidden remedy
      (`--abort` / tree-wide `reset --hard` / `checkout -- .`) — it would
      fail against a tree-wide-reset "fix" exactly as much as against the
      original defect, since either wipes the untouched peer file this test
      plants.
    - `test_quit_failure_is_surfaced`: yes — pre-fix, `--quit`'s exit status
      is discarded entirely (Finding 2), so a failing `--quit` is silently
      swallowed instead of raising.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.consolidate_assemble import apply as apply_mod
from coordinator_core.win_portability import no_console_creationflags

# Every fixture spawns real `git` subprocesses against a tmp repo.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "branch", "-M", "work")
    return root


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _status(root: Path) -> str:
    return _git(root, "status", "--porcelain").stdout


# ---------------------------------------------------------------------------
# Batched happy path
# ---------------------------------------------------------------------------

def test_batched_happy_path_applies_every_commit_and_deletes_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _init_repo(tmp_path)
    _write(root, "base.txt", "base\n")
    _commit_all(root, "seed")

    _git(root, "checkout", "-q", "-b", "feature")
    _write(root, "a.txt", "a\n")
    _commit_all(root, "add a")
    _write(root, "b.txt", "b\n")
    _commit_all(root, "add b")
    _git(root, "checkout", "-q", "work")

    # Cherry-picking creates NEW commit shas with identical trees, never the
    # original `feature` commits themselves -- `git branch -d`'s "fully
    # merged" ancestry check compares actual commit identity, so a safe
    # delete correctly refuses here. Production selects the force delete via
    # COORDINATOR_OVERRIDE_BRANCH for exactly this reason (see this module's
    # docstring); mirror that rather than asserting the safe-delete path.
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "feature")

    result = apply_mod._dispatch_cherry_pick_and_delete(["feature", "feature"], root)

    assert result["cli"] == "cherry-pick-and-delete"
    assert len(result["commits"]) == 2
    assert (root / "a.txt").exists()
    assert (root / "b.txt").exists()
    # Branch deleted (safe delete succeeds -- feature is now fully merged
    # into work via the cherry-picks).
    assert result["local_deleted"] == "feature"
    branches = _git(root, "branch").stdout
    assert "feature" not in branches


def test_empty_shas_guard_skips_cherry_pick_and_only_deletes(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _write(root, "base.txt", "base\n")
    _commit_all(root, "seed")
    # "already-merged" branches at the exact same commit as work -- no
    # commits in `work..already-merged`, so `_unique_commit_shas` returns [].
    _git(root, "branch", "already-merged")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")

    result = apply_mod._dispatch_cherry_pick_and_delete(
        ["already-merged", "already-merged"], root
    )

    assert result["commits"] == []
    assert result["local_deleted"] == "already-merged"


# ---------------------------------------------------------------------------
# Conflict / failure path — the scoped cleanup under review
# ---------------------------------------------------------------------------

def _make_conflicting_repo(tmp_path: Path) -> Path:
    """`work` and `feature` both edit the same line of the same file
    differently from a shared base -- guarantees a real cherry-pick conflict,
    never a synthetic/stubbed one."""
    root = _init_repo(tmp_path)
    _write(root, "conflict.py", "line1\nline2\nline3\n")
    _commit_all(root, "seed")

    _git(root, "checkout", "-q", "-b", "feature")
    _write(root, "conflict.py", "line1\nfeature-line2\nline3\n")
    _commit_all(root, "feature changes line2")

    _git(root, "checkout", "-q", "work")
    _write(root, "conflict.py", "line1\nwork-line2\nline3\n")
    _commit_all(root, "work changes line2 differently")
    return root


def test_conflict_cleanup_leaves_a_clean_tree_scoped_to_conflicted_paths(tmp_path: Path) -> None:
    root = _make_conflicting_repo(tmp_path)

    with pytest.raises(RuntimeError, match="cherry-pick"):
        apply_mod._dispatch_cherry_pick_and_delete(["feature", "feature"], root)

    # The defect this test exists to catch: pre-fix, `--quit` alone leaves
    # `UU conflict.py` and live `<<<<<<<` markers in the file.
    assert _status(root) == ""
    assert "<<<<<<<" not in (root / "conflict.py").read_text(encoding="utf-8")
    # Sequencer cleared -- the next cherry-pick on this tree must not be
    # refused as "already in progress" (a repo mid-sequence makes even
    # `--quit` itself fail with "no cherry-pick in progress" is NOT the
    # failure mode here; the failure mode this guards is the opposite --
    # a lingering sequencer that blocks a fresh cherry-pick).
    assert not (root / ".git" / "CHERRY_PICK_HEAD").exists()
    assert not (root / ".git" / "sequencer").exists()


def test_conflict_cleanup_never_touches_an_unrelated_dirty_file(tmp_path: Path) -> None:
    """Negative control for the review's forbidden remedy: a tree-wide
    `--abort` / `reset --hard` / `checkout -- .` would wipe this untouched,
    unrelated dirty file exactly as this test would catch."""
    root = _make_conflicting_repo(tmp_path)
    _write(root, "peer_session_work.txt", "uncommitted peer edit\n")

    with pytest.raises(RuntimeError, match="cherry-pick"):
        apply_mod._dispatch_cherry_pick_and_delete(["feature", "feature"], root)

    assert (root / "peer_session_work.txt").read_text(encoding="utf-8") == (
        "uncommitted peer edit\n"
    )
    status = _status(root)
    assert "peer_session_work.txt" in status
    assert "conflict.py" not in status


def test_quit_failure_is_surfaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_conflicting_repo(tmp_path)

    real_run_git = apply_mod._run_git

    def _fake_run_git(args: list[str], cwd: Path):
        if args[:2] == ["cherry-pick", "--quit"]:
            return subprocess.CompletedProcess(
                ["git", *args], 128, stdout="", stderr="fatal: could not lock ref"
            )
        return real_run_git(args, cwd)

    monkeypatch.setattr(apply_mod, "_run_git", _fake_run_git)

    with pytest.raises(RuntimeError, match="cherry-pick --quit"):
        apply_mod._dispatch_cherry_pick_and_delete(["feature", "feature"], root)


# ---------------------------------------------------------------------------
# Delete only when the tip is reachable from origin/<base>
# ---------------------------------------------------------------------------

def _origin_with_branch(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    root = _init_repo(tmp_path)
    _git(root, "branch", "-M", "main")
    _write(root, "base.txt", "base\n")
    _commit_all(root, "seed")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "checkout", "-q", "-b", "feat")
    _write(root, "a.txt", "a\n")
    _commit_all(root, "a")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--ff-only", "feat")
    _git(root, "push", "-q", "origin", "main", "feat")
    return root


def test_snapshot_merged_then_advanced_branch_is_refused(tmp_path: Path) -> None:
    root = _origin_with_branch(tmp_path)
    _git(root, "checkout", "-q", "feat")
    for n in ("b", "c"):
        _write(root, f"{n}.txt", n)
        _commit_all(root, n)
    _git(root, "push", "-q", "origin", "feat")
    _git(root, "checkout", "-q", "main")
    tip = _git(root, "rev-parse", "origin/feat").stdout.strip()

    with pytest.raises(RuntimeError) as exc:
        apply_mod._delete_branch("feat", True, root)

    assert tip in str(exc.value)
    assert "has 2 commits" in str(exc.value)
    assert _git(root, "ls-remote", "origin", "feat").stdout.strip()
    assert "feat" in _git(root, "branch").stdout


def test_fully_merged_branch_is_deleted_local_and_remote(tmp_path: Path) -> None:
    root = _origin_with_branch(tmp_path)

    detail = apply_mod._delete_branch("feat", True, root)

    assert detail["local_deleted"] == "feat"
    assert detail["remote_deleted"] == "feat"
    assert not _git(root, "ls-remote", "origin", "feat").stdout.strip()


def test_clean_path_check_is_two_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _origin_with_branch(tmp_path)
    calls: list[list[str]] = []
    real = apply_mod._run_git
    monkeypatch.setattr(apply_mod, "_run_git", lambda a, c: (calls.append(a), real(a, c))[1])

    assert apply_mod._refuse_unmerged_tips("feat", True, root) is None
    assert len(calls) == 2  # lookup + one rev-list (local and remote tips equal)


def _feat_cherry_picked_to_origin_main(tmp_path: Path, extra: bool) -> Path:
    root = _origin_with_branch(tmp_path)
    _git(root, "checkout", "-q", "feat")
    for n in ("b", "c"):
        _write(root, f"{n}.txt", n)
        _commit_all(root, n)
    picked = _git(root, "rev-list", "--reverse", "main..feat").stdout.split()
    if extra:
        _write(root, "d.txt", "d")
        _commit_all(root, "d")
    _git(root, "push", "-q", "origin", "feat")
    _git(root, "checkout", "-q", "main")
    # A later committer date forces new SHAs; within one clock second the
    # pick reproduced the originals and hid the defect half the time.
    subprocess.run(
        ["git", "cherry-pick", *picked], cwd=str(root), check=True, capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2030-01-01T00:00:00Z"},
        **no_console_creationflags(),
    )
    _git(root, "push", "-q", "origin", "main")
    return root


def test_branch_cherry_picked_onto_origin_main_under_new_shas_is_deleted(tmp_path: Path) -> None:
    root = _feat_cherry_picked_to_origin_main(tmp_path, extra=False)

    detail = apply_mod._delete_branch("feat", True, root)

    assert detail["remote_deleted"] == "feat"
    assert not _git(root, "ls-remote", "origin", "feat").stdout.strip()


def test_branch_with_one_uncherry_picked_commit_is_refused(tmp_path: Path) -> None:
    root = _feat_cherry_picked_to_origin_main(tmp_path, extra=True)

    with pytest.raises(RuntimeError, match="has 1 commits"):
        apply_mod._delete_branch("feat", True, root)

    assert _git(root, "ls-remote", "origin", "feat").stdout.strip()
