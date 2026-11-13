"""Pins for the update-docs sweep census: absent-tolerant base shape, and coverage/agent-phase reading."""

from __future__ import annotations

from coordinator_core.benchmarks import update_docs_sweep_census as census


def test_base_op_without_verdicts_or_cohort_reads_zero_and_absent(tmp_path):
    base = {"agent_worklist": [{"kind": "doc_drift_review"}], "decided_by_op": {"prune": {"plans": []}}}
    report = census.run(tmp_path, runs=1, call=lambda: base)
    assert report["agent_phases"] == 0
    assert report["phase_verdicts"] == "absent"
    assert report["archived_handoff_coverage"] == "absent"
    assert report["agent_worklist_items"] == 1
    assert report["token_baseline"]["tokens"] == 148000
    assert report["spawns"] == 0


def test_agent_phases_counts_only_agent_verdicts():
    assert census.agent_phase_count({"phase_verdicts": {"1": "agent", "6": "noop", "8": {"verdict": "agent"}, "9b": "op"}}) == 2


def test_coverage_counts_all_three_states_against_the_disk_total(tmp_path):
    (tmp_path / "archive" / "handoffs" / "2026-11").mkdir(parents=True)
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / "archive" / "handoffs" / "2026-11" / name).write_text("x", encoding="utf-8")
    (tmp_path / "archive" / "handoffs" / "notes.txt").write_text("x", encoding="utf-8")
    result = {"decided_by_op": {"prune": {"archived_handoffs": {"counts": {"prune": 1, "keep": 1, "indeterminate": 1}, "rows": []}}}}
    cov = census.archived_handoff_coverage(result, census.archived_handoff_total(tmp_path))
    assert cov == {"classified": 3, "total": 3, "ratio": 1.0}


def test_partial_coverage_is_reported_not_rounded_up():
    result = {"decided_by_op": {"prune": {"archived_handoffs": {"counts": {"prune": 1, "keep": 1}}}}}
    assert census.archived_handoff_coverage(result, 4)["ratio"] == 0.5


def test_pass_follows_the_bar(tmp_path):
    assert census.run(tmp_path, runs=1, call=lambda: {})["pass"] is True
