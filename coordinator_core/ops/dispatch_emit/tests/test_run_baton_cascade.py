"""
coordinator_core.ops.dispatch_emit.tests.test_run_baton_cascade -- pins run_baton_cascade.

Run (from repo root):
    python3 -m pytest -p no:xdist coordinator_core/ops/dispatch_emit/tests/test_run_baton_cascade.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import coordinator_core.warm.entry_seam as entry_seam
from coordinator_core.ops.cascade_backstop_sweep import _handler as sweep_handler
from coordinator_core.ops.dispatch_emit import run_baton_cascade as mod
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import (
    _git,
    _init_repo,
    _seed_handoff,
)

pytestmark = [pytest.mark.spawns_process]

_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_DID = "dlv-run-baton-000000"
_NAME = "2026-10-21-run-baton.md"


@pytest.fixture(autouse=True)
def _caller_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"{message}\n\nSession-Id: {_SESSION_ID}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _baton(repo: Path, *, state: str, shipped_in: str | None, archived: bool,
           did: str = _DID, name: str = _NAME) -> str:
    path = _seed_handoff(repo, name, deployment_state=state, deliverable_id=did)
    if shipped_in:
        text = path.read_text(encoding="utf-8").replace(
            f"deployment_state: {state}\n", f"deployment_state: {state}\nshipped_in: {shipped_in}\n", 1
        )
        path.write_text(text, encoding="utf-8")
    if not archived:
        return f"state/handoffs/{name}"
    dest = repo / "archive" / "handoffs" / name[:7] / name
    dest.parent.mkdir(parents=True)
    path.rename(dest)
    return f"state/handoffs/{name}"


def _sweep(repo: Path, did: str) -> dict:
    return asyncio.run(sweep_handler({"deliverable_id": did}, repo_root=repo / ".git"))


@pytest.fixture
def no_dispatch(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("reentrant_dispatch must not be called")

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", boom)


def test_live_open_baton_is_skipped_not_terminal_and_op_never_resolved(tmp_path, no_dispatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rel = _baton(repo, state="ready_to_fire", shipped_in=None, archived=False)
    out = mod.fire_for_batons(repo, repo / ".git", [rel], "a" * 40)
    assert out == {"fired": [], "skipped": [{"baton": rel, "reason": "not-terminal"}]}


def test_archived_shipped_baton_fires_once_and_backstop_is_clean(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rel = _baton(repo, state="shipped", shipped_in="abc1234", archived=True)
    _seed_handoff(repo, "2026-10-21-peer.md", deliverable_id=_DID)
    sha = _commit_all(repo, "seed")
    assert _sweep(repo, _DID)["divergences"]

    state = mod.resolve_run_baton(repo, rel)
    assert state is not None and state.rel == f"archive/handoffs/2026-10/{_NAME}"
    assert (state.deployment_state, state.deliverable_id) == ("shipped", _DID)

    out = mod.fire_for_batons(repo, repo / ".git", [rel], sha)

    assert len(out["fired"]) == 1 and out["skipped"] == []
    fired = out["fired"][0]
    assert fired["advanced"] == ["state/handoffs/2026-10-21-peer.md"]
    assert fired["source"] == state.rel and fired["commit_sha"]
    assert "commit_error" not in fired
    assert _sweep(repo, _DID)["divergences"] == []


def test_two_terminal_batons_with_one_deliverable_dispatch_once(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    a = _baton(repo, state="shipped", shipped_in="abc1234", archived=False, name="2026-10-21-a.md")
    b = _baton(repo, state="shipped", shipped_in="abc1234", archived=False, name="2026-10-21-b.md")
    calls = []

    def fake(op, params, *, repo_root=None):
        calls.append(params)
        return {"by_kind": {}, "commit_sha": None}

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", fake)
    out = mod.fire_for_batons(repo, repo / ".git", [a, b], None)
    assert len(calls) == 1 and len(out["fired"]) == 1
    assert calls[0]["source_path"] == a and calls[0]["ship_sha"] == "abc1234"


def test_terminal_baton_without_ship_evidence_is_skipped(tmp_path, no_dispatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rel = _baton(repo, state="shipped", shipped_in=None, archived=False)
    out = mod.fire_for_batons(repo, repo / ".git", [rel], None)
    assert out["skipped"] == [{"baton": rel, "reason": "no-ship-sha"}]


def test_baton_without_valid_deliverable_id_is_skipped(tmp_path, no_dispatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rel = _baton(repo, state="shipped", shipped_in="abc1234", archived=False, did="plain-id")
    out = mod.fire_for_batons(repo, repo / ".git", [rel], "a" * 40)
    assert out["skipped"] == [{"baton": rel, "reason": "no-deliverable-id"}]


@pytest.mark.parametrize("rel", [
    "../escape.md", "state/handoffs/../../README.md", "README.md", "docs/plans/x.md",
    "/etc/hosts", "state/handoffs/missing.md", "",
])
def test_path_outside_homes_or_with_dotdot_is_unreadable(tmp_path, no_dispatch, rel):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert mod.resolve_run_baton(repo, rel) is None
    out = mod.fire_for_batons(repo, repo / ".git", [rel], "a" * 40)
    assert out["skipped"] == [{"baton": rel, "reason": "unreadable"}]


def test_unavailable_op_is_skipped_by_name(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rel = _baton(repo, state="shipped", shipped_in="abc1234", archived=False)

    def gone(*a, **k):
        raise entry_seam.OpUnavailableError("suspended")

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", gone)
    out = mod.fire_for_batons(repo, repo / ".git", [rel], None)
    assert out == {"fired": [], "skipped": [{"baton": rel, "reason": "op-unavailable"}]}


def test_op_exception_lands_as_commit_error_naming_the_type(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rel = _baton(repo, state="shipped", shipped_in="abc1234", archived=False)

    def bad(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", bad)
    out = mod.fire_for_batons(repo, repo / ".git", [rel], None)
    assert out["fired"][0]["commit_error"] == "RuntimeError"
