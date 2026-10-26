"""Pins the executor-economy contract values and its import hygiene."""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.ops.dispatch_emit import executor_economy as ee


def test_values_pinned():
    assert ee.CONTEXT_CEILING_TOKENS == 150_000
    assert ee.CHECKPOINT_STATUS == "CHECKPOINT"
    assert ee.MAX_CONTINUATIONS == 2
    assert ee.EXCERPT_HEADING == "## Row excerpts"
    assert ee.EXCERPT_MAX_BYTES == 1200
    assert ee.EXCERPT_MAX_SYMBOLS_PER_FILE == 40
    assert ee.SYMBOL_CONTEXT_HEADING == "## Symbol context"
    assert ee.SYMBOL_CONTEXT_MAX_ENTRIES == 24
    assert ee.RUN_COST_FILE == "run-cost.json"
    assert ee.PENDING_REVISIONS_OP == "workflow.pending_revisions"
    assert ee.CLAIM_ROWS_OP == "plan.claim_register_rows"
    assert ee.ABSORB_ROUNDS == 1
    assert ee.MEASURED_BASELINE == {
        "executor_peak_context": 366000,
        "executor_tool_calls": 254,
        "executor_max_same_file_reads": 32,
    }
    assert isinstance(ee.MEASURED_BASELINE, dict)


def test_symbol_context_contract_pinned():
    from coordinator_core.ops.dispatch_emit import symbol_context as sc

    assert sc.SYMBOL_CONTEXT_FIELD == "symbol_context"
    assert sc.SYMBOL_CONTEXT_UNAVAILABLE_FIELD == "symbol_context_unavailable"
    assert sc.REASONS == ("no-index", "stale-index", "store-error", "no-symbols")
    e = sc.SymbolContextEntry("f", "a.py", 3, "caller", "g")
    assert (e.symbol, e.file, e.line, e.role, e.of) == ("f", "a.py", 3, "caller", "g")
    assert sc.lookup("/r", [{"id": "C0"}]) == {"C0": sc.SymbolContext((), "no-index")}


def test_ratchet_metrics_exclude_execute_share_and_match_baseline():
    assert ee.RATCHET_METRICS == (
        "executor_peak_context",
        "executor_tool_calls",
        "executor_max_same_file_reads",
    )
    assert set(ee.RATCHET_METRICS) == set(ee.MEASURED_BASELINE)


def test_messages():
    assert "batch" in ee.BATCHING_CLAUSE.lower()
    msg = ee.ceiling_message(160_000)
    assert "160000" in msg and "150000" in msg
    assert "CHECKPOINT: <path>" in msg


def test_checkpoint_path_is_posix_for_windows_inputs():
    drive = "Z" + ":"
    assert ee.checkpoint_path(drive + "\\x\\side\\C0\\", "C0", 1) == drive + "/x/side/C0/C0.checkpoint-1.md"
    assert ee.checkpoint_path("a/b", "R2", 2) == "a/b/R2.checkpoint-2.md"
    assert "\\" not in ee.checkpoint_path("a\\b", "R2", 1)


def test_module_imports_nothing_from_coordinator_core_ops():
    src = Path(ee.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("coordinator_core")
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("coordinator_core") for a in node.names)
