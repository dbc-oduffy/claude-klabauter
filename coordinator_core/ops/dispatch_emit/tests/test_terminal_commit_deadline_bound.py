"""dispatch.terminal_commit binds a land_deadline scope and refuses a landing whose paths a hand committed mid-call."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_deadline, terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    _REVIEWED,
    _SID,
    _git,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

LEG = "peer_hunk_gate.co_declared_peer_holds"


def _call(repo) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": _SID,
        "ledger_verdict": {"items": [{"id": "R1", "status": "met", "wired": True}]},
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _head(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _hand_commit_midcall(monkeypatch, repo, rel: str, text: str) -> None:
    """A peer/hand commit on `rel` lands after the call captured its entry HEAD, before its commit."""
    real = terminal_commit._regenerate_landing_allowlist
    state = {"done": False}

    def racing(*args, **kwargs):
        if not state["done"]:
            state["done"] = True
            _write(repo, rel, text)
            _git(repo, "add", "--", rel)
            _git(repo, "commit", "-q", "-m", f"hand {rel}", "--", rel)
        return real(*args, **kwargs)

    monkeypatch.setattr(terminal_commit, "_regenerate_landing_allowlist", racing)


def test_a_landing_path_committed_by_hand_after_entry_is_refused_not_recommitted(repo, monkeypatch):
    _hand_commit_midcall(monkeypatch, repo, "a.py", "hand = 1\n")
    out = _call(repo)
    assert out["committed"] is False
    assert out["refused"] == "head-moved", out
    assert "a.py" in out["moved_paths"]
    assert _git(repo, "show", "HEAD:a.py") == "hand = 1\n"
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""


def test_a_hand_commit_on_a_disjoint_path_still_lands(repo, monkeypatch):
    _hand_commit_midcall(monkeypatch, repo, "peer.txt", "peer\n")
    out = _call(repo)
    assert out["committed"] is True, out
    assert _git(repo, "show", f"{out['sha']}:a.py") == "a\n"


def test_an_overrunning_chain_answers_a_named_halt_with_nothing_landed(repo, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(land_deadline, "_now", lambda: clock[0])

    def overrun(*args, **kwargs):
        clock[0] += land_deadline.LAND_BUDGET_SECS + land_deadline.RESERVE_SECS + 1.0
        land_deadline.check(LEG)
        raise AssertionError("check must raise past the deadline")

    monkeypatch.setattr(terminal_commit, "_landing_integrity_gate", overrun)
    head = _head(repo)
    status_before = _git(repo, "status", "--porcelain", "--untracked-files=all")
    out = _call(repo)
    assert out["committed"] is False
    assert out["refused"] == land_deadline.REFUSED, out
    assert out["leg"] == LEG
    assert out["leg_costs"], "the leg ledger rides the reply"
    assert _head(repo) == head
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == status_before


def test_the_unmoved_head_costs_no_git_spawn(repo, monkeypatch):
    spawned: list = []
    monkeypatch.setattr(terminal_commit, "run_git", lambda *a, **k: spawned.append(a))
    entry = terminal_commit._entry_head(repo / ".git")
    assert entry == _head(repo)
    assert terminal_commit._paths_committed_since(repo, entry, ["a.py"]) == []
    assert spawned == []
