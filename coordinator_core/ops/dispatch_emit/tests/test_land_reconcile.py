"""land_reconcile.reconcile_landing: git-derived committed / not_committed / partial / unknown verdict."""

from __future__ import annotations

import hashlib
import os
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.land_reconcile import reconcile_landing

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ENV = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
_EPOCH = [1_700_000_000]


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", *_ENV, *args], cwd=repo, check=True, capture_output=True, text=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo, files, message="c"):
    """Write ``files`` (path -> text | None to delete), commit with a strictly later date."""
    for name, text in files.items():
        target = repo / name
        if text is None:
            _git(repo, "rm", "-q", name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _git(repo, "add", name)
    _EPOCH[0] += 10
    stamp = f"{_EPOCH[0]} +0000"
    env = {**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp}
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _blob(repo, name):
    return _git(repo, "rev-parse", f"HEAD:{name}")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    base = _commit(tmp_path, {"keep.txt": "keep", "old.txt": "old"}, "base")
    return tmp_path, base


def test_committed_reports_landing_sha(repo):
    root, base = repo
    landing = _commit(root, {"a.txt": "a", "dir/b.txt": "b"})
    expected = {"a.txt": {"blob": _blob(root, "a.txt")}, "dir/b.txt": {"blob": _blob(root, "dir/b.txt")}}
    assert reconcile_landing(root, base, expected) == {"verdict": "committed", "sha": landing}


def test_peer_commit_on_top_still_reports_landing_sha(repo):
    root, base = repo
    landing = _commit(root, {"a.txt": "a"})
    expected = {"a.txt": {"blob": _blob(root, "a.txt")}}
    _commit(root, {"peer.txt": "p"})
    _commit(root, {"a.txt": "a changed later"})
    assert reconcile_landing(root, base, expected) == {"verdict": "committed", "sha": landing}


def test_oldest_carrying_commit_wins_over_later_cumulative_commits(repo):
    root, base = repo
    landing = _commit(root, {"a.txt": "a"})
    expected = {"a.txt": {"blob": _blob(root, "a.txt")}}
    _commit(root, {"x.txt": "x"})
    _commit(root, {"y.txt": "y"})
    assert reconcile_landing(root, base, expected)["sha"] == landing


def test_sha256_expectation_matches_by_content(repo):
    root, base = repo
    landing = _commit(root, {"a.txt": "alpha"})
    digest = hashlib.sha256(b"alpha").hexdigest()
    assert reconcile_landing(root, base, {"a.txt": {"sha256": digest}}) == {
        "verdict": "committed", "sha": landing,
    }
    wrong = hashlib.sha256(b"beta").hexdigest()
    assert reconcile_landing(root, base, {"a.txt": {"sha256": wrong}})["verdict"] == "not_committed"


def test_deletion_expected_absent(repo):
    root, base = repo
    landing = _commit(root, {"old.txt": None})
    assert reconcile_landing(root, base, {"old.txt": None}) == {"verdict": "committed", "sha": landing}


def test_not_committed_when_no_commit_carries_the_set(repo):
    root, base = repo
    _commit(root, {"peer.txt": "p"})
    expected = {"a.txt": {"blob": "1" * 40}}
    assert reconcile_landing(root, base, expected) == {"verdict": "not_committed", "sha": None}


def test_not_committed_when_head_has_not_moved(repo):
    root, base = repo
    assert reconcile_landing(root, base, {"a.txt": {"blob": "1" * 40}}) == {
        "verdict": "not_committed", "sha": None,
    }


def test_partial_names_commit_and_missing_paths(repo):
    root, base = repo
    part = _commit(root, {"a.txt": "a"})
    expected = {"a.txt": {"blob": _blob(root, "a.txt")}, "b.txt": {"blob": "2" * 40}, "c.txt": None}
    result = reconcile_landing(root, base, expected)
    assert result["verdict"] == "partial"
    assert result["sha"] == part
    assert result["matched"] == ["a.txt"]  # c.txt is absent at base already, so it is dropped
    assert result["missing"] == ["b.txt"]


def test_path_unchanged_from_base_never_fakes_a_partial(repo):
    root, base = repo
    keep = {"blob": _blob(root, "keep.txt")}
    _commit(root, {"peer.txt": "p"})
    assert reconcile_landing(root, base, {"keep.txt": keep, "a.txt": {"blob": "1" * 40}}) == {
        "verdict": "not_committed", "sha": None,
    }


def test_path_unchanged_from_base_does_not_block_a_real_landing(repo):
    root, base = repo
    keep = {"blob": _blob(root, "keep.txt")}
    landing = _commit(root, {"a.txt": "a"})
    expected = {"keep.txt": keep, "a.txt": {"blob": _blob(root, "a.txt")}}
    assert reconcile_landing(root, base, expected) == {"verdict": "committed", "sha": landing}


def test_unknown_without_evidence(repo):
    root, base = repo
    expected = {"a.txt": {"blob": "1" * 40}}
    assert reconcile_landing(root, None, expected) == {"verdict": "unknown"}
    assert reconcile_landing(root, "0" * 40, expected) == {"verdict": "unknown"}
    assert reconcile_landing(root, base, {}) == {"verdict": "unknown"}
    assert reconcile_landing(root, base, {"keep.txt": {"blob": _blob(root, "keep.txt")}}) == {
        "verdict": "unknown",
    }


def test_unknown_without_a_repo(tmp_path):
    assert reconcile_landing(tmp_path, "a" * 40, {"a.txt": None}) == {"verdict": "unknown"}


def test_commits_at_or_below_base_never_count(repo):
    root, base = repo
    expected = {"keep.txt": {"blob": _blob(root, "keep.txt")}, "old.txt": {"blob": _blob(root, "old.txt")}}
    _commit(root, {"z.txt": "z"})
    assert reconcile_landing(root, base, expected)["verdict"] == "unknown"


def test_spawns_nothing(repo, monkeypatch):
    root, base = repo
    landing = _commit(root, {"a.txt": "a"})
    expected = {"a.txt": {"blob": _blob(root, "a.txt")}}
    spawns = []

    def boom(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert reconcile_landing(root, base, expected) == {"verdict": "committed", "sha": landing}
    assert reconcile_landing(root, base, {"q.txt": {"blob": "1" * 40}})["verdict"] == "not_committed"
    assert spawns == []
