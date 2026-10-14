"""Tests for coordinator_core.git.reconcile_manifest."""

from __future__ import annotations

import pytest

from coordinator_core.git import reconcile_manifest as rm

SHA = "a" * 40


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _open_merge(repo, sha=SHA):
    (repo / ".git" / "MERGE_HEAD").write_text(sha + "\n", encoding="utf-8")


def test_round_trip_sorts_and_normalises(repo):
    _open_merge(repo)
    rm.write(repo, merge_head=SHA, branch="b", paths=["z\\y.py", "a.py", "a.py"])
    m = rm.read(repo)
    assert m == rm.Manifest(merge_head=SHA, branch="b", paths=("a.py", "z/y.py"))
    assert rm.manifest_path(repo).parent == repo / ".git" / "coordinator-reconcile"


def test_absent_returns_none(repo):
    _open_merge(repo)
    assert rm.read(repo) is None


def test_malformed_returns_none(repo):
    _open_merge(repo)
    p = rm.manifest_path(repo)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert rm.read(repo) is None


def test_wrong_schema_returns_none(repo):
    _open_merge(repo)
    rm.write(repo, merge_head=SHA, branch="b", paths=["a"])
    p = rm.manifest_path(repo)
    p.write_text(p.read_text().replace('"schema": 1', '"schema": 2'), encoding="utf-8")
    assert rm.read(repo) is None


def test_bad_field_types_return_none(repo):
    _open_merge(repo)
    p = rm.manifest_path(repo)
    p.parent.mkdir(parents=True)
    p.write_text(
        f'{{"schema": 1, "merge_head": "{SHA}", "branch": "b", "paths": "a"}}',
        encoding="utf-8",
    )
    assert rm.read(repo) is None


def test_stale_merge_head_returns_none(repo):
    _open_merge(repo, "b" * 40)
    rm.write(repo, merge_head=SHA, branch="b", paths=["a"])
    assert rm.read(repo) is None


def test_missing_merge_head_returns_none(repo):
    _open_merge(repo)
    rm.write(repo, merge_head=SHA, branch="b", paths=["a"])
    (repo / ".git" / "MERGE_HEAD").unlink()
    assert rm.read(repo) is None


def test_remove_is_idempotent(repo):
    _open_merge(repo)
    rm.write(repo, merge_head=SHA, branch="b", paths=["a"])
    rm.remove(repo)
    rm.remove(repo)
    assert rm.read(repo) is None


def test_gitdir_pointer_file_is_honoured(tmp_path):
    real = tmp_path / "real-gitdir"
    real.mkdir()
    (real / "MERGE_HEAD").write_text(SHA, encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
    rm.write(wt, merge_head=SHA, branch="b", paths=["a"])
    assert (real / "coordinator-reconcile" / "conflict-manifest.json").is_file()
    assert rm.read(wt) is not None


@pytest.mark.parametrize(
    "text",
    [
        "<<<<<<< HEAD\nx\n",
        "a\n=======\nb\n",
        "a\n>>>>>>> branch\n",
        "a\r\n=======\r\nb\r\n",
    ],
)
def test_marker_matches(text):
    assert rm.has_marker(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain\n",
        "x = '<<<<<<< HEAD'\n",
        "    =======\n",
        "======= trailing\n",
        "==========\n",
        "<<<<<<<HEAD\n",
        "<<<<<<<\n",
        "# heading\n=======x\n",
    ],
)
def test_marker_ignores_non_anchored(text):
    assert not rm.has_marker(text)


def test_pinned_strings():
    assert rm.CONCLUDE_CLI == "reconcile-merge-conclude"
    assert rm.OUTCOME_CONFLICT_OPEN == "RECONCILE-CONFLICT-OPEN"
    assert rm.OUTCOME_CONCLUDED == "RECONCILED-MERGE-CONCLUDED"
    assert {
        rm.REFUSAL_NO_MERGE,
        rm.REFUSAL_MANIFEST_STALE,
        rm.REFUSAL_MARKERS,
        rm.REFUSAL_UNMERGED,
        rm.REFUSAL_FOREIGN_STAGED,
    } == {
        "RECONCILE-CONCLUDE-NO-MERGE",
        "RECONCILE-CONCLUDE-MANIFEST-STALE",
        "RECONCILE-CONCLUDE-MARKERS",
        "RECONCILE-CONCLUDE-UNMERGED",
        "RECONCILE-CONCLUDE-FOREIGN-STAGED",
    }
