"""gate_halt_product routes co-declared peer holds through co_declared_settle."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import co_declared_settle, halt_product_gates, halted_hunk_attribution as hha, peer_hunk_gate

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261027T000000-aaaaaa"
SIB = "ask-20261027T000000-bbbbbb"
PARENT = "ask-parent-20261027T000000-111111"
DELIVERY = "DELIVERY.md"


def _gate(tmp_path, monkeypatch, settled):
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds",
        lambda root, run, paths, declared, **kw: [(p, SIB) for p in paths],
    )
    monkeypatch.setattr(
        co_declared_settle, "settle",
        lambda root, run, holds, **kw: ({p: [] for p, _s in holds if p in settled}, [h for h in holds if h[0] not in settled]),
    )
    paths = ["DELIVERY.md", "src/a.py"]
    owner = {p: "C1" for p in paths}
    prov: list = []
    out = halt_product_gates.gate_halt_product(tmp_path, RUN, paths, [], owner, paths, plan=None, provenance=prov)
    return out, prov


def test_a_settled_co_declared_path_clears_the_gate(tmp_path, monkeypatch):
    out, prov = _gate(tmp_path, monkeypatch, {"DELIVERY.md"})
    assert out == {}
    assert prov == [("src/a.py", peer_hunk_gate.PEER_HUNKS_CO_DECLARED, SIB)]


def test_a_path_settle_keeps_held_is_provenance_not_a_refusal(tmp_path, monkeypatch):
    out, prov = _gate(tmp_path, monkeypatch, set())
    assert out == {}
    assert prov == [
        ("DELIVERY.md", peer_hunk_gate.PEER_HUNKS_CO_DECLARED, SIB),
        ("src/a.py", peer_hunk_gate.PEER_HUNKS_CO_DECLARED, SIB),
    ]


def _text(n, edits=None):
    edits = edits or {}
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, n + 1))


def _git(root, *args):
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def real_world(tmp_path, monkeypatch):
    """A repo with DELIVERY.md at HEAD, two same-parent siblings, and only the peer-hold lookup stubbed."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / DELIVERY).write_text(_text(10))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    baselines: dict = {}
    monkeypatch.setattr(hha, "_baseline", lambda root, run: baselines.get(run))
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds",
        lambda root, run, paths, declared, **kw: [(p, SIB) for p in paths],
    )
    for run in (RUN, SIB):
        rel = tmp_path / contract.run_parent_copy_rel(run)
        rel.parent.mkdir(parents=True, exist_ok=True)
        rel.write_text(json.dumps({"parent_id": PARENT}))

    def baseline(run, text):
        sha = git_objects.write_object(resolve_git_common_dir(tmp_path), b"blob", text.encode())
        baselines.setdefault(run, {})[DELIVERY] = sha

    return tmp_path, baseline


def _run_gate(root):
    return halt_product_gates.gate_halt_product(
        root, RUN, [DELIVERY], [], {DELIVERY: "C1"}, [DELIVERY], plan=None,
    )


def test_live_sibling_disjoint_delivery_hunks_clear_the_gate_through_the_real_settle(real_world):
    root, baseline = real_world
    (root / DELIVERY).write_text(_text(10, {2: "mine", 9: "theirs"}))
    baseline(RUN, _text(10, {9: "theirs"}))
    baseline(SIB, _text(10, {2: "mine"}))
    out = _run_gate(root)
    assert DELIVERY not in out
    assert peer_hunk_gate.PEER_HUNKS_CO_DECLARED not in out.values()


def test_live_sibling_overlapping_delivery_hunks_are_recorded_as_co_declared_provenance(real_world):
    root, baseline = real_world
    (root / DELIVERY).write_text(_text(10, {3: "both"}))
    baseline(RUN, _text(10))
    baseline(SIB, _text(10))
    prov: list = []
    out = halt_product_gates.gate_halt_product(
        root, RUN, [DELIVERY], [], {DELIVERY: "C1"}, [DELIVERY], plan=None, provenance=prov,
    )
    assert out == {}
    assert prov == [(DELIVERY, peer_hunk_gate.PEER_HUNKS_CO_DECLARED, SIB)]
