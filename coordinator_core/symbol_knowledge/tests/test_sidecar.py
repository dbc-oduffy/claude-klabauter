"""Sidecar I/O and the leg composition of the symbol-knowledge interface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.symbol_knowledge import contract, leg, sidecar

QN = "scip-python python coordinator_core 0.1 `coordinator_core.x`/f()."


def test_read_missing_and_corrupt_is_empty(tmp_path):
    assert sidecar.read_sidecar(tmp_path) == {}
    p = sidecar.sidecar_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert sidecar.read_sidecar(tmp_path) == {}


def test_write_read_roundtrip_and_byte_stable(tmp_path):
    entries = sidecar.merge_entries({}, {"b.f": ("def b", "s1"), QN: ("def a", "s2")})
    path = sidecar.write_sidecar(tmp_path, entries)
    first = path.read_bytes()
    assert sidecar.read_sidecar(tmp_path) == entries
    sidecar.write_sidecar(tmp_path, dict(reversed(list(entries.items()))))
    assert path.read_bytes() == first
    assert list(json.loads(first)) == sorted(entries)
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".atomic-write")]


def test_malformed_entry_dropped_on_read(tmp_path):
    p = sidecar.sidecar_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text('{"ok": {"definition": "d", "sha": "s", "traps": []}, "bad": {"definition": 3}}', encoding="utf-8")
    assert list(sidecar.read_sidecar(tmp_path)) == ["ok"]


def test_merge_keeps_traps_unique_and_replaces_definition():
    base = sidecar.merge_entries({}, {"s": ("old", "a")}, {"s": ["trap 1"]})
    merged = sidecar.merge_entries(base, {"s": ("new", "b")}, {"s": ["trap 1", "trap 2"]})
    assert merged["s"] == {"definition": "new", "sha": "b", "traps": ["trap 1", "trap 2"]}
    only_trap = sidecar.merge_entries({}, {}, {"t": ["x"]})
    assert only_trap["t"] == {"definition": "", "sha": "", "traps": ["x"]}


def test_flagged_symbol_rejects_unknown_kind():
    assert contract.FlaggedSymbol(QN, "a.py", "adjust", True).kind == "adjust"
    with pytest.raises(ValueError):
        contract.FlaggedSymbol(QN, "a.py", "bogus", True)


def _ctx(tmp_path, sha="a" * 40):
    return SimpleNamespace(root=tmp_path, common_dir=tmp_path / ".git", run_id="r1", terminal_sha=sha, record={})


@pytest.fixture
def opted_in(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    monkeypatch.setattr(process_artifact_policy, "hosts_coordinator_state", lambda root: True)
    db = tmp_path / "graph.db"
    db.write_bytes(b"")
    monkeypatch.setattr(leg, "index_graph_db", lambda root: db)


def test_leg_not_applicable_without_terminal_commit(tmp_path):
    assert leg.symbol_knowledge_leg(_ctx(tmp_path, sha=None)).status == "not-applicable"


def test_leg_not_applicable_and_no_launch_when_nothing_flagged(opted_in, monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(contract, "flag_symbols", lambda *a: [])
    monkeypatch.setattr(contract, "launch_batches", lambda *a: launched.append(a) or "x")
    assert leg.symbol_knowledge_leg(_ctx(tmp_path)).status == "not-applicable"
    assert launched == []


def test_leg_queues_with_traps_and_launches_once(opted_in, monkeypatch, tmp_path):
    flagged = [contract.FlaggedSymbol(QN, "a.py", "adjust", True)]
    calls = []
    monkeypatch.setattr(contract, "flag_symbols", lambda *a: flagged)
    monkeypatch.setattr(contract, "record_traps", lambda record, f: {QN: ["revised twice"]})

    def launch(root, run_id, f, traps, sha=None):
        calls.append((run_id, list(f), dict(traps), sha))
        return "1 adjust, 0 net-new queued"

    monkeypatch.setattr(contract, "launch_batches", launch)
    result = leg.symbol_knowledge_leg(_ctx(tmp_path))
    assert (result.status, result.detail) == ("queued", "1 adjust, 0 net-new queued")
    assert calls == [("r1", flagged, {QN: ["revised twice"]}, "a" * 40)]
    assert not sidecar.sidecar_path(tmp_path).exists()


def test_leg_not_applicable_when_repo_not_opted_in(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    monkeypatch.setattr(process_artifact_policy, "hosts_coordinator_state", lambda root: False)
    assert leg.symbol_knowledge_leg(_ctx(tmp_path)).status == "not-applicable"
