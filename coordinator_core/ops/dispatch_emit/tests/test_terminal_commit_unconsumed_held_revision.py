"""Both commit gates refuse a ledger-bearing run while a held revision for its sizing is unconsumed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import held_revisions, land_run, requirements_ledger, terminal_commit

SIZING = "state/sizings/2026-10-26-slice-2.yaml"


@pytest.fixture
def run(tmp_path, monkeypatch):
    run_dir = tmp_path / "run-minted-other-id"
    run_dir.mkdir()
    ledger = SimpleNamespace(sizing_rel=SIZING)
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda root, d: ledger)
    return tmp_path, run_dir


def _call_land(root, run_dir):
    return land_run._ledger_gate(root, run_dir, None, None, [], {}, None, {}, None, None)


def _call_terminal(root, run_dir):
    return terminal_commit._ledger_gate(root, run_dir, None)


@pytest.mark.parametrize("gate", [_call_land, _call_terminal])
def test_unconsumed_held_revision_refuses_naming_it(run, gate):
    root, run_dir = run
    entry = held_revisions.hold(root, SIZING, "ask-named-id", note="recount")
    refusal, ledger, _ = gate(root, run_dir)
    assert refusal["refused"] == "held-revision-unconsumed"
    assert refusal["held"][0]["id"] == entry["id"]
    assert entry["id"] in refusal["error"]
    assert "Re-fire" in refusal["error"]


@pytest.mark.parametrize("gate", [_call_land, _call_terminal])
def test_drained_held_revision_does_not_refuse(run, gate):
    root, run_dir = run
    held_revisions.hold(root, SIZING, "ask-named-id", note="recount")
    held_revisions.drain(root, SIZING, run_dir.name)
    refusal, _, _ = gate(root, run_dir)
    assert refusal is None or refusal.get("refused") != "held-revision-unconsumed"


@pytest.mark.parametrize("gate", [_call_land, _call_terminal])
def test_held_refusal_precedes_missing_verdict(run, gate):
    root, run_dir = run
    held_revisions.hold(root, SIZING, "r", note="n")
    refusal, _, _ = gate(root, run_dir)
    assert refusal["refused"] == "held-revision-unconsumed"
    held_revisions.drain(root, SIZING, "x")
    refusal, _, _ = gate(root, run_dir)
    assert refusal["refused"] == "ledger-unjudged"
