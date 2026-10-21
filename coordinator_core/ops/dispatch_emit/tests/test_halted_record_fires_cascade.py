"""A halted run fires deliverable.cascade_terminal for its recorded baton only when already terminal.

Run (from repo root):
    python3 -m pytest -p no:xdist coordinator_core/ops/dispatch_emit/tests/test_halted_record_fires_cascade.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import coordinator_core.warm.entry_seam as entry_seam
from coordinator_core.ops.cascade_backstop_sweep import _handler as sweep_handler
from coordinator_core.ops.dispatch_emit import halted_record_commit, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    write_run_baton,
)
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _git, _init_repo, _seed_handoff

pytestmark = [pytest.mark.spawns_process]

RUN = "ask-20261015T000000-aaaaaa"
DID = "dlv-halt-cascade-000000"
NAME = "2026-10-21-run-baton.md"
BATON = f"state/handoffs/{NAME}"
SESSION = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _caller_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", SESSION)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    return root


def _seed_baton(root: Path, *, state: str, archived: bool) -> Path:
    path = _seed_handoff(root, NAME, deployment_state=state, deliverable_id=DID)
    if state == "shipped":
        text = path.read_text(encoding="utf-8").replace(
            "deployment_state: shipped\n", "deployment_state: shipped\nshipped_in: abc1234\n", 1
        )
        path.write_text(text, encoding="utf-8")
    if archived:
        dest = root / "archive" / "handoffs" / NAME[:7] / NAME
        dest.parent.mkdir(parents=True)
        path.rename(dest)
        path = dest
    write_run_baton(root, RUN, BATON)
    return path


def _halt(root: Path) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: {"halted": "refusal"}}
    return terminal_commit._handler(params, repo_root=root / ".git")


def _sweep(root: Path) -> dict:
    return asyncio.run(sweep_handler({"deliverable_id": DID}, repo_root=root / ".git"))


def _forbid_dispatch(monkeypatch):
    real = entry_seam.reentrant_dispatch
    calls: list = []

    def guard(op, *a, **k):
        calls.append(op)
        if op == "deliverable.cascade_terminal":
            raise AssertionError("the cascade op must not be called")
        return real(op, *a, **k)

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", guard)
    return calls


def test_open_baton_is_left_open_and_the_op_is_not_called(repo, monkeypatch):
    baton = _seed_baton(repo, state="ready_to_fire", archived=False)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _forbid_dispatch(monkeypatch)

    out = _halt(repo)

    assert out["committed"] is True, out
    text = baton.read_text(encoding="utf-8")
    assert "status: open\n" in text and "deployment_state: ready_to_fire\n" in text
    assert f"status_reason: 'run {RUN} halted: refusal'\n" in text
    assert out["run_baton_cascade"]["fired"] == []
    assert out["run_baton_cascade"]["skipped"] == [{"baton": BATON, "reason": "not-terminal"}]


def test_run_recording_no_baton_has_no_cascade_key(repo, monkeypatch):
    _forbid_dispatch(monkeypatch)
    out = _halt(repo)
    assert out["committed"] is True, out
    assert "run_baton_cascade" not in out


def test_shipped_archived_baton_advances_peer_and_backstop_is_clean(repo):
    _seed_baton(repo, state="shipped", archived=True)
    _seed_handoff(repo, "2026-10-21-peer.md", deliverable_id=DID)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    assert _sweep(repo)["divergences"]

    out = _halt(repo)

    assert out["committed"] is True, out
    fired = out["run_baton_cascade"]["fired"]
    assert len(fired) == 1 and fired[0]["advanced"] == ["state/handoffs/2026-10-21-peer.md"]
    assert "commit_error" not in fired[0]
    assert _sweep(repo)["divergences"] == []


def test_errored_commit_reply_makes_no_cascade_call(repo, monkeypatch):
    _seed_baton(repo, state="shipped", archived=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    real = entry_seam.reentrant_dispatch
    ops: list = []

    def fake(op, params, **k):
        ops.append(op)
        if op == "ceremony.commit_v2":
            return {"error": "boom"}
        return real(op, params, **k)

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", fake)
    out = _halt(repo)

    assert out.get("error")
    assert "deliverable.cascade_terminal" not in ops
    assert "run_baton_cascade" not in out


def test_no_headroom_records_op_unavailable_and_calls_nothing(repo, monkeypatch):
    _seed_baton(repo, state="shipped", archived=True)
    calls = _forbid_dispatch(monkeypatch)
    monkeypatch.setattr(halted_record_commit.dispatch_abandon, "remaining", lambda: 0.0)

    got = halted_record_commit._baton_cascade(repo, repo / ".git", RUN)

    assert got == {"run_baton_cascade": {"fired": [], "skipped": [{"baton": BATON, "reason": "op-unavailable"}]}}
    assert calls == []
