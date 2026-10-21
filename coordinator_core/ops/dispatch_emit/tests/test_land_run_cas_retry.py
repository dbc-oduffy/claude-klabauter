"""dispatch.land_run retries a lost ref compare-and-swap on disjoint peer commits and refuses on overlap."""

from __future__ import annotations

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.git import ref_cas_retry
from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as base
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import (  # noqa: F401 -- fixture
    RUN,
    SCRIPT,
    _author,
    _declare,
    _git,
    _land,
    _record_dir,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _lose_first_cas(monkeypatch, root, peer_rel: str, times: int = 1) -> list:
    """Make the first ``times`` ref CAS calls fail after a peer commit lands on ``peer_rel``."""
    real = git_commit.cas_ref
    peers: list = []

    def fake(*args, **kwargs):
        if len(peers) < times:
            peers.append(None)
            _write(root, peer_rel, "peer\n" if len(peers) == 1 else f"peer{len(peers)}\n")
            _git(["add", peer_rel], root)
            _git(["commit", "-q", "-m", f"peer {len(peers)}"], root)
            return False
        return real(*args, **kwargs)

    monkeypatch.setattr(git_commit, "cas_ref", fake)
    return peers


def _setup(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    _record_dir(repo)


def test_a_lost_cas_to_a_disjoint_peer_commit_lands_on_the_peers_head(repo, monkeypatch):
    _setup(repo)
    _lose_first_cas(monkeypatch, repo, "peer/other.txt")

    out = _land(repo)

    assert out["committed"] is True, out
    head = _git(["rev-parse", "HEAD"], repo).strip()
    assert out["sha"] == head
    parent = _git(["rev-parse", "HEAD~1"], repo).strip()
    assert _git(["log", "-1", "--format=%s", parent], repo).strip() == "peer 1"
    assert _git(["show", "HEAD:peer/other.txt"], repo) == "peer\n"
    assert _git(["show", "HEAD:src/a.py"], repo) == "a = 1\n"


def test_a_peer_commit_on_a_landing_path_is_refused_not_retried(repo, monkeypatch):
    _setup(repo)
    _lose_first_cas(monkeypatch, repo, "src/a.py")
    before = _git(["rev-parse", "HEAD"], repo).strip()

    out = _land(repo)

    assert out["committed"] is False
    assert out["refused"] == "ref-moved-overlap"
    assert "compare-and-swap failed" in out["error"]
    assert _git(["rev-parse", "HEAD~1"], repo).strip() == before
    assert _git(["show", "HEAD:src/a.py"], repo) == "peer\n"
    assert _git(["log", "-1", "--format=%s"], repo).strip() == "peer 1"


def test_every_attempt_losing_returns_the_last_cas_refusal(repo, monkeypatch):
    _setup(repo)
    _lose_first_cas(monkeypatch, repo, "peer/other.txt", times=ref_cas_retry.REF_CAS_ATTEMPTS)

    out = _land(repo)

    assert out["committed"] is False
    assert out["refused"] == "commit"
    assert "compare-and-swap failed" in out["error"]
    assert _git(["log", "-1", "--format=%s"], repo).strip() == f"peer {ref_cas_retry.REF_CAS_ATTEMPTS}"


def test_a_non_cas_refusal_is_attempted_once(repo, monkeypatch):
    _setup(repo)
    calls: list = []
    real = git_commit.commit_paths

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(git_commit, "commit_paths", counting)
    (repo / "src/a.py").write_text("drifted\n", encoding="utf-8")

    out = _land(repo)

    assert out["committed"] is False
    assert len(calls) == 1


def test_the_run_token_refusal_names_the_tokenless_recovery(repo):
    _setup(repo)
    token = run_token.mint(repo, RUN, SCRIPT, session_id=base.SELF)
    out = _land(repo, run_token=token + "x")

    assert out["refused"] == "run-token"
    assert "task_output_path" in out["error"]
