"""`directives_memo_lifecycle.memo_paths_dirty` -- DR-419's CHEAPEN-IN-PLACE
ruling for the memo-lifecycle git-status row
(`docs/decisions/DR-419-what-may-run-at-close-time.md` § C1).

Origin: docs/plans/2026-08-25-the-close-ceremony-inside-the-brightline.md,
chunk C3. DR-419 rules the whole-worktree `git status --porcelain`
`directives_memo_lifecycle.py :: _run_git` issued (203.1ms, census-measured)
must be replaced, for the "are memo files dirty" question, with a
pathspec-scoped `git status --porcelain -- <memo corpus root>` -- never a
narrowing of `_git_status_porcelain`/`classify_session_authored_files`
itself, which genuinely needs every dirty path for the unrelated Step 2.67
session-authored-file predicate (see `memo_paths_dirty`'s own docstring).

These tests pin: (1) a dirty memo-corpus path is detected, (2) a dirty path
OUTSIDE the memo corpus is NOT detected (the scoping is real, not
cosmetic), (3) a clean repo returns False, (4) the underlying `git status
--porcelain` call this function issues carries a `--` pathspec restricted to
the memo corpus root, never an unscoped worktree-wide call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_passthrough_kwargs
from coordinator_core.workstream_complete import directives_memo_lifecycle

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, **no_console_passthrough_kwargs())
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True, **no_console_passthrough_kwargs())


def test_memo_paths_dirty_detects_dirty_memo_file(tmp_path):
    _init_git_repo(tmp_path)
    inbox = tmp_path / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "some-memo.md").write_text("---\nstatus: open\n---\nbody\n", encoding="utf-8")

    assert directives_memo_lifecycle.memo_paths_dirty(tmp_path) is True


def test_memo_paths_dirty_ignores_dirty_path_outside_memo_corpus(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "unrelated-scratch.md").write_text("not a memo\n", encoding="utf-8")

    assert directives_memo_lifecycle.memo_paths_dirty(tmp_path) is False


def test_memo_paths_dirty_false_on_clean_repo(tmp_path):
    _init_git_repo(tmp_path)

    assert directives_memo_lifecycle.memo_paths_dirty(tmp_path) is False


def test_memo_paths_dirty_scopes_git_status_to_memo_corpus_root(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    captured: list[list[str]] = []
    real_run_git = directives_memo_lifecycle._run_git

    def _spy(repo_root, args):
        captured.append(list(args))
        return real_run_git(repo_root, args)

    monkeypatch.setattr(directives_memo_lifecycle, "_run_git", _spy)

    directives_memo_lifecycle.memo_paths_dirty(tmp_path)

    assert len(captured) == 1
    args = captured[0]
    assert args[0] == "status"
    assert args[1] == "--porcelain"
    assert "--" in args, "must issue a pathspec-scoped call, never a bare `git status --porcelain`"
    dash_idx = args.index("--")
    pathspec = args[dash_idx + 1 :]
    assert pathspec, "the `--` pathspec separator must be followed by the memo corpus root"
    assert all("state/cross-repo" in p or "cross-repo" in p for p in pathspec)
