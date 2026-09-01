"""The commit leg compares `_filter_commit_pathspec` output against
`_dest_head_tree` keys. Those two forms MUST match, and nothing else pins it.

Both sides of the commit leg's absent-path classification depend on one
unstated contract: the strings `_filter_commit_pathspec` returns are keys into
the set `_dest_head_tree` returns. When that contract broke, it broke SILENTLY
and TOTALLY -- every tracked deletion classified as "untracked at dest HEAD"
and declined, on every round, while `git ls-tree HEAD` listed every one of
them. The engine reported a confident, specific, false reason.

Measured witness (coordinator-claude, 2026-08-31): eight `bin/` deletions
declined across three consecutive rounds. The first version of the fix resolved
each pathspec entry with `Path(p).resolve()` before comparing -- and
`_filter_commit_pathspec` returns DEST-RELATIVE paths, so `resolve()` resolved
them against the process CWD (claude-klabauter's own repo, never the dest). Every entry
then fell outside `repo_root`, `relative_to` raised, and the classification
defaulted to "not tracked". A total miss reads exactly like a working filter.

This file exists because that class of defect is invisible to any test that
stubs `subprocess.run`: the harness in `test_percolate_round.py` spies the git
boundary, so `_dest_head_tree` never answers from a real tree there and the two
forms are never actually compared. These tests use a REAL git repo and no spy.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_form", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _seed_repo(repo: Path) -> None:
    """A dest carrying a nested path under `bin/` -- the shape that failed.

    Nested, not top-level: a single-segment path cannot distinguish a POSIX
    key from a native-separator one, so a top-level fixture would pass against
    the very bug this file exists to catch.
    """
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    nested = repo / "bin" / "coordinator-auto-push.cmd"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("@echo off\n", encoding="utf-8")
    (repo / "keep.txt").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")


def test_head_tree_keys_are_dest_relative_posix(tmp_path):
    """`_dest_head_tree` emits dest-relative POSIX, on every host.

    Pinned directly rather than assumed, because every comparison against this
    set is only as correct as its key form -- and on Windows a native-separator
    key would still LOOK like a path in a debugger while matching nothing.
    """
    repo = tmp_path / "dest"
    _seed_repo(repo)

    head_tree = _mod._dest_head_tree(str(repo))

    assert "bin/coordinator-auto-push.cmd" in head_tree
    assert not any("\\" in key for key in head_tree)


def test_a_tracked_deletion_is_a_head_tree_member_by_its_pathspec_string(tmp_path):
    """The contract itself: the string the commit leg carries for a deleted
    path IS a key into `_dest_head_tree`, with no normalisation in between.

    This is the assertion whose absence let the `resolve()` defect ship. It
    deliberately compares the two producers' outputs against each other rather
    than either against a literal, so a future change to EITHER form fails here
    instead of silently declining every deletion at a public mirror.
    """
    repo = tmp_path / "dest"
    _seed_repo(repo)
    deleted = repo / "bin" / "coordinator-auto-push.cmd"
    deleted.unlink()

    head_tree = _mod._dest_head_tree(str(repo))
    seen = {str(deleted): ("REMOVE", "bin/coordinator-auto-push.cmd")}
    pathspec, _counts = _mod._filter_commit_pathspec(
        repo, str(repo), seen, repo_root=str(repo)
    )

    assert pathspec, "a tracked-but-deleted path must survive the benign-decline filter"
    for entry in pathspec:
        pp = Path(entry)
        assert not pp.is_absolute(), (
            f"{entry!r} is absolute; the commit leg's classification treats a "
            "pathspec entry as a dest-relative key"
        )
        assert pp.as_posix() in head_tree, (
            f"{entry!r} is not a key into _dest_head_tree {sorted(head_tree)!r} -- "
            "the two forms have diverged and every tracked deletion will decline"
        )


def test_tracked_deletion_is_routed_to_deletions_not_declined(tmp_path):
    """THE regression test: the classifier must route a tracked-but-deleted
    path to the deletion channel, not the decline channel.

    This is the assertion the first fix shipped without. It fails against the
    `Path(entry).resolve()` version -- the entry resolves against the process
    CWD, lands outside `repo_root`, and drops into `declined` -- and passes
    once the relative entry is used as the key it already is.
    """
    repo = tmp_path / "dest"
    _seed_repo(repo)
    (repo / "bin" / "coordinator-auto-push.cmd").unlink()

    head_tracked = _mod._dest_head_tree(str(repo))
    present, deletions, declined = _mod._partition_pathspec_for_commit(
        ["bin/coordinator-auto-push.cmd", "keep.txt"], str(repo), head_tracked
    )

    assert deletions == ["bin/coordinator-auto-push.cmd"]
    assert present == ["keep.txt"]
    assert declined == []


def test_absent_and_untracked_still_declines_with_an_accurate_reason(tmp_path):
    """The other arm must keep declining -- the fix widens what commits, and
    a path that is neither on disk nor at HEAD has no deletion to carry.

    The reason string is asserted for its CLAIM, not its wording: the previous
    message asserted the index had been consulted when no such check existed,
    and a confident false reason is what cost two round trips to disprove.
    """
    repo = tmp_path / "dest"
    _seed_repo(repo)

    head_tracked = _mod._dest_head_tree(str(repo))
    present, deletions, declined = _mod._partition_pathspec_for_commit(
        ["bin/never-existed.cmd"], str(repo), head_tracked
    )

    assert present == []
    assert deletions == []
    assert [d["path"] for d in declined] == ["bin/never-existed.cmd"]
    assert "untracked at dest HEAD" in declined[0]["reason"]


def test_absolute_entry_outside_the_dest_is_declined_not_crashed(tmp_path):
    """An absolute entry from some other tree must decline, never raise.

    `relative_to` raises on a path outside `repo_root`, and this classifier
    sits on the commit leg of a publish round -- a raise here is a crash mid-
    round, not a refusal.
    """
    repo = tmp_path / "dest"
    _seed_repo(repo)
    foreign = tmp_path / "elsewhere" / "bin" / "coordinator-auto-push.cmd"

    head_tracked = _mod._dest_head_tree(str(repo))
    present, deletions, declined = _mod._partition_pathspec_for_commit(
        [str(foreign)], str(repo), head_tracked
    )

    assert present == []
    assert deletions == []
    assert [d["path"] for d in declined] == [str(foreign)]


def test_resolving_a_relative_entry_is_what_broke_it(tmp_path):
    """Negative control, pinning the specific wrong move rather than a mood.

    `Path(entry).resolve()` on a dest-relative pathspec entry resolves against
    the PROCESS CWD, not the destination -- so the resolved path lies outside
    `repo_root` and `relative_to` raises. Asserting the raise keeps the reason
    the first fix failed legible to the next reader, who will otherwise see a
    plausible-looking `resolve()` and reintroduce it.
    """
    repo = tmp_path / "dest"
    _seed_repo(repo)
    entry = "bin/coordinator-auto-push.cmd"

    resolved = Path(entry).resolve()

    assert not str(resolved).startswith(str(repo)), (
        "fixture no longer reproduces the defect: the CWD-resolved entry must "
        "NOT land inside the destination for this control to mean anything"
    )
    try:
        resolved.relative_to(repo)
    except ValueError:
        pass
    else:  # pragma: no cover - only reachable if the defect stops reproducing
        raise AssertionError("expected relative_to to raise on the CWD-resolved entry")


def test_commit_subject_counts_deletions_it_actually_carries():
    """The subject's removed-count must include the deletion channel.

    A removal reaches the pathspec from the dest-HEAD comparison, not from
    this run's change lines, so a triple summarised from `real_changes` alone
    reports zero however many files the commit deletes. Measured on
    coordinator-claude 2026-09-01: a commit carrying eight file deletions and
    1,217 deleted lines announced "0 removed" in its own subject -- and a
    commit subject is the one report that outlives the round, so the OSS
    mirror's permanent history now carries that claim.
    """
    subject = _mod._build_commit_subject(
        "coordinator-claude",
        [("NEW", "bin/added.py")],
        ["bin/added.py", "bin/gone.cmd", "bin/also-gone.py"],
        deletion_paths=["bin/gone.cmd", "bin/also-gone.py"],
    )

    assert "1 added, 0 modified, 2 removed" in subject


def test_a_deletion_with_its_own_change_line_is_not_counted_twice():
    """Set difference, not addition: a path already tagged DELETE/REMOVE in
    the carried change lines must not also be counted through the deletion
    channel. Overcounting a removal is the same class of false claim as
    undercounting one, in the same permanent record.
    """
    subject = _mod._build_commit_subject(
        "coordinator-claude",
        [("REMOVE", "bin/gone.cmd")],
        ["bin/gone.cmd"],
        deletion_paths=["bin/gone.cmd"],
    )

    assert "0 added, 0 modified, 1 removed" in subject


def test_subject_is_unchanged_when_no_deletions_are_carried():
    """Negative control: the argument is optional and additive, so a round
    carrying no removals must produce exactly the subject it produced before
    the deletion channel existed.
    """
    without = _mod._build_commit_subject(
        "coordinator-claude", [("NEW", "bin/added.py")], ["bin/added.py"]
    )
    with_empty = _mod._build_commit_subject(
        "coordinator-claude", [("NEW", "bin/added.py")], ["bin/added.py"],
        deletion_paths=[],
    )

    assert without == with_empty
    assert "1 added, 0 modified, 0 removed" in without


def test_a_round_that_carries_removals_is_not_a_warning(capsys):
    """A pathspec larger than this run's change lines, with nothing dropped,
    must not count as a warning.

    `real_changes` is the worktree comparison; a removal reaches the pathspec
    from the dest-HEAD comparison instead, so ANY round that deletes something
    has a bigger pathspec by construction. That used to return a counted
    warning, so a healthy round announced its own success in the register of a
    warning -- reported by DoE on the first round that could carry deletions
    at all. The informational line must still print: uncounted, not silent.
    """
    warning = _mod._report_commit_residual(
        "coordinator-claude",
        [("NEW", "bin/added.py")],
        ["bin/added.py", "bin/gone.cmd", "bin/also-gone.py"],
        deletion_paths=["bin/gone.cmd", "bin/also-gone.py"],
    )

    assert warning is None
    err = capsys.readouterr().err
    assert "2 removal(s) this round carries" in err
    assert "2 carried into the pathspec beyond" in err
    assert "this is not a warning" in err


def test_surplus_separates_removals_from_unexplained_residue(capsys):
    """Removals and stranded residue used to render identically. They are not
    the same fact: one is the round working, the other is a path nothing in
    this run explains and is still worth an eye.
    """
    _mod._report_commit_residual(
        "coordinator-claude",
        [("NEW", "bin/added.py")],
        ["bin/added.py", "bin/gone.cmd", "bin/mystery.txt"],
        deletion_paths=["bin/gone.cmd"],
    )

    err = capsys.readouterr().err
    assert "1 removal(s) this round carries" in err
    assert "1 path(s) from an earlier round's" in err


def test_a_dropped_change_is_still_a_counted_warning():
    """The direction that matters must keep warning: an intended change that
    did NOT reach the pathspec is the defect this function exists for, and a
    round once printed a bare PASS while dropping 57 of them.
    """
    warning = _mod._report_commit_residual(
        "coordinator-claude",
        [("NEW", "bin/added.py"), ("UPDATE", "bin/dropped.py")],
        ["bin/added.py"],
    )

    assert warning is not None
    assert "NOT committed" in warning


def test_the_dropped_warning_does_not_call_the_pathspec_committed():
    """Vocabulary: the pathspec is what is NAMED to the commit leg, not what
    committed -- entries can still decline there. The refusal line called the
    same paths "declined" while this report called them "committed": one fact,
    two words, opposite meanings, which cost a round trip to reconcile. The
    surviving warning speaks about reported changes, never about paths having
    committed.
    """
    warning = _mod._report_commit_residual(
        "coordinator-claude",
        [("NEW", "a.py"), ("NEW", "b.py"), ("NEW", "c.py")],
        ["a.py", "b.py"],
    )

    assert warning is not None
    assert "path(s) committed" not in warning
    assert "were NOT committed" in warning
