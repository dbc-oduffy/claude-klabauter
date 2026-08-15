"""
coordinator_core.ops.tests.test_gen_dod_backlog_fragment

Tests for scripts/gen_dod_backlog_fragment.py — the per-op x per-dimension DoD
backlog generator (C9 leg (a)). Imported as a module (never subprocessed, same
constraint as test_gen_ported_ops_fragment.py), so every assertion below calls
straight into the script's own functions.

Deliberately asserts NO absolute op/row count anywhere in this file — that is
the exact failure mode C9 exists to end (the plan's op-count prose was pinned
wrong three times: 73 -> 138 -> 176). Counts here are always derived from the
same live discovery walk they are compared against.

Coverage:
    (a) row-shape         — build_rows() over a synthetic record list produces
                             exactly len(records) * len(DIMENSIONS) rows, one
                             per (op, dimension), each carrying the pinned
                             verdict vocabulary's initial state and the pinned
                             gate_op_key literal.
    (b) ops-scope-filter   — a hooks/** record (out of C6's ported-ops-paths
                             scope) is excluded from the backlog population,
                             matching render_paths_txt()'s own scope.
    (c) determinism        — two independent build_rows() calls over the same
                             synthetic input, and two independent render_json()
                             calls over the live repo walk, produce byte-
                             identical output.
    (d) freshness-check-committed — check_freshness() against the real
                             committed .github/dod-backlog.json returns no
                             drift (current as of this baton).
    (e) write-artifact-idempotent-and-red-path — write_artifact() against a
                             synthetic output path: fresh-tree idempotence,
                             and the red path (hand-staled artifact -> non-
                             empty check_freshness() -> repaired by
                             write_artifact() -> clean again).
    (f) fragment-consistency — the backlog's derived op population matches
                             C6's own ops-scoped record population exactly
                             (same op_keys, same module_paths) — the
                             fragment<->disk consistency property this test
                             module exists to prove, in the same spirit as
                             test_gen_ported_ops_fragment.py's own freshness
                             check, without hardcoding a count on either side.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § Chunks C9
Exercises:     scripts/gen_dod_backlog_fragment.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.gen_dod_backlog_fragment as gdbf  # noqa: E402
import scripts.gen_ported_ops_fragment as gpof  # noqa: E402


# ---------------------------------------------------------------------------
# (a) row shape over a synthetic record list
# ---------------------------------------------------------------------------


def _make_record(
    op_key: str,
    module_path: str,
    classification: Optional[str] = "COMPUTE_ONLY",
) -> "gpof.OpRecord":
    return gpof.OpRecord(
        op_key=op_key,
        module_path=module_path,
        dotted_module=module_path.replace("/", ".").removesuffix(".py"),
        classification=classification,
        key_scope=None,
        in_registry_map=False,
        ported_from=None,
    )


def test_build_rows_emits_one_row_per_op_per_dimension():
    records = [
        _make_record("fixture.op_one", "coordinator_core/ops/fixture_op_one.py"),
        _make_record("fixture.op_two", "coordinator_core/ops/fixture_op_two.py", classification=None),
    ]
    rows = gdbf.build_rows(records=records)
    assert len(rows) == len(records) * len(gdbf.DIMENSIONS)

    for r in records:
        row_dims = {row.dimension for row in rows if row.op_key == r.op_key}
        assert row_dims == set(gdbf.DIMENSIONS)

    for row in rows:
        assert row.verdict == gdbf.INITIAL_VERDICT
        assert row.verdict in gdbf.VERDICT_STATES
        assert row.gate_op_key == "gate.validate_invocable"


def test_build_rows_carries_classification_through():
    records = [_make_record("fixture.op_one", "coordinator_core/ops/fixture_op_one.py", classification="MUTATING")]
    rows = gdbf.build_rows(records=records)
    assert all(row.classification == "MUTATING" for row in rows)


# ---------------------------------------------------------------------------
# (b) ops-scope filter excludes hooks/**
# ---------------------------------------------------------------------------


def test_build_rows_excludes_hooks_scope_records():
    records = [
        _make_record("fixture.ops_op", "coordinator_core/ops/fixture_ops_op.py"),
        _make_record("fixture.hooks_op", "coordinator_core/hooks/fixture_hooks_op.py"),
    ]
    rows = gdbf.build_rows(records=records)
    op_keys = {row.op_key for row in rows}
    assert op_keys == {"fixture.ops_op"}


# ---------------------------------------------------------------------------
# (c) determinism
# ---------------------------------------------------------------------------


def test_build_rows_deterministic_over_same_input():
    records = [
        _make_record("fixture.b_op", "coordinator_core/ops/fixture_b_op.py"),
        _make_record("fixture.a_op", "coordinator_core/ops/fixture_a_op.py"),
    ]
    first = gdbf.render_json(gdbf.build_rows(records=records))
    second = gdbf.render_json(gdbf.build_rows(records=records))
    assert first == second

    # sorted by (op_key, dimension) regardless of input record order
    rows = gdbf.build_rows(records=records)
    op_key_sequence = [row.op_key for row in rows]
    assert op_key_sequence == sorted(op_key_sequence)


def test_live_render_json_deterministic_across_independent_calls():
    """Same inputs (the real repo tree) -> same bytes, run twice independently
    with no shared state between calls."""
    first = gdbf.render_json(gdbf.build_rows())
    second = gdbf.render_json(gdbf.build_rows())
    assert first == second


# ---------------------------------------------------------------------------
# (d) freshness check against the committed artifact
# ---------------------------------------------------------------------------


def test_freshness_check_passes_against_committed_artifact():
    problems = gdbf.check_freshness()
    assert problems == []


# ---------------------------------------------------------------------------
# (e) write_artifact: idempotence + red-path repair
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_artifact_path(tmp_path, monkeypatch):
    json_out = tmp_path / "dod-backlog.json"
    monkeypatch.setattr(gdbf, "JSON_OUT", json_out)
    monkeypatch.setattr(gdbf, "REPO_ROOT", tmp_path)
    return json_out


def test_write_artifact_idempotent_on_already_fresh_tree(synthetic_artifact_path):
    json_out = synthetic_artifact_path

    first = gdbf.write_artifact()
    assert first == json_out
    first_bytes = json_out.read_bytes()

    second = gdbf.write_artifact()
    assert second is None
    assert json_out.read_bytes() == first_bytes
    assert gdbf.check_freshness() == []


def test_write_artifact_repairs_staled_artifact(synthetic_artifact_path):
    json_out = synthetic_artifact_path

    gdbf.write_artifact()
    json_out.write_text('[{"op_key": "stale.fixture.entry"}]\n', encoding="utf-8")

    problems = gdbf.check_freshness()
    assert problems, "expected a hand-staled artifact to be flagged as drift"

    written = gdbf.write_artifact()
    assert written == json_out
    assert gdbf.check_freshness() == []


# ---------------------------------------------------------------------------
# (f) fragment<->disk consistency: backlog op population matches C6's own
# ---------------------------------------------------------------------------


def test_backlog_op_population_matches_c6_ops_scope_exactly():
    """The backlog's derived (op_key, module_path) population must equal C6's
    own ops-scoped record population one-for-one — no more, no fewer, and
    never a hardcoded count on either side of the comparison."""
    c6_records = gpof.discover_records()
    c6_ops_pairs = {
        (r.op_key, r.module_path)
        for r in c6_records
        if r.module_path.startswith(gpof.OPS_ROOT.relative_to(gpof.REPO_ROOT).as_posix() + "/")
    }

    rows = gdbf.build_rows()
    backlog_pairs = {(row.op_key, row.module_path) for row in rows}

    assert backlog_pairs == c6_ops_pairs
    # exactly DIMENSIONS-many rows per (op, module) pair, no drift within the group
    assert len(rows) == len(backlog_pairs) * len(gdbf.DIMENSIONS)


# ---------------------------------------------------------------------------
# (g) CLI entrypoint (main(): --check / --write) -- the actual invocation
# shape a Makefile/CI step would call.
# Review: coordinator:code-reviewer-3c4f24d7 -- main()'s argparse wiring had
# no direct coverage; only the library functions it delegates to were
# exercised.
# ---------------------------------------------------------------------------


def test_main_check_fresh_prints_fresh_and_returns_0(synthetic_artifact_path, capsys):
    gdbf.write_artifact()

    rc = gdbf.main(["--check"])

    assert rc == 0
    assert "fresh." in capsys.readouterr().out


def test_main_check_stale_prints_stale_and_returns_1(synthetic_artifact_path, capsys):
    rc = gdbf.main(["--check"])

    assert rc == 1
    assert "STALE" in capsys.readouterr().err


def test_main_write_writes_and_returns_0(synthetic_artifact_path, capsys):
    json_out = synthetic_artifact_path
    assert not json_out.exists()

    rc = gdbf.main(["--write"])

    assert rc == 0
    assert "wrote" in capsys.readouterr().out
    assert json_out.exists()


def test_main_write_already_fresh_prints_no_changes(synthetic_artifact_path, capsys):
    gdbf.write_artifact()

    rc = gdbf.main(["--write"])

    assert rc == 0
    assert "already fresh" in capsys.readouterr().out


def test_main_check_and_write_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        gdbf.main(["--check", "--write"])
