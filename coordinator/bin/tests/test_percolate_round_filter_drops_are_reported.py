"""test_percolate_round_filter_drops_are_reported — pins the distinction
between "the commit-pathspec filter removed every path this round declared"
and "this round declared nothing".

The two produce the same empty pathspec, and the round's no-op branch stated
the second on evidence for the first: `real run reported no changed files;
nothing to commit`. Four consecutive publishes copied files to dest, committed
none, and reported `Rows succeeded: 6/6, Warnings: 0` (DoE-claude, filed as
state/bug-backlog/2026-08-28-the-publish-stager-drops-a-declared-path-under-a-
gitignore-negation.yaml).

`_report_commit_residual` does not cover this and cannot: publish.py's change
lines compare the transformed staging dir against dest's WORKING TREE, so once
the copy has landed the two agree and `real_changes` is empty. A filtered-to-
empty pathspec is then 0-vs-0 -- agreement -- and the divergence warning
correctly stays silent. The signal has to come from the filter's own count,
which is why `_filter_commit_pathspec` now returns one.

NEGATIVE SPEC — none of this makes a drop an error, and no test here asserts a
refusal. `_round_warnings` owns that boundary (a warning degrades the verdict,
it never refuses the push) and each drop class is legitimate. What is pinned is
that the round SAYS which of the two zeroes it hit.

Unit-level only, same posture as test_percolate_round_commit_pathspec.py:
exercises the functions directly against real temp git repos, never via a
subprocess percolate round and never against a live mirror.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_filter_drops_are_reported.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_filter_drops", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "dest"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    return repo


def test_filter_reports_the_class_it_dropped(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (repo / "cached.pyc").write_text("x", encoding="utf-8")
    seen = {str(repo / "cached.pyc"): ("MODIFY", "cached.pyc")}

    kept, drops = _mod._filter_commit_pathspec(repo, str(repo), seen, repo_root=str(repo))

    assert kept == []
    assert drops["gitignored"] == 1
    assert drops["absent_deletion"] == 0
    assert drops["staging"] == 0


def test_a_declared_nothing_round_and_a_filtered_to_empty_round_differ(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (repo / "cached.pyc").write_text("x", encoding="utf-8")

    declared_nothing, no_drops = _mod._filter_commit_pathspec(
        repo, str(repo), {}, repo_root=str(repo)
    )
    filtered_empty, real_drops = _mod._filter_commit_pathspec(
        repo,
        str(repo),
        {str(repo / "cached.pyc"): ("MODIFY", "cached.pyc")},
        repo_root=str(repo),
    )

    assert declared_nothing == filtered_empty == []
    assert _mod._filter_drop_warning(no_drops) is None

    warning = _mod._filter_drop_warning(real_drops)
    assert warning is not None
    assert "1 gitignored at dest" in warning
    assert "dropped from the commit" in warning


def test_the_warning_reaches_the_verdict_block():
    assert (
        _mod._round_warnings(
            has_review_warnings=False, residual_warning=None, filter_drop_warning=None
        )
        == []
    )

    only_drops = _mod._round_warnings(
        has_review_warnings=False,
        residual_warning=None,
        filter_drop_warning="3 declared path(s) were dropped",
    )
    assert only_drops == ["3 declared path(s) were dropped"]

    all_three = _mod._round_warnings(
        has_review_warnings=True,
        residual_warning="57 change(s) NOT committed",
        filter_drop_warning="3 declared path(s) were dropped",
    )
    assert len(all_three) == 3


def test_no_filter_drops_is_the_shape_every_return_path_hands_back():
    assert _mod._no_filter_drops() == {
        "gitignored": 0,
        "absent_deletion": 0,
        "staging": 0,
        "declared_residue": 0,
    }
    assert set(_mod._no_filter_drops()) == set(_mod._FILTER_DROP_LABELS)


def test_gitignored_residue_no_row_wrote_is_named_but_not_a_warning(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("*.bak\n", encoding="utf-8")
    (repo / "junk.bak").write_text("x", encoding="utf-8")
    (repo / "written.bak").write_text("x", encoding="utf-8")
    seen = {
        str(repo / "junk.bak"): (_mod._DECLARED_ONLY_TAG, "junk.bak"),
        str(repo / "written.bak"): ("NEW", "written.bak"),
    }

    kept, drops = _mod._filter_commit_pathspec(repo, str(repo), seen, repo_root=str(repo))

    assert kept == []
    assert drops["declared_residue"] == 1
    assert drops["gitignored"] == 1
    warning = _mod._filter_drop_warning(drops)
    assert warning is not None and warning.startswith("1 declared path(s)")
    assert _mod._filter_drop_warning({**_mod._no_filter_drops(), "declared_residue": 15}) is None


def test_declared_only_untracked_path_that_is_not_ignored_is_still_carried(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "stranded.py").write_text("x = 1\n", encoding="utf-8")
    seen = {str(repo / "stranded.py"): (_mod._DECLARED_ONLY_TAG, "stranded.py")}

    kept, drops = _mod._filter_commit_pathspec(repo, str(repo), seen, repo_root=str(repo))

    assert kept == ["stranded.py"]
    assert not any(drops.values())


def test_an_ignored_path_is_one_git_add_would_refuse(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n!build/keep/\n", encoding="utf-8")
    (repo / "build" / "keep").mkdir(parents=True)
    (repo / "build" / "keep" / "wanted.txt").write_text("x", encoding="utf-8")

    seen = {
        str(repo / "build" / "keep" / "wanted.txt"): ("MODIFY", "build/keep/wanted.txt")
    }
    kept, drops = _mod._filter_commit_pathspec(repo, str(repo), seen, repo_root=str(repo))

    assert kept == []
    assert drops["gitignored"] == 1

    added = _git(repo, "add", "--", "build/keep/wanted.txt")
    assert added.returncode != 0, (
        "git accepted a path check-ignore called ignored -- the filter's premise "
        "is broken and the drop really would lose a commit"
    )


def test_a_negation_matching_nothing_never_drops_the_file_it_names(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("*.local.toml\n\n!*.toml.example\n", encoding="utf-8")
    (repo / "keep.toml.example").write_text("x", encoding="utf-8")

    status = _git(repo, "status", "--porcelain", "--", "keep.toml.example")
    assert status.stdout.strip().startswith("??"), (
        "keep.toml.example must be untracked-not-ignored for this to be the "
        "no-op-negation shape -- if it shows anything else the fixture no "
        "longer represents the memo's trigger"
    )

    seen = {str(repo / "keep.toml.example"): ("MODIFY", "keep.toml.example")}
    kept, drops = _mod._filter_commit_pathspec(repo, str(repo), seen, repo_root=str(repo))

    assert kept == ["keep.toml.example"]
    assert drops["gitignored"] == 0

    added = _git(repo, "add", "--", "keep.toml.example")
    assert added.returncode == 0, (
        "git refused a path the no-op negation never excluded -- the fixture "
        "does not reproduce the memo's trigger"
    )
