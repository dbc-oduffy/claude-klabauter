"""One directory reached by two spellings is one repo, not two.

Regression for dbc-oduffy/claude-klabauter#2, reported from a first-time
macOS install: `_to_posix_key` folds only the Windows drive letter, by
design (folding the rest would collapse two genuinely distinct paths on
case-sensitive POSIX). On a case-INSENSITIVE filesystem that left
`~/code/repo` and `~/Code/repo` as two dedup keys for one directory,
discovery emitted the repo twice, and registration downstream wrote both,
last-wins.

NOT A macOS DEFECT. APFS is where it was found, not where it lives: NTFS is
case-insensitive by default too, and these tests reproduce the double-count
on Windows. A fix scoped to "case-insensitive POSIX" would land half-done,
so the condition is the FILESYSTEM's behaviour and never the platform's
name -- which is also why nothing here branches on `os.name`.

These tests are written to assert the CORRECT answer on either kind of
filesystem rather than to skip on one: on a case-insensitive volume the two
spellings must collapse to one emission, and on a case-sensitive volume the
two spellings are two real directories and must both survive. The same
`dir_identity` code has to produce both outcomes, which is the property that
makes it safe to publish to boxes we do not control.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.discover_working_repos import _gate_and_dedup
from coordinator_core.path_identity import dir_identity, same_dir


def _make_repo(path: str) -> None:
    """A directory `_is_git_root` accepts, built without spawning `git init`.

    `_is_git_root` delegates to `repo_root.show_toplevel`, which walks up
    for a `.git` entry and only spawns when the walk finds nothing. A bare
    `.git` directory satisfies the walk, so these tests stay in the fast
    tier and out of the spawn-budget accounting.
    """
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)


def _case_variant(path: str) -> str:
    """`path` with its LAST component's case flipped."""
    head, tail = os.path.split(path)
    return os.path.join(head, tail.upper() if tail.islower() else tail.lower())


def _fs_is_case_insensitive(variant: str) -> bool:
    """Whether `variant` -- a case-flipped spelling of a directory that
    exists -- reaches that same directory on this filesystem."""
    return os.path.isdir(variant)


@pytest.fixture()
def repo_and_variant(tmp_path):
    repo = str(tmp_path / "coordinator-claude")
    _make_repo(repo)
    variant = _case_variant(repo)
    assert variant != repo
    return repo, variant


def test_two_spellings_of_one_directory_yield_one_repo(repo_and_variant):
    repo, variant = repo_and_variant
    if not _fs_is_case_insensitive(variant):
        pytest.skip("case-sensitive filesystem: the two spellings are two directories")

    emitted = list(_gate_and_dedup([repo, variant], mirror_keys=set()))

    assert len(emitted) == 1, (
        f"one directory reached by two spellings emitted {len(emitted)} repos: {emitted}"
    )


def test_two_genuinely_distinct_directories_both_survive(repo_and_variant):
    """The other half of the contract: dedup must not fold case ITSELF.

    On a case-sensitive filesystem the case-variant spelling is a second,
    real repo, and collapsing it would be the mirror-image defect -- one
    that a `casefold()`-shaped fix would have shipped.
    """
    repo, variant = repo_and_variant
    if _fs_is_case_insensitive(variant):
        pytest.skip("case-insensitive filesystem: the variant is not a distinct directory")
    _make_repo(variant)

    emitted = list(_gate_and_dedup([repo, variant], mirror_keys=set()))

    assert len(emitted) == 2, (
        f"two distinct directories collapsed to {len(emitted)}: {emitted}"
    )


def test_publish_mirror_is_excluded_under_a_second_spelling(repo_and_variant):
    """A mirror named in the registry under one spelling is still the mirror
    when discovery reaches it under another. Registering a publish target as
    a working repo is the failure the exclusion exists to prevent.
    """
    repo, variant = repo_and_variant
    if not _fs_is_case_insensitive(variant):
        pytest.skip("case-sensitive filesystem: the two spellings are two directories")

    from coordinator_core.ops.discover_working_repos import _to_posix_key

    emitted = list(_gate_and_dedup([variant], mirror_keys={_to_posix_key(repo)}))

    assert emitted == [], f"mirror reached under a second spelling was not excluded: {emitted}"


def test_dir_identity_collapses_spellings_and_separates_directories(tmp_path):
    """`dir_identity` asks the filesystem rather than folding case, so it is
    correct on both kinds of volume without consulting `os.name`."""
    a = str(tmp_path / "one")
    b = str(tmp_path / "two")
    os.makedirs(a)
    os.makedirs(b)

    assert dir_identity(a, fallback=a) == dir_identity(a, fallback=a)
    assert dir_identity(a, fallback=a) != dir_identity(b, fallback=b)

    variant = _case_variant(a)
    if _fs_is_case_insensitive(variant):
        assert dir_identity(variant, fallback=variant) == dir_identity(a, fallback=a)
        assert same_dir(variant, a)


def test_dir_identity_falls_back_for_a_path_that_cannot_be_stat(tmp_path):
    """An absent path degrades to the caller's own key rather than raising
    or inventing an identity that could merge unrelated directories."""
    missing = str(tmp_path / "not-here")

    assert dir_identity(missing, fallback="sentinel-key") == "sentinel-key"
