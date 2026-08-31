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
    """The count reaches the caller, not just stderr. A stderr line above a
    green verdict is not a report — the same ruling `_round_warnings` was
    written to enforce."""
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
    """Both end with an empty pathspec. Only one of them is a no-op, and the
    warning is what tells them apart."""
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
    # Names the count and the class, never a bare number: an operator reading
    # the verdict block has to be able to act on it without the stderr scroll.
    assert "dropped from the commit" in warning


def test_the_warning_reaches_the_verdict_block():
    """`_round_warnings` is what the verdict COUNTS and NAMES. A drop warning
    that never reaches it leaves `warnings: 0` on a round that carried
    nothing."""
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
    """The empty-`seen` early return and a real filtered run agree on shape,
    so no caller has to branch on `None` before it can count."""
    assert _mod._no_filter_drops() == {
        "gitignored": 0,
        "absent_deletion": 0,
        "staging": 0,
    }
    assert set(_mod._no_filter_drops()) == set(_mod._FILTER_DROP_LABELS)


def test_an_ignored_path_is_one_git_add_would_refuse(tmp_path):
    """Why the ignore branch STAYS, recorded as a test rather than a claim.

    The filed row proposed that `check-ignore` answers "is this ignored" when
    the question is "did the author mean to exclude it", and that a `!`
    negation inside an excluded directory makes those differ. It does make
    them differ in INTENT — and not in what git will do: git never descends
    into an excluded directory, so the negation does not re-include the file
    and `git add` refuses it exactly as `check-ignore` predicted. Dropping it
    loses no commit that could otherwise have happened.

    The two legs agree in the other direction too (`check-ignore` is
    index-aware, so it reports nothing for a TRACKED file matching an ignore
    pattern, and the filter leaves it in). The defect this file pins was never
    the drop; it was the round reporting the drop as a no-op.
    """
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
    """Third `.gitignore` negation shape, distinct from the two above.

    `build/` + `!build/keep/` (a real re-include) and `build/*` +
    `!build/keep/` both involve a negation that DOES something. This memo's
    trigger did not: `*.local.toml` + `!*.toml.example` over a file named
    `keep.toml.example`, which `*.local.toml` never matched in the first
    place (it requires the name to END in `.local.toml`). The negation is a
    no-op -- nothing was ignored to begin with -- and the file was never
    excluded (`git status` reports it `??`, untracked-not-ignored).

    `_filter_commit_pathspec` is check-ignore-driven (§ its own docstring:
    "git check-ignore is index-aware ... an ignored path here is one `git
    add` would refuse"), so this shape was never actually reachable through
    this filter -- check-ignore answers the real question regardless of an
    inert `!` line. Pinned here because the corpus had two shapes covered and
    a third, structurally different one (inert negation, no re-include, no
    prior exclusion) unpinned -- see
    2026-08-28-doe-claude-em-our-negation-instance-was-a-no-op-and-still-
    broke-the-commit.md.
    """
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
