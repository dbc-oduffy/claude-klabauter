"""merge3/classify: verdicts, and parity with `git merge-file -p` on conflict presence and HEAD-unchanged."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from coordinator_core.ops.kept_blob_classify import classify, merge3
from coordinator_core.ops.tests._kept_blob_fixture import commit_at, git_repo

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BASE = b"a\nb\nc\nd\ne\nf\ng\n"
T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _git_merge(tmp_path, base, kept, head):
    for n, d in (("head", head), ("base", base), ("kept", kept)):
        (tmp_path / n).write_bytes(d)
    r = subprocess.run(
        ["git", "merge-file", "-p", str(tmp_path / "head"), str(tmp_path / "base"), str(tmp_path / "kept")],
        capture_output=True, creationflags=_NOWIN)
    return r.stdout, r.returncode


CASES = {
    "kept-only": (BASE, b"a\nB\nc\nd\ne\nf\ng\n", BASE),
    "head-only": (BASE, BASE, b"a\nb\nc\nd\nE\nf\ng\n"),
    "both-disjoint": (BASE, b"a\nB\nc\nd\ne\nf\ng\n", b"a\nb\nc\nd\ne\nF\ng\n"),
    "identical": (BASE, b"a\nB\nc\nd\ne\nf\ng\n", b"a\nB\nc\nd\ne\nf\ng\n"),
    "conflict": (BASE, b"a\nX\nc\nd\ne\nf\ng\n", b"a\nY\nc\nd\ne\nf\ng\n"),
    "insert-both-same-spot": (BASE, b"a\nX\nb\nc\nd\ne\nf\ng\n", b"a\nY\nb\nc\nd\ne\nf\ng\n"),
    "delete-vs-edit": (BASE, b"a\nc\nd\ne\nf\ng\n", b"a\nB\nc\nd\ne\nf\ng\n"),
    "kept-in-head-plus-more": (BASE, b"a\nB\nc\nd\ne\nf\ng\n", b"a\nB\nc\nd\ne\nF\ng\nz\n"),
    "empty-base": (b"", b"x\ny\n", b"x\ny\nz\n"),
    "no-trailing-newline": (BASE, b"a\nb\nc\nd\ne\nf\nG", b"A\nb\nc\nd\ne\nf\ng\n"),
}


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("name", sorted(CASES))
def test_merge3_parity_with_git(tmp_path, name):
    base, kept, head = CASES[name]
    ours, n = merge3(base, kept, head)
    theirs, rc = _git_merge(tmp_path, base, kept, head)
    assert (n > 0) == (rc > 0), name
    if n == 0:
        assert ours == theirs
    assert (ours == head) == (theirs == head)


def test_conflict_emits_both_sides_between_markers():
    out, n = merge3(BASE, b"a\nX\nc\nd\ne\nf\ng\n", b"a\nY\nc\nd\ne\nf\ng\n")
    assert n == 1
    assert b"<<<<<<<" in out and b"=======" in out and b">>>>>>>" in out
    assert b"X\n" in out and b"Y\n" in out


def test_superseded_when_head_equals_kept():
    kept = b"a\nB\n"
    v = classify(b"a\nb\n", kept, kept, [("c1", T, b"a\nb\n"), ("c2", T, kept)])
    assert (v.verdict, v.commit) == ("SUPERSEDED", "c2")


def test_superseded_when_head_already_has_kept_hunks():
    kept = b"a\nB\nc\nd\ne\nf\ng\n"
    head = b"a\nB\nc\nd\ne\nF\ng\n"
    v = classify(BASE, kept, head, [("c1", T, BASE), ("c2", T, kept), ("c3", T, head)])
    assert (v.verdict, v.commit) == ("SUPERSEDED", "c2")


def test_superseded_without_landing_commit_has_null_commit():
    kept = b"a\nB\n"
    assert classify(b"a\nb\n", kept, kept, []).commit is None


def test_not_landed_when_clean_merge_changes_head():
    v = classify(BASE, b"a\nB\nc\nd\ne\nf\ng\n", b"a\nb\nc\nd\ne\nF\ng\n", [])
    assert v.verdict == "NOT-LANDED" and "missing from HEAD" in v.why


def test_not_landed_on_conflict():
    v = classify(BASE, b"a\nX\nc\nd\ne\nf\ng\n", b"a\nY\nc\nd\ne\nf\ng\n", [])
    assert v.verdict == "NOT-LANDED" and "conflicts (1)" in v.why


def test_carried_when_head_differs_from_kept_only_by_crlf():
    kept = b"a\nB\nc\n"
    v = classify(b"a\nb\nc\n", kept, b"a\r\nB\r\nc\r\n", [])
    assert (v.verdict, v.commit, v.why) == ("CARRIED", None, "EOL-only difference from HEAD")


def test_crlf_head_with_kept_hunks_present_is_superseded_not_conflict():
    base, kept = b"a\nb\nc\n", b"a\nB\nc\n"
    v = classify(base, kept, b"a\r\nB\r\nc\r\nd\r\n", [])
    assert v.verdict == "SUPERSEDED"


def test_crlf_does_not_mask_a_real_difference():
    v = classify(b"a\nb\n", b"a\nB\n", b"a\r\nX\r\n", [])
    assert v.verdict == "NOT-LANDED"


def test_not_landed_when_path_absent_at_head():
    v = classify(BASE, b"x\n", None, [])
    assert (v.verdict, v.why) == ("NOT-LANDED", "path absent at HEAD")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_classify_agrees_with_git_on_real_repo_history(tmp_path):
    repo = git_repo(tmp_path)
    commit_at(repo, "f.txt", BASE, "2026-01-01T00:00:00Z")
    kept = b"a\nB\nc\nd\ne\nf\ng\n"
    c2, _ = commit_at(repo, "f.txt", kept, "2026-01-02T00:00:00Z")
    head = b"a\nB\nc\nd\ne\nF\ng\n"
    commit_at(repo, "f.txt", head, "2026-01-03T00:00:00Z")
    v = classify(BASE, kept, head, [(c2, T, kept), ("later", T, head)])
    _, rc = _git_merge(tmp_path, BASE, kept, head)
    assert rc == 0 and v.verdict == "SUPERSEDED" and v.commit == c2
