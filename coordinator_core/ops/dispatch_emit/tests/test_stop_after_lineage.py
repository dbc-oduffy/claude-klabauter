"""lineage_stop_after follows run.json ancestry; write_stop_after_marker is the one marker writer."""
from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import ask_contract as ac


def _run(root, rid, *, marker=None, **links):
    d = ac.record_dir(root, rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(links), encoding="utf-8")
    if marker:
        ac.write_stop_after_marker(d, marker, "em")
    return d


def test_marker_roundtrip(tmp_path):
    d = _run(tmp_path, "r1")
    m = ac.write_stop_after_marker(d, "plan", "  a   b ")
    on_disk = json.loads((d / ac.STOP_AFTER_FILE).read_text())
    assert on_disk == m and m["stop_after"] == "plan" and m["from"] == "a b"


def test_own_marker_and_unmarked(tmp_path):
    _run(tmp_path, "r1", marker="plan")
    _run(tmp_path, "r2")
    assert ac.lineage_stop_after(tmp_path, "r1") == "plan"
    assert ac.lineage_stop_after(tmp_path, "r2") is None


def test_each_lineage_key_inherits(tmp_path):
    _run(tmp_path, "parent", marker="plan")
    for key in ac.LINEAGE_KEYS:
        _run(tmp_path, f"child-{key}", **{key: "parent"})
        assert ac.lineage_stop_after(tmp_path, f"child-{key}") == "plan", key


def test_grandparent_and_non_plan_marker_refused(tmp_path):
    _run(tmp_path, "gp", marker="plan")
    _run(tmp_path, "p", chain_parent="gp")
    _run(tmp_path, "c", resumed_from="p")
    assert ac.lineage_stop_after(tmp_path, "c") == "plan"
    d = _run(tmp_path, "other")
    (d / ac.STOP_AFTER_FILE).write_text(json.dumps({"stop_after": "execute"}))
    _run(tmp_path, "c2", chain_parent="other")
    assert ac.lineage_stop_after(tmp_path, "c2") is None


def test_cycle_missing_and_unsafe_ids(tmp_path):
    _run(tmp_path, "a", chain_parent="b")
    _run(tmp_path, "b", chain_parent="a")
    assert ac.lineage_stop_after(tmp_path, "a") is None
    _run(tmp_path, "m", chain_parent="ghost")
    assert ac.lineage_stop_after(tmp_path, "m") is None
    assert ac.lineage_stop_after(tmp_path, "../x") is None


def test_depth_bound(tmp_path):
    _run(tmp_path, "n0", marker="plan")
    n = ac._LINEAGE_MAX_DEPTH + 2
    for i in range(1, n):
        _run(tmp_path, f"n{i}", chain_parent=f"n{i - 1}")
    assert ac.lineage_stop_after(tmp_path, "n3") == "plan"
    assert ac.lineage_stop_after(tmp_path, f"n{n - 1}") is None


def test_emit_inheritance_skips_a_resumed_parked_run_but_keeps_chain_parent_and_unparked_resume(tmp_path):
    from coordinator_core.ops.dispatch_emit.op import _inherited_stop_after

    _run(tmp_path, "ask-root", marker="plan")
    parked = _run(tmp_path, "warp-parked", chain_parent="ask-root")
    (parked / "halted.json").write_text(json.dumps({"status": "halted", "halted": "parked"}))
    halted = _run(tmp_path, "warp-halted", chain_parent="ask-root")
    (halted / "halted.json").write_text(json.dumps({"status": "halted", "halted": "gate"}))
    assert _inherited_stop_after(tmp_path, "new", None, "warp-parked") is None
    assert _inherited_stop_after(tmp_path, "new", None, "warp-halted") == "plan"
    assert _inherited_stop_after(tmp_path, "new", "ask-root", "warp-parked") == "plan"
