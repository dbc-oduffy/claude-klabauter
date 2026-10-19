"""The landing-export re-run in the runner's BASELINE_CONTEXT clause and the executor's foreign-red rule."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

SHA = "b" * 40
DOOR = "python -m coordinator_core.ops.dispatch_emit.landing_export"


def test_the_baseline_clause_carries_the_landing_export_door_and_foreign_key():
    clause = emit._baseline_context_clause(SHA, ["coordinator_core/x"])
    assert DOOR in clause
    assert f"scratch/test-landing-{SHA[:8]}" in clause
    assert "baseline.foreign" in clause


def test_the_test_call_carries_the_clause_with_a_sha_and_not_without():
    with_sha = emit._test_agent_call_expr(["a/tests/test_one.py"], review_edits_base=SHA)
    assert "baseline.foreign" in with_sha
    assert "baseline.foreign" not in emit._test_agent_call_expr(["a/tests/test_one.py"], review_edits_base=None)


def test_the_executor_brief_carries_the_foreign_red_sentence_once():
    row = WaveRow(id="C1", title="t", surface="s", writes=["a/b.py"], reads=[], depends_on=[])
    contract = emit._row_return_contract(row, "docs/plans/p.md")
    assert contract.count("is a peer's dirt") == 1
    assert "it is not a BLOCKED" in contract


def test_the_clauses_name_no_absolute_path():
    text = emit._baseline_context_clause(SHA, ["coordinator_core/x"]) + emit._foreign_red_clause()
    assert not re.search(r"[A-Za-z]:[\\/]|/home/|/Users/", text)
