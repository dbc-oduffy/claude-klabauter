"""test_workday_complete_step9_append_changelog.py — regression suite for
workday-complete-step9-append-changelog.py's Zone C (commit) and push-verify logic.

Covers two live defects observed during the 2026-07-23 /workday-complete run:

Defect 1 (push race vs. the repo's own auto-push hook): step9's own explicit
`git push` can lose a ref-lock race against the coordinator-auto-push hook that
already pushed the same HEAD on commit -- the hook's push and step9's push produce
an identical-looking "! [remote rejected] ... cannot lock ref" failure whether or
not the commit actually reached the remote. `_head_on_remote` disambiguates by
fetching and checking ancestry against `origin/<branch>` rather than trusting the
local push exit code alone.

Defect 2 (bare `git commit` absorbs whatever else is staged): a bare `git commit`
with no pathspec sweeps the ENTIRE index, which is unsafe in this repo's routine
4-5-concurrent-EM-session shared working tree -- a peer staging a file mid-commit
would be silently absorbed into step9's changelog commit. `_commit_frozen_paths`
takes an explicit, pre-frozen pathspec (step9's own outputs UNIONED with whatever
was staged BEFORE step9 ran, per the Step 2.6/4.5 pre-stage contract) so a path
staged during step9's own execution can never be swept in.

Spec backlink: coordinator/bin/workday-complete-step9-append-changelog.py
    `_commit_frozen_paths`, `_head_on_remote`, `_get_staged_paths`, `_to_repo_relative`
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(
    _REPO_ROOT, "coordinator", "bin", "workday-complete-step9-append-changelog.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "workday_complete_step9_append_changelog", _TARGET
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    )


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Bare `origin` repo + a clone with `work/test` checked out and an initial
    commit already pushed -- mirrors the work/* branch shape the auto-push hook
    and step9's own push both target."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "work/test", str(origin)], check=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "checkout", "-q", "-B", "work/test")
    (clone / "README.md").write_text("hello\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", "initial")
    _git(clone, "push", "-q", "-u", "origin", "work/test")
    return origin, clone


# ---------------------------------------------------------------------------
# Defect 1: _head_on_remote push-race verification
# ---------------------------------------------------------------------------

def test_head_on_remote_true_when_commit_already_pushed(mod, tmp_path):
    """Commit succeeded and HEAD is already on the remote (e.g. a racing
    auto-push hook won first) -- must report True, the caller's cue to treat
    a subsequent local push failure as NOT a real failure."""
    _origin, clone = _make_origin_and_clone(tmp_path)
    (clone / "file.txt").write_text("content\n")
    _git(clone, "add", "file.txt")
    _git(clone, "commit", "-q", "-m", "second")
    # Simulate the auto-push hook having already pushed this exact HEAD,
    # out from under step9's own (about-to-race) push attempt.
    _git(clone, "push", "-q", "origin", "work/test")

    assert mod._head_on_remote(str(clone), "work/test") is True


def test_head_on_remote_false_when_commit_never_reached_remote(mod, tmp_path):
    """Commit succeeded locally but genuinely never reached the remote -- must
    report False, the genuine-failure case that should still exit 2."""
    _origin, clone = _make_origin_and_clone(tmp_path)
    (clone / "file.txt").write_text("content\n")
    _git(clone, "add", "file.txt")
    _git(clone, "commit", "-q", "-m", "second - never pushed")

    assert mod._head_on_remote(str(clone), "work/test") is False


# ---------------------------------------------------------------------------
# Defect 2: _commit_frozen_paths explicit-pathspec commit
# ---------------------------------------------------------------------------

def test_third_party_path_staged_after_snapshot_is_excluded(mod, tmp_path):
    """A path staged by a third party AFTER step9 captured its pre-staged
    snapshot must NOT be swept into step9's commit."""
    _origin, clone = _make_origin_and_clone(tmp_path)

    # step9 captures its frozen snapshot here (nothing staged yet).
    pre_staged = mod._get_staged_paths(str(clone))
    assert pre_staged == []

    # step9 produces + stages its own output file.
    (clone / "own-output.md").write_text("step9 block\n")
    _git(clone, "add", "own-output.md")

    # A concurrent peer stages an unrelated file DURING step9's own execution --
    # this must never appear in step9's commit.
    (clone / "peer-file.md").write_text("peer content\n")
    _git(clone, "add", "peer-file.md")

    own_rel = [mod._to_repo_relative(str(clone), str(clone / "own-output.md"))]
    commit_paths = sorted(set(pre_staged) | set(own_rel))

    sha = mod._commit_frozen_paths(str(clone), commit_paths, "chore: test commit")
    assert sha is not None

    committed_files = _git(clone, "show", "--stat", "--name-only", "--format=", "HEAD").stdout.split()
    assert "own-output.md" in committed_files
    assert "peer-file.md" not in committed_files

    # peer-file.md remains staged, untouched, for its own owner to commit.
    still_staged = mod._get_staged_paths(str(clone))
    assert "peer-file.md" in still_staged


def test_pre_staged_paths_before_step9_are_included(mod, tmp_path):
    """Paths pre-staged BEFORE step9 runs (the Step 2.6/4.5 completion-reconcile
    contract) must be swept into step9's commit."""
    _origin, clone = _make_origin_and_clone(tmp_path)

    # Step 2.6/4.5 pre-stage a completion-log path before step9 starts.
    (clone / "completion-entry.md").write_text("completion entry\n")
    _git(clone, "add", "completion-entry.md")

    # step9 captures its frozen snapshot -- this DOES include the pre-staged path.
    pre_staged = mod._get_staged_paths(str(clone))
    assert pre_staged == ["completion-entry.md"]

    # step9 produces + stages its own output file.
    (clone / "own-output.md").write_text("step9 block\n")
    _git(clone, "add", "own-output.md")

    own_rel = [mod._to_repo_relative(str(clone), str(clone / "own-output.md"))]
    commit_paths = sorted(set(pre_staged) | set(own_rel))

    sha = mod._commit_frozen_paths(str(clone), commit_paths, "chore: test commit")
    assert sha is not None

    committed_files = _git(clone, "show", "--stat", "--name-only", "--format=", "HEAD").stdout.split()
    assert "own-output.md" in committed_files
    assert "completion-entry.md" in committed_files


def test_commit_frozen_paths_returns_none_when_nothing_staged(mod, tmp_path):
    _origin, clone = _make_origin_and_clone(tmp_path)
    assert mod._commit_frozen_paths(str(clone), [], "chore: test commit") is None
