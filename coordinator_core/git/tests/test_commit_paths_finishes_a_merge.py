"""`commit_paths(finish_merge=True)` lands a two-parent commit over a real `git merge` fixture.

Not covered: a modify/delete conflict resolved by deletion, and the real signed-commit parent bytes."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused, commit_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_MERGE_PATHS = ["auto.txt", "shared.txt"]


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _w(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def merging(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "base")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    for n in ("shared.txt", "auto.txt", "kept.txt", "bystander.txt"):
        _w(r, n, "base\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    _git(r, "checkout", "-q", "-b", "other")
    _w(r, "shared.txt", "other\n")
    _w(r, "auto.txt", "other auto\n")
    _git(r, "commit", "-qam", "other side")
    _git(r, "checkout", "-q", "base")
    _w(r, "shared.txt", "mine\n")
    _w(r, "kept.txt", "mine kept\n")
    _git(r, "commit", "-qam", "my side")
    _git(r, "merge", "other", check=False)
    assert (r / ".git" / "MERGE_HEAD").exists()
    return r


def _heads(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_finish_merge_lands_two_parents_and_clears_state(merging):
    old_head = _heads(merging)
    merge_head = _git(merging, "rev-parse", "MERGE_HEAD").stdout.strip()
    # shared.txt is resolved in the worktree but never `git add`ed.
    _w(merging, "shared.txt", "resolved\n")

    outcome = commit_paths(merging, _MERGE_PATHS, "merge other", finish_merge=True)

    assert outcome.merge_parent == merge_head
    raw = _git(merging, "cat-file", "-p", "HEAD").stdout
    parents = [ln.split()[1] for ln in raw.splitlines() if ln.startswith("parent ")]
    assert parents == [old_head, merge_head]
    assert _git(merging, "show", "HEAD:shared.txt").stdout == "resolved\n"
    assert _git(merging, "show", "HEAD:auto.txt").stdout == "other auto\n"
    for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE"):
        assert not (merging / ".git" / name).exists(), name
    assert _git(merging, "ls-files", "-u").stdout == ""
    assert _git(merging, "status", "--porcelain").stdout.strip() == ""


def test_finish_merge_unsigned_route_spawns_nothing(merging, monkeypatch):
    _w(merging, "shared.txt", "resolved\n")
    calls = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run_spy(*a, **k):
        calls.append(a)
        return real_run(*a, **k)

    class PopenSpy(real_popen):
        def __init__(self, *a, **k):
            calls.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "run", run_spy)
    monkeypatch.setattr(subprocess, "Popen", PopenSpy)
    commit_paths(merging, _MERGE_PATHS, "merge other", finish_merge=True)
    assert calls == []


def test_default_call_still_refuses_mid_merge(merging):
    _w(merging, "shared.txt", "resolved\n")
    with pytest.raises(CommitRefused, match="MERGE_HEAD"):
        commit_paths(merging, _MERGE_PATHS, "no flag")


def _assert_refused_untouched(repo, head, **kw):
    paths = kw.pop("paths", _MERGE_PATHS)
    with pytest.raises(CommitRefused):
        commit_paths(repo, paths, "merge other", finish_merge=True)
    assert _heads(repo) == head
    assert (repo / ".git" / "MERGE_HEAD").exists()


def test_marker_left_refuses(merging):
    head = _heads(merging)
    # git's own conflict-marked worktree file is still in place.
    _assert_refused_untouched(merging, head)


def test_unmerged_path_omitted_refuses(merging):
    head = _heads(merging)
    _w(merging, "shared.txt", "resolved\n")
    _assert_refused_untouched(merging, head, paths=["auto.txt"])


def test_path_outside_merge_refuses(merging):
    head = _heads(merging)
    _w(merging, "shared.txt", "resolved\n")
    _w(merging, "bystander.txt", "extra\n")
    _assert_refused_untouched(merging, head, paths=_MERGE_PATHS + ["bystander.txt"])


def test_octopus_merge_head_refuses(merging):
    head = _heads(merging)
    _w(merging, "shared.txt", "resolved\n")
    mh = (merging / ".git" / "MERGE_HEAD").read_text()
    (merging / ".git" / "MERGE_HEAD").write_text(mh + head + "\n")
    _assert_refused_untouched(merging, head)


def test_finish_merge_without_merge_head_is_ordinary_commit(merging):
    _w(merging, "shared.txt", "resolved\n")
    _git(merging, "add", "-A")
    _git(merging, "commit", "-q", "--no-edit")
    _w(merging, "bystander.txt", "two\n")

    outcome = commit_paths(merging, ["bystander.txt"], "after the merge", finish_merge=True)

    assert outcome.merge_parent is None
    raw = _git(merging, "cat-file", "-p", "HEAD").stdout
    assert sum(1 for ln in raw.splitlines() if ln.startswith("parent ")) == 1


def test_splice_failure_after_cas_leaves_merge_state_cleared(merging, monkeypatch):
    _w(merging, "shared.txt", "resolved\n")
    old_head = _heads(merging)

    def boom(self):
        raise gcommit.index_write.IndexWriteError("forced")

    monkeypatch.setattr(gcommit.index_write.PreparedSplice, "_publish", boom)
    monkeypatch.setattr(gcommit, "_reset_index_paths", lambda *a, **k: False)
    with pytest.raises(gcommit.index_write.IndexStaleAfterCommit):
        commit_paths(merging, _MERGE_PATHS, "merge other", finish_merge=True)

    assert _heads(merging) != old_head
    assert not (merging / ".git" / "MERGE_HEAD").exists()
    assert not (merging / ".git" / "index.lock").exists()


def test_busy_index_lock_refuses_merge_commit_and_leaves_state_as_found(merging, monkeypatch):
    _w(merging, "shared.txt", "resolved\n")
    old_head = _heads(merging)
    cached_before = _git(merging, "diff", "--cached").stdout
    unmerged_before = _git(merging, "ls-files", "-u").stdout
    lock = merging / ".git" / "index.lock"
    lock.write_bytes(b"")
    monkeypatch.setattr(gcommit, "_SPLICE_LOCK_BACKOFF_SECS", ())

    with pytest.raises(CommitRefused, match="nothing committed"):
        commit_paths(merging, _MERGE_PATHS, "merge other", finish_merge=True)

    assert lock.exists()
    assert _heads(merging) == old_head
    assert (merging / ".git" / "MERGE_HEAD").exists()
    assert _git(merging, "diff", "--cached").stdout == cached_before
    assert _git(merging, "ls-files", "-u").stdout == unmerged_before


def test_signed_commit_tree_emits_extra_parent_lines(merging, monkeypatch):
    seen = {}

    def fake_in_process(repo, tree, parent, name, email, when, message, *, extra_parents=()):
        seen["extra"] = tuple(extra_parents)
        return "a" * 40

    monkeypatch.setattr(gcommit, "_sign_commit_in_process", fake_in_process)
    monkeypatch.setattr(gcommit, "_signing_budget", lambda: None)
    sha, warn = gcommit._sign_commit_tree(
        merging, "b" * 40, "c" * 40, "n", "e@x", "0 +0000", "m", extra_parents=("d" * 40,)
    )
    assert (sha, warn) == ("a" * 40, None)
    assert seen["extra"] == ("d" * 40,)
