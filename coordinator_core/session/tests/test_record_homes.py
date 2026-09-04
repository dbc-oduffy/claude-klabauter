"""Tests for `session.record_homes` -- path shapes and the read-side
pattern only, per its own negative-spec (owns paths, never creates a
directory, never does import-time I/O).
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.session import record_homes

REPO_ROOT = os.path.join("X:", os.sep, "fake-repo")

_REPO_ROOT_ON_DISK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
_ALLOWLIST_PATH = os.path.join(
    _REPO_ROOT_ON_DISK, "docs", "reference", "state-corpus-allowlist.txt"
)

#: The machinery relocation set `machinery_paths.py` already owns and the
#: allowlist itself deliberately excludes -- same set
#: `test_machinery_paths.test_every_tracked_state_first_segment_is_on_the_allowlist`
#: carries. `record_homes.HOMES` must never declare one of these: it would
#: be a second owner for a home `machinery_paths.py` already has.
_MACHINERY_RELOCATION_SET = {
    "subagent-share",
    "review-trail",
    "ceremony",
    "dispatch-briefs",
    "plan-sidecars",
    "memo-outbox",
    "ledgers",
    "kill-ledger.md",
}


def _read_allowlist() -> set[str]:
    with open(_ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def test_home_dir_joins_repo_root_state_and_segment():
    got = record_homes.home_dir(REPO_ROOT, "handoffs")
    assert got == os.path.join(REPO_ROOT, "state", "handoffs")


def test_record_path_joins_basename_onto_home_dir():
    got = record_homes.record_path(REPO_ROOT, "lessons", "foo.md")
    assert got == os.path.join(
        record_homes.home_dir(REPO_ROOT, "lessons"), "foo.md"
    )


def test_home_dir_raises_key_error_for_undeclared_kind():
    with pytest.raises(KeyError):
        record_homes.home_dir(REPO_ROOT, "not-a-real-kind")


def test_module_never_creates_a_directory(tmp_path):
    repo_root = str(tmp_path)
    record_homes.home_dir(repo_root, "handoffs")
    record_homes.record_path(repo_root, "sizings", "foo.yaml")
    assert not os.path.exists(os.path.join(repo_root, "state"))


@pytest.mark.parametrize(
    "path",
    [
        "state/handoffs/foo.md",
        "state\\handoffs\\foo.md",
        ".coordinator-local/handoffs/foo.md",
        ".coordinator-local\\handoffs\\foo.md",
        "X:\\repo\\state\\handoffs\\foo.md",  # abs-path-ok: test fixture, not a real host path
        "/repo/state/handoffs/foo.md",
    ],
)
def test_home_pattern_matches_both_roots_and_both_separators(path):
    pattern = record_homes.home_pattern("handoffs")
    assert pattern.search(path), f"expected a match against {path!r}"


@pytest.mark.parametrize(
    "path",
    [
        "state/handoffsomething/foo.md",
        "state/other-handoffs/foo.md",
        "state/lessons/foo.md",
        "handoffs/foo.md",
    ],
)
def test_home_pattern_does_not_match_a_different_or_prefixed_segment(path):
    pattern = record_homes.home_pattern("handoffs")
    assert not pattern.search(path), f"unexpected match against {path!r}"


def test_home_pattern_raises_key_error_for_undeclared_kind():
    with pytest.raises(KeyError):
        record_homes.home_pattern("not-a-real-kind")


def test_every_declared_home_appears_on_the_allowlist():
    """The write side of the agreement: every `HOMES` segment must be a
    line the allowlist already names. A segment declared here that the
    allowlist does not name is either a typo in `record_homes.py` or a new
    kind that has not actually landed on disk yet -- either way, this
    module must not silently win over the allowlist.
    """
    allowlist = _read_allowlist()
    missing = sorted(set(record_homes.HOMES) - allowlist)
    assert not missing, (
        f"record_homes.HOMES declares segment(s) {missing} the allowlist "
        f"({_ALLOWLIST_PATH}) does not name -- record_homes.py has moved "
        f"ahead of the corpus it describes"
    )


def test_no_declared_home_is_a_machinery_relocation_bucket():
    """The converse of the exclusion this module's own docstring states:
    `HOMES` must never declare a segment `machinery_paths.py` already
    owns. A segment appearing in both would be two owners for one home,
    exactly the ambiguity this plan exists to remove.
    """
    overlap = sorted(set(record_homes.HOMES) & _MACHINERY_RELOCATION_SET)
    assert not overlap, (
        f"record_homes.HOMES declares machinery-relocation segment(s) "
        f"{overlap} -- machinery_paths.py already owns these"
    )


def test_every_allowlist_directory_not_in_the_relocation_set_is_declared():
    """The read side of the agreement, and the one that catches the
    silent-reader failure this plan exists to close: every allowlist entry
    that is a real directory under `state/` on THIS checkout, and is not
    part of the machinery relocation set, must have a `record_homes.HOMES`
    entry. An allowlist directory with no declared home is exactly the gap
    `artifact_owner._SUBAGENT_SHARE_DIR_RE` fell into at `2acd5ca032`,
    caught here instead of by a peer hitting a refusal.
    """
    declared = set(record_homes.HOMES)
    allowlist = _read_allowlist()
    undeclared = sorted(
        seg
        for seg in allowlist
        if seg not in _MACHINERY_RELOCATION_SET
        and seg not in declared
        and os.path.isdir(os.path.join(_REPO_ROOT_ON_DISK, "state", seg))
    )
    assert not undeclared, (
        f"allowlist director{'y' if len(undeclared) == 1 else 'ies'} "
        f"{undeclared} under state/ has no record_homes.py entry"
    )
