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
    result = {"decided_by_op": {"archived_handoffs": {"total": 3, "prune": 1, "keep": 1, "indeterminate": 1}, "prune": {"archived_handoffs": ["x"]}}}
    cov = census.archived_handoff_coverage(result, census.archived_handoff_total(tmp_path))
    assert cov == {"classified": 3, "total": 3, "ratio": 1.0}


def test_partial_coverage_is_reported_not_rounded_up():
    result = {"decided_by_op": {"archived_handoffs": {"total": 4, "prune": 1, "keep": 1}}}
    assert census.archived_handoff_coverage(result, 4)["ratio"] == 0.5


def test_pass_follows_the_bar(tmp_path):
    assert census.run(tmp_path, runs=1, call=lambda: {})["pass"] is True


def _unavailable():
    from coordinator_core.warm.entry_seam import OpUnavailableError

    raise OpUnavailableError("not registered")


def test_every_op_is_measured_and_an_unregistered_one_reads_absent(tmp_path):
    calls = {census.OP: lambda: {}, census.INDEX_OP: lambda: {}, census.BOOKKEEPING_OP: _unavailable}
    report = census.run(tmp_path, runs=1, calls=calls)
    assert set(report["ops"]) == set(census.OPS)
    assert report["ops"][census.INDEX_OP]["status"] == "measured"
    assert report["ops"][census.INDEX_OP]["spawns"] == 0
    assert report["ops"][census.BOOKKEEPING_OP] == {"op": census.BOOKKEEPING_OP, "status": "absent"}
    assert report["absent_ops"] == [census.BOOKKEEPING_OP]
    assert report["pass"] is False


def test_all_ops_measured_passes(tmp_path):
    calls = {op: (lambda: {}) for op in census.OPS}
    assert census.run(tmp_path, runs=1, calls=calls)["pass"] is True


def test_reply_tokens_count_the_composite_once(tmp_path):
    inventory, index = {"a": "x" * 400}, {"b": "y" * 400}
    calls = {
        census.OP: lambda: inventory,
        census.INDEX_OP: lambda: index,
        census.BOOKKEEPING_OP: lambda: {"inventory": inventory, "index_regen": index},
    }
    report = census.run(tmp_path, runs=1, calls=calls)
    composite = report["ops"][census.BOOKKEEPING_OP]["reply_tokens"]
    parts = report["ops"][census.OP]["reply_tokens"] + report["ops"][census.INDEX_OP]["reply_tokens"]
    assert report["reply_tokens"] == composite
    assert composite > parts - 1 and report["reply_tokens"] != composite + parts


def test_reply_tokens_sum_the_base_ops_when_the_composite_is_absent(tmp_path):
    calls = {census.OP: lambda: {"a": "x" * 400}, census.INDEX_OP: lambda: {"b": "y" * 400}, census.BOOKKEEPING_OP: _unavailable}
    report = census.run(tmp_path, runs=1, calls=calls)
    assert report["reply_tokens"] == report["ops"][census.OP]["reply_tokens"] + report["ops"][census.INDEX_OP]["reply_tokens"]


def test_pass_fails_when_any_measured_op_is_over_the_bar(tmp_path, monkeypatch):
    monkeypatch.setattr(census, "BAR_MS", -1.0)
    assert census.run(tmp_path, runs=1, calls={census.INDEX_OP: lambda: {}})["pass"] is False


def test_token_axis_is_before_and_unmeasured_after_without_a_transcript(tmp_path):
    axis = census.run(tmp_path, runs=1, call=lambda: {"phase_verdicts": {"1": "agent"}})["token_axis"]
    assert axis["before"]["tokens"] == 148000
    assert axis["after"] == {"tokens": "unmeasured", "agent_phases": 1}


def test_token_axis_after_sums_a_transcript_through_ceremony_token_census(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"id":"m1","content":[],"usage":{"input_tokens":10,"output_tokens":5}}}\n',
        encoding="utf-8",
    )
    after = census.token_axis(0, transcript, None)["after"]
    assert (after["input_tokens"], after["output_tokens"], after["tokens"]) == (10, 5, 15)
