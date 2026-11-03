"""The `roadmap.plan_gate` sweep: report behaviour pinned and cost scaling with baton count falsified.

Corpora come from `gate_v2_sweep_profile.build_corpus` (a fixed template into `tmp_path`), never the
live tree, so nothing here drifts with claude-klabauter's handoffs. The digest test is the behaviour pin a
cost cut must leave unchanged; the scaling test holds the per-added-baton cost under its bar.
"""

from __future__ import annotations

import subprocess

from coordinator_core.benchmarks import gate_v2_sweep_profile as prof
from coordinator_core.ops import roadmap_plan_gate as op

N = prof.N_DEFAULT

# sha256 of the sorted-key JSON of each shape's report over `build_corpus(tmp, N)`, taken at base 24ee6f09ae.
BASE_DIGESTS = {
    "A": "5fc00f8b8c531c1eecfe61769abcc160e88d3e143d4781f063aa124370d2e7ce",
    "B": "febcc303d207771a6c271bc04cd17af6755895957fc771b18b0f7b66e1bd01fd",
    "C": "33d9f03915061c2a923032531138f86b3c7fbd3c205f9c36c32a049727a27f29",
    "D": "5fc00f8b8c531c1eecfe61769abcc160e88d3e143d4781f063aa124370d2e7ce",
}


def test_synthetic_corpus_is_live_shaped_and_never_spawns(tmp_path, monkeypatch):
    root = prof.build_corpus(tmp_path, N)
    assert len(list((root / "state" / "handoffs").glob("*.md"))) == N

    def _boom(*args, **kwargs):
        raise AssertionError("roadmap.plan_gate's read path must not create a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    report = op._handler({}, root)
    rows = report["batons"]
    assert rows, "an empty sweep proves nothing about cost"
    assert report["counts"]["candidates"] >= N // 3
    assert any(not r["planning_gate"]["open"] for r in rows), "no blocked baton: closure never ran"
    assert any(r["plan"] for r in rows), "no linked plan: the plan index never ran"
    assert any(r["sized"] for r in rows), "no sized baton: sizing resolution never ran"


def test_shape_reports_match_the_base_digests(tmp_path):
    assert prof.report_digests(prof.build_corpus(tmp_path, N)) == BASE_DIGESTS


def test_digests_distinguish_a_changed_report(tmp_path):
    root = prof.build_corpus(tmp_path, N)
    victim = next((root / "state" / "handoffs").glob("*-synth-0000-3.md"))
    victim.write_text(victim.read_text(encoding="utf-8").replace("ready_to_fire", "in_flight"), encoding="utf-8")
    assert prof.report_digests(root)["B"] != BASE_DIGESTS["B"]


PER_BATON_BAR_MS = 0.1


def test_sweep_cost_per_added_baton_is_under_the_bar():
    """(t(2N) - t(N)) / N < 0.1ms over the synthetic corpus. Reading every head is linear by
    construction, so the per-baton ceiling is the scaling bar; a t(2N)/t(N) ratio swings across 2.0
    on timer noise once the fixed cost is near zero."""
    result = prof.scaling(N)
    assert result["per_added_baton_ms"] < PER_BATON_BAR_MS, (
        f"each added baton costs {result['per_added_baton_ms']:.3f}ms process time "
        f"(t({2 * N}) - t({N})) / {N}; bar {PER_BATON_BAR_MS}ms"
    )
