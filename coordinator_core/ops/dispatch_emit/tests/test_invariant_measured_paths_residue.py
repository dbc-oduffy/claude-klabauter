"""invariant_findings excludes halted-run residue paths from the landing diff it judges."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import halted_hold_release as hhr
from coordinator_core.ops.dispatch_emit import landing_integrity as li

NODE = "coordinator_core/tests/test_raw_writes_have_a_disposition.py"
RESIDUE = "coordinator_core/halted_peer.py"
OWN = "coordinator_core/own.py"


def _root(tmp_path):
    f = tmp_path / NODE
    f.parent.mkdir(parents=True)
    f.write_text("def test_the_live_gate_is_green():\n    pass\n", encoding="utf-8")
    return tmp_path


def _residue(monkeypatch, paths):
    monkeypatch.setattr(hhr, "residue_paths", lambda root, run_id: frozenset(paths))


def test_a_halted_peers_path_selects_no_invariant(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _residue(monkeypatch, [RESIDUE])
    assert li.invariant_findings(root, [RESIDUE], "run-a", {}) == []


def test_the_runs_own_path_still_selects_the_invariant(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _residue(monkeypatch, [RESIDUE])
    found = li.invariant_findings(root, [RESIDUE, OWN], "run-a", {})
    assert [f.path for f in found] == [NODE]


def test_empty_residue_reproduces_the_unfiltered_result(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _residue(monkeypatch, [])
    found = li.invariant_findings(root, [RESIDUE], "run-a", {})
    assert [f.path for f in found] == [NODE]


def test_no_run_id_skips_residue(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _residue(monkeypatch, [RESIDUE])
    assert [f.path for f in li.invariant_findings(root, [RESIDUE], None, {})] == [NODE]
