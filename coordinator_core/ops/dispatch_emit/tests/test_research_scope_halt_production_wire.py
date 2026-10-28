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
    out = halt_product_gates.gate_halt_product(tmp_path, RUN, PATHS, [], owner, PATHS, plan=None)
    return out, seen


def test_settle_receives_the_gate_pairs_and_a_staged_path_clears(tmp_path, monkeypatch):
    holds = [("DELIVERY.md", SIB)]
    out, seen = _gate(tmp_path, monkeypatch, holds, lambda pairs: ({"DELIVERY.md": []}, []))
    assert seen == [holds]
    assert out == {}


def test_only_still_held_paths_halt_as_co_declared(tmp_path, monkeypatch):
    holds = [("DELIVERY.md", SIB), ("src/a.py", SIB)]
    out, _ = _gate(tmp_path, monkeypatch, holds, lambda pairs: ({"DELIVERY.md": []}, [("src/a.py", SIB)]))
    assert out == {"src/a.py": peer_hunk_gate.PEER_HUNKS_CO_DECLARED}


def test_real_settle_keeps_unreadable_claims_held(tmp_path, monkeypatch):
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds",
        lambda root, run, paths, declared, **kw: [("DELIVERY.md", peer_hunk_gate.UNREADABLE_PEER_CLAIMS)],
    )
    owner = {p: "C1" for p in PATHS}
    out = halt_product_gates.gate_halt_product(tmp_path, RUN, PATHS, [], owner, PATHS, plan=None)
    assert out == {"DELIVERY.md": peer_hunk_gate.PEER_HUNKS_CO_DECLARED}
