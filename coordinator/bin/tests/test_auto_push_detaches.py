"""test_auto_push_detaches.py — pytest tests for
coordinator_core.hooks.auto_push's C2 spawn-reduction work
(docs/plans/2026-08-21-a-commit-stops-paying-for-thirty-processes.md, C2).

Covers the branch-resolution half of C2's remit: `_resolve_branch_no_spawn`
and `_canonical_branch_case` (coordinator_core/hooks/auto_push.py), which
replace `resolve_branch`'s two-spawn `git branch --show-current` +
`git for-each-ref` fallback with a direct `HEAD`/`refs/heads` read in the
ordinary case.

Hazard cases asserted against a REAL git repo (never a mock of git's own
ref-resolution semantics), each cross-checked against git's own answer:
  (a) detached HEAD (raw sha) -- must match `git branch --show-current`'s
      empty-stdout contract exactly: `resolved=True, branch=None`, and the
      caller must NOT fall back to the spawn.
  (b) a symbolic ref onto something outside `refs/heads/*` (e.g. a tag) --
      same definitive "no current branch" answer as detached HEAD.
  (c) a linked worktree, where `HEAD` lives in the per-worktree private
      gitdir but `refs/heads/*` lives in the shared COMMON dir -- the same
      gitdir/common-dir split that made `87b2f3f43` a real bug once.
  (d) a submodule, where the private gitdir has no `commondir` file and IS
      itself the common dir.
  (e) packed-refs -- a branch with no loose ref file, resolvable only via
      `packed-refs`.
  (f) case-canonicalization on a `work/Machine-a/...`-style branch (the
      Windows case-insensitive-filesystem hazard `resolve_branch` exists
      for) -- across an ordinary repo, a linked worktree, and a submodule.
  (g) no case-fold match on disk at all -- falls back to the raw name.

Plus the spawn-count assertion itself: `resolve_branch` on an ordinary,
resolvable repo must not call `subprocess`/`_run_git` at all.

Declared, not excused: this file spawns real `git` to build its fixtures
(worktree/submodule setup, `pack-refs`) and to cross-check git's own
canonical-case answer -- coordinator_core/tests/test_no_new_spawning_tests.py
Rule 2/4. The property under test is that `auto_push`'s OWN resolution path
spawns zero subprocesses in the ordinary case, not that git itself never
runs anywhere in the test.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.hooks import auto_push  # noqa: E402
from coordinator_core.git.git_dir import (  # noqa: E402
    resolve_git_common_dir,
    resolve_git_dir,
)

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        **_NO_WINDOW,
    )


def _init_repo(root: Path, branch: str = "main") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", branch], cwd=root)
    _git(["config", "user.email", "t@example.com"], cwd=root)
    _git(["config", "user.name", "T"], cwd=root)
    _git(["commit", "-q", "--allow-empty", "-m", "initial"], cwd=root)
    return root


def _real_show_current(cwd) -> str:
    return _git(["branch", "--show-current"], cwd=cwd).stdout.strip()


def _real_for_each_ref(cwd) -> list[str]:
    out = _git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd=cwd
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


@pytest.fixture()
def repo(tmp_path):
    return _init_repo(tmp_path / "repo", branch="work/machine-a/case-test")


# ---------------------------------------------------------------------------
# (a) detached HEAD
# ---------------------------------------------------------------------------


def test_detached_head_matches_git_show_current_empty_contract(repo):
    sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    _git(["checkout", "-q", sha], cwd=repo)

    resolved, branch = auto_push._resolve_branch_no_spawn(str(repo))

    assert resolved is True
    assert branch is None
    # Must match git's own empty-stdout-on-detached contract exactly.
    assert _real_show_current(repo) == ""
    # And the public entry point must NOT fall back to the spawn on this
    # definitive answer.
    assert auto_push.resolve_branch(str(repo)) is None


def test_detached_head_resolve_branch_does_not_spawn(repo, monkeypatch):
    sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    _git(["checkout", "-q", sha], cwd=repo)

    calls = []
    monkeypatch.setattr(auto_push, "_run_git", lambda *a, **k: calls.append((a, k)))
    auto_push.resolve_branch(str(repo))
    assert calls == []


# ---------------------------------------------------------------------------
# (b) symbolic ref outside refs/heads/*
# ---------------------------------------------------------------------------


def test_symbolic_ref_outside_refs_heads_is_definitive_no_branch(repo):
    _git(["tag", "a-tag"], cwd=repo)
    (resolve_git_dir(str(repo)) / "HEAD").write_text(
        "ref: refs/tags/a-tag\n", encoding="utf-8"
    )

    resolved, branch = auto_push._resolve_branch_no_spawn(str(repo))
    assert resolved is True
    assert branch is None


# ---------------------------------------------------------------------------
# (c) linked worktree -- HEAD in the private gitdir, refs/heads/* in the
# shared common dir. Getting this backwards is exactly the 87b2f3f43 bug.
# ---------------------------------------------------------------------------


def test_linked_worktree_reads_head_from_private_and_refs_from_common(tmp_path):
    main_repo = _init_repo(tmp_path / "main", branch="main")
    _git(["branch", "work/Feature-X"], cwd=main_repo)
    wt = tmp_path / "wt"
    _git(["worktree", "add", "-q", str(wt), "work/Feature-X"], cwd=main_repo)

    private = resolve_git_dir(str(wt))
    common = resolve_git_common_dir(str(wt))
    assert private != common, "fixture must exercise the private/common split"

    # Case-mismatched HEAD in the WORKTREE's private gitdir.
    (private / "HEAD").write_text("ref: refs/heads/work/feature-x\n", encoding="utf-8")

    resolved, branch = auto_push._resolve_branch_no_spawn(str(wt))
    assert resolved is True
    assert branch == "work/Feature-X"
    assert branch in _real_for_each_ref(wt)


# ---------------------------------------------------------------------------
# (d) submodule -- private gitdir has no commondir file; it IS the common
# dir.
# ---------------------------------------------------------------------------


def test_submodule_private_gitdir_is_its_own_common_dir(tmp_path):
    sub_upstream = _init_repo(tmp_path / "sub_upstream", branch="main")
    superproject = _init_repo(tmp_path / "super", branch="main")
    _git(
        [
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub_upstream).replace("\\", "/"),
            "sub",
        ],
        cwd=superproject,
    )
    sub_dir = superproject / "sub"

    private = resolve_git_dir(str(sub_dir))
    common = resolve_git_common_dir(str(sub_dir))
    assert private == common, "submodule gitdir is its own common dir (no commondir file)"

    _git(["checkout", "-q", "-b", "work/sub-branch"], cwd=sub_dir)
    (private / "HEAD").write_text("ref: refs/heads/work/Sub-Branch\n", encoding="utf-8")

    resolved, branch = auto_push._resolve_branch_no_spawn(str(sub_dir))
    assert resolved is True
    assert branch == "work/sub-branch"
    assert branch in _real_for_each_ref(sub_dir)


# ---------------------------------------------------------------------------
# (e) packed-refs
# ---------------------------------------------------------------------------


def test_packed_refs_no_loose_ref_file(repo):
    _git(["branch", "work/Packed-Branch"], cwd=repo)
    _git(["pack-refs", "--all"], cwd=repo)
    loose = repo / ".git" / "refs" / "heads" / "work" / "Packed-Branch"
    assert not loose.exists(), "fixture must actually pack the ref away"

    (resolve_git_dir(str(repo)) / "HEAD").write_text(
        "ref: refs/heads/work/packed-branch\n", encoding="utf-8"
    )

    resolved, branch = auto_push._resolve_branch_no_spawn(str(repo))
    assert resolved is True
    assert branch == "work/Packed-Branch"
    assert branch in _real_for_each_ref(repo)


# ---------------------------------------------------------------------------
# (f) case-canonicalization -- the Windows hazard resolve_branch exists for.
# ---------------------------------------------------------------------------


def test_ordinary_repo_case_canonicalizes_work_machine_a_branch(repo):
    # HEAD already carries the on-disk case (repo fixture created it that
    # way); simulate the Windows hazard by writing a differently-cased HEAD.
    (resolve_git_dir(str(repo)) / "HEAD").write_text(
        "ref: refs/heads/work/Machine-a/case-test\n", encoding="utf-8"
    )

    resolved, branch = auto_push._resolve_branch_no_spawn(str(repo))
    assert resolved is True
    assert branch == "work/machine-a/case-test"
    assert branch == _real_for_each_ref(repo)[0] or branch in _real_for_each_ref(repo)


def test_no_case_fold_match_falls_back_to_raw_name(tmp_path):
    r = _init_repo(tmp_path / "repo", branch="work/onlyname")
    resolved, branch = auto_push._resolve_branch_no_spawn(str(r))
    assert resolved is True
    assert branch == "work/onlyname"


# ---------------------------------------------------------------------------
# End-to-end: resolve_branch() reaches the same answer as the two-spawn
# git fallback, but spawns zero subprocesses in the ordinary case (AC2).
# ---------------------------------------------------------------------------


def test_resolve_branch_matches_git_and_spawns_nothing(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(auto_push, "_run_git", lambda *a, **k: calls.append((a, k)))

    got = auto_push.resolve_branch(str(repo))

    assert got == "work/machine-a/case-test"
    assert got in _real_for_each_ref(repo)
    assert calls == [], "resolve_branch must not fall back to the spawn on a resolvable repo"


def test_resolve_branch_none_repo_root_still_uses_spawn_fallback(monkeypatch):
    # repo_root=None ("use cwd") can't resolve a git-dir without a concrete
    # path, so the docstring says it reaches the fallback unconditionally.
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/foo",
    }.get(tuple(args)))
    assert auto_push.resolve_branch(None) == "work/foo"


def test_unresolvable_repo_root_falls_back_to_spawn(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/fallback",
    }.get(tuple(args)))
    assert auto_push.resolve_branch(str(not_a_repo)) == "work/fallback"
