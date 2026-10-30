"""Production wire: gate_halt_product feeds the co-declared holds through co_declared_settle.settle."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import co_declared_settle, halt_product_gates, peer_hunk_gate

RUN = "ask-20261027T000000-aaaaaa"
SIB = "ask-20261027T000000-bbbbbb"
PATHS = ["DELIVERY.md", "src/a.py"]


def _gate(tmp_path, monkeypatch, holds, settle):
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda root, run, paths, declared, **kw: holds)
    seen: list = []

    def fake_settle(root, run, pairs, **kw):
        seen.append(list(pairs))
        return settle(pairs)

    monkeypatch.setattr(co_declared_settle, "settle", fake_settle)
    owner = {p: "C1" for p in PATHS}
    provenance: list = []
    out = halt_product_gates.gate_halt_product(tmp_path, RUN, PATHS, [], owner, PATHS, plan=None, provenance=provenance)
    return out, seen, provenance


def test_settle_receives_the_gate_pairs_and_a_staged_path_clears(tmp_path, monkeypatch):
    holds = [("DELIVERY.md", SIB)]
    out, seen, provenance = _gate(tmp_path, monkeypatch, holds, lambda pairs: ({"DELIVERY.md": []}, []))
    assert seen == [holds]
    assert out == {}
    assert provenance == []


def test_only_still_held_paths_are_recorded_as_co_declared_provenance(tmp_path, monkeypatch):
    holds = [("DELIVERY.md", SIB), ("src/a.py", SIB)]
    out, _, provenance = _gate(
        tmp_path, monkeypatch, holds, lambda pairs: ({"DELIVERY.md": []}, [("src/a.py", SIB)]),
    )
    assert out == {}
    assert provenance == [("src/a.py", peer_hunk_gate.PEER_HUNKS_CO_DECLARED, SIB)]


def test_real_settle_records_unreadable_claims_as_co_declared_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds",
        lambda root, run, paths, declared, **kw: [("DELIVERY.md", peer_hunk_gate.UNREADABLE_PEER_CLAIMS)],
    )
    owner = {p: "C1" for p in PATHS}
    provenance: list = []
    out = halt_product_gates.gate_halt_product(tmp_path, RUN, PATHS, [], owner, PATHS, plan=None, provenance=provenance)
    assert out == {}
    assert provenance == [
        ("DELIVERY.md", peer_hunk_gate.PEER_HUNKS_CO_DECLARED, peer_hunk_gate.UNREADABLE_PEER_CLAIMS)
    ]
