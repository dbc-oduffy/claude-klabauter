"""Pins for the update-docs / distill process-time benchmark's helpers and verdict."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.architecture_survey import structure
from coordinator_core.benchmarks import update_docs_distill_process_time as bench


def test_resolve_prefers_worklist_when_registered():
    seen = {"ceremony.update_docs_worklist": object(), "ceremony.update_docs_scan": object()}
    name, handler = bench.resolve_update_docs_op(seen.get)
    assert name == "ceremony.update_docs_worklist"
    assert handler is seen[name]


def test_resolve_falls_back_to_scan_when_worklist_absent():
    scan = object()
    name, handler = bench.resolve_update_docs_op({"ceremony.update_docs_scan": scan}.get)
    assert (name, handler) == ("ceremony.update_docs_scan", scan)


def test_resolve_raises_when_neither_registered():
    with pytest.raises(RuntimeError):
        bench.resolve_update_docs_op(lambda name: None)


def test_rag_off_makes_the_structure_post_raise_and_restores():
    real = structure._default_post
    with bench.rag_axis("rag_off") as stats:
        with pytest.raises(structure.StructureUnavailable):
            structure._default_post("http://x", {}, 0.1)
        assert stats == {"calls": 1, "answered": False}
    assert structure._default_post is real


def test_rag_on_delegates_to_the_real_post(monkeypatch):
    monkeypatch.setattr(structure, "_default_post", lambda url, payload, timeout_s: {"ok": 1})
    with bench.rag_axis("rag_on") as stats:
        assert structure._default_post("http://x", {}, 0.1) == {"ok": 1}
    assert stats == {"calls": 1, "answered": True}


def test_rag_on_unreachable_is_reported_not_answered(monkeypatch):
    def down(url, payload, timeout_s):
        raise structure.StructureUnavailable("down")

    monkeypatch.setattr(structure, "_default_post", down)
    with bench.rag_axis("rag_on") as stats:
        with pytest.raises(structure.StructureUnavailable):
            structure._default_post("http://x", {}, 0.1)
    assert stats == {"calls": 1, "answered": False}


def test_unknown_axis_is_refused():
    with pytest.raises(ValueError):
        with bench.rag_axis("sideways"):
            pass


def test_measure_op_counts_spawns_bytes_and_worklist():
    def call():
        subprocess.run([sys.executable, "-c", "pass"], check=True)
        return {"worklist": [1, 2, 3]}

    row = bench.measure_op("fake.op", call, bench.update_docs_worklist_len, "rag_off", runs=2)
    assert row["op_name"] == "fake.op"
    assert len(row["runs"]) == 2
    assert row["spawns"] == 1
    assert row["worklist_len"] == 3
    assert row["output_bytes"] == len('{"worklist": [1, 2, 3]}')
    assert row["process_ms"] >= 0


def test_worklist_len_shapes():
    assert bench.update_docs_worklist_len({"phase8b_prune": [1, 2]}) == 2
    assert bench.update_docs_worklist_len({"worklist": [1], "phase8b_prune": [1, 2]}) == 1
    assert bench.distill_worklist_len({"counts": {"scannable": 7, "batches": 2}}) == 7
    assert bench.distill_worklist_len({"counts": {"batches": 2}}) == 2


def test_worklist_len_reads_the_worklist_ops_real_key():
    real_shape = {"ack_head": "x", "decided_by_op": {}, "agent_worklist": [{"kind": "a"}, {"kind": "b"}]}
    assert bench.update_docs_worklist_len(real_shape) == 2
    assert bench.update_docs_worklist_len({"agent_worklist": [], "phase8b_prune": [1]}) == 0
    assert bench.update_docs_worklist_len({"agent_worklist": [1], "worklist": [1, 2, 3]}) == 1


def _manifest(batches, cohorts):
    return {"manifest": {"batches": batches, "cohorts": cohorts}}


def test_scope_fileset_sha_is_order_independent_and_sensitive_to_membership():
    base = bench.scope_fileset_sha256(_manifest([["b", "a"], ["c"]], {"harvest": ["a", "b"], "skip": ["z"]}))
    assert len(base) == 64
    same = bench.scope_fileset_sha256(_manifest([["c"], ["a", "b"]], {"skip": ["z"], "harvest": ["b", "a"]}))
    assert same == base
    assert bench.scope_fileset_sha256(_manifest([["a", "b"], ["d"]], {"harvest": ["a", "b"], "skip": ["z"]})) != base
    assert bench.scope_fileset_sha256(_manifest([["b", "a"], ["c"]], {"harvest": ["a"], "skip": ["z", "b"]})) != base


def test_scope_fileset_sha_reads_the_manifest_at_input_json_path(tmp_path):
    import json

    manifest = {"batches": [["a"]], "cohorts": {"harvest": ["a"]}}
    (tmp_path / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
    got = bench.scope_fileset_sha256({"input_json_path": "m.json"}, tmp_path)
    assert got == bench.scope_fileset_sha256({"manifest": manifest})
    with pytest.raises(ValueError):
        bench.scope_fileset_sha256({"input_json_path": "m.json"})


def test_verdict_pins_both_sides_of_the_bar():
    assert bench.verdict([{"process_ms": 499.9}, {"process_ms": 10.0}])
    assert not bench.verdict([{"process_ms": 500.0}])
    assert not bench.verdict([{"process_ms": 10.0}, {"process_ms": 1288.0}])


def test_main_exit_code_follows_verdict(monkeypatch, capsys):
    import coordinator_core.benchmarks as pkg

    monkeypatch.setattr(pkg, "declare_benchmark_origin", lambda: None)
    monkeypatch.setattr(bench, "run", lambda repo, runs: {"pass": False, "ops": []})
    assert bench.main(["--runs", "1"]) == 1
    monkeypatch.setattr(bench, "run", lambda repo, runs: {"pass": True, "ops": []})
    assert bench.main(["--runs", "1"]) == 0


def test_estimate_is_labelled_and_survives_a_missing_workflow_input(tmp_path):
    est = bench.estimate_before_tokens({"phase8b_prune": []}, {"workflow_input_path": "nope.json"}, tmp_path)
    assert est["estimate"] is True
    assert est["update_docs"]["estimate"] is True
    assert est["distill"]["estimate"] is True and "unavailable" in est["distill"]
