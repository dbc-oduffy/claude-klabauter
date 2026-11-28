"""dispatch.terminal_commit re-attempts a lost ref compare-and-swap while no peer commit touched a landing path."""

from __future__ import annotations

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.git import ref_cas_retry
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    _REVIEWED,
    _SID,
    _git,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _call(repo, **extra) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": _SID,
        "ledger_verdict": {"items": [{"id": "R1", "status": "met", "wired": True}]}, **extra,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _peer_commit(repo, rel: str, text: str) -> str:
    _write(repo, rel, text)
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", f"peer {rel}", "--", rel)
    return _git(repo, "rev-parse", "HEAD").strip()


def _lose_first(monkeypatch, repo, *, rel: str, text: str, losses: int = 1, peers: list | None = None):
    """Make the first `losses` ref CAS calls lose to a fresh peer commit on `rel`."""
    real = git_commit.cas_ref
    state = {"calls": 0}

    def lossy(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] <= losses:
            sha = _peer_commit(repo, rel if losses == 1 else f"{rel}.{state['calls']}", text)
            if peers is not None:
                peers.append(sha)
            return False
        return real(*args, **kwargs)

    monkeypatch.setattr(git_commit, "cas_ref", lossy)
    return state


def test_a_lost_cas_on_a_disjoint_peer_commit_lands_on_the_peers_head(repo, monkeypatch):
    peers: list = []
    state = _lose_first(monkeypatch, repo, rel="peer.txt", text="peer\n", peers=peers)
    out = _call(repo)
    assert out["committed"] is True, out
    assert state["calls"] >= 2  # later stamp commits also pass through cas_ref
    assert _git(repo, "rev-parse", f"{out['sha']}^").strip() == peers[0]
    assert _git(repo, "show", f"{out['sha']}:peer.txt") == "peer\n"
    assert _git(repo, "show", f"{out['sha']}:a.py") == "a\n"


def test_a_peer_commit_on_a_landing_path_is_refused_not_retried(repo, monkeypatch):
    state = _lose_first(monkeypatch, repo, rel="a.py", text="peer a\n")
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    out = _call(repo)
    assert out["committed"] is False
    assert out["refused"] == ref_cas_retry.REFUSED_OVERLAP, out
    assert "compare-and-swap failed" in out["error"]
    assert state["calls"] == 1
    assert _git(repo, "show", "HEAD:a.py") == "peer a\n"
    assert _git(repo, "rev-parse", "HEAD~1").strip() == head_before


def test_after_the_bound_the_last_cas_error_is_returned(repo, monkeypatch):
    state = _lose_first(monkeypatch, repo, rel="peer", text="p\n", losses=ref_cas_retry.REF_CAS_ATTEMPTS)
    out = _call(repo)
    assert out["committed"] is False
    assert "compare-and-swap failed" in out["error"]
    assert state["calls"] == ref_cas_retry.REF_CAS_ATTEMPTS
    assert "a.py" not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()


def test_a_non_cas_refusal_is_attempted_once(repo, monkeypatch):
    calls = {"n": 0}

    def refusing(op, params, **kw):
        calls["n"] += 1
        return {"committed": False, "sha": None, "error": "blob drift on a.py", "refused": "blob-drift"}

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", refusing)
    out = _call(repo)
    assert out["committed"] is False and calls["n"] == 1
