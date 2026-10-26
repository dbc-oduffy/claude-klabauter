"""Tests for git_ancestry's batched helpers on tmp_path repos (one spawn each)."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import git_ancestry
from coordinator_core.git_ancestry import ancestors_of, resolve_commit_prefixes

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
}


def _git(repo, *args, input=None):
    import os

    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True,
        input=input.encode() if input is not None else None,
        env={**os.environ, **_ENV}, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return r.stdout.decode().strip()


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    real = git_ancestry.run_git

    def counting(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(git_ancestry, "run_git", counting)
    return calls


def test_ancestors_of_graph_shapes_one_spawn(repo, spawns):
    base = _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "side")
    _git(repo, "checkout", "-q", "main")
    main1 = _commit(repo, "main1")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "unmerged")
    un1 = _commit(repo, "un1")
    un2 = _commit(repo, "un2")
    _git(repo, "checkout", "-q", "main")

    ok, got = ancestors_of([head, base, main1, side, un1, un2], head, str(repo))
    assert ok
    assert got == frozenset({head, base, main1, side})
    assert len(spawns) == 1

    spawns.clear()
    ok, got = ancestors_of([base, un2], head, str(repo))
    assert ok and got == frozenset({base})
    assert len(spawns) == 1


def test_ancestors_of_bad_ref_is_unreadable_not_negative(repo):
    base = _commit(repo, "base")
    assert ancestors_of([base], "nosuchref", str(repo)) == (False, None)


def test_empty_inputs_spawn_nothing(repo, spawns):
    assert ancestors_of([], "HEAD", str(repo)) == (True, frozenset())
    assert resolve_commit_prefixes([], str(repo)) == ({}, None)
    assert spawns == []


def test_resolve_unique_prefixes_one_spawn(repo, spawns):
    a = _commit(repo, "a")
    b = _commit(repo, "b")
    got, refusal = resolve_commit_prefixes([a[:10], b[:12]], str(repo))
    assert refusal is None
    assert got == {a[:10]: a, b[:12]: b}
    assert len(spawns) == 1


def test_resolve_unknown_prefix_refuses(repo):
    _commit(repo, "a")
    got, refusal = resolve_commit_prefixes(["deadbeefdead"], str(repo))
    assert got == {}
    assert "deadbeefdead" in refusal and "no commit" in refusal


def test_resolve_prefix_shadowed_by_branch_refuses_ambiguous(repo):
    a = _commit(repo, "a")
    other = _commit(repo, "b")
    prefix = "beef"
    assert not a.startswith(prefix) and not other.startswith(prefix)
    _git(repo, "branch", prefix, a)
    got, refusal = resolve_commit_prefixes([prefix], str(repo))
    assert got == {}
    assert prefix in refusal and "ambiguous" in refusal


def test_resolve_ambiguous_prefix_refuses(repo):
    stream = []
    for i in range(1000):
        msg = f"c{i}"
        stream.append(
            f"commit refs/heads/main\nmark :{i + 1}\n"
            f"author t <t@example.invalid> {1600000000 + i} +0000\n"
            f"committer t <t@example.invalid> {1600000000 + i} +0000\n"
            f"data {len(msg)}\n{msg}\n"
            + (f"from :{i}\n" if i else "")
            + "\n"
        )
    _git(repo, "fast-import", "--quiet", input="".join(stream))
    shas = _git(repo, "rev-list", "main").split()
    seen = {}
    pair = None
    for s in shas:
        if s[:4] in seen:
            pair = s[:4]
            break
        seen[s[:4]] = s
    assert pair is not None, "no 4-hex collision among 1000 commits"
    got, refusal = resolve_commit_prefixes([pair], str(repo))
    assert got == {}
    assert pair in refusal and "ambiguous" in refusal
