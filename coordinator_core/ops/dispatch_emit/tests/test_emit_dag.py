"""Tests for DAG emission (§ Design D4): per-row promises, no commit/
preflight phases, the terminal-commit-request marker, and per-row
verification.

Spec: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md,
row C12. Pins AC9-AC12, AC21 and AC23's script-shape half.
"""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.pathspec import NoWritesDeclaredError
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

_JS_STRING_LITERAL_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _write_row(id_, writes=None, depends_on=None, reads=None):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface="dispatch_emit",
        writes=writes if writes is not None else [f"pkg/{id_}.py"],
        reads=reads or [],
        depends_on=depends_on or [],
    )


def test_no_commit_agent_no_preflight_no_commit_wave_phase_no_parallel_wrap():
    waves = [[_write_row("C1"), _write_row("C2")]]
    script = compose_script(waves, name="wf", description="two rows")

    assert "coordinator:git-commit-agent" not in script
    assert "Preflight" not in script
    assert "Commit wave" not in script
    assert "parallel(" not in script


def test_seven_disjoint_write_rows_emit_seven_rows_with_empty_after():
    waves = [[_write_row(f"C{i}") for i in range(1, 8)]]
    script = compose_script(waves, name="wf", description="seven disjoint rows")

    for i in range(1, 8):
        assert f"_rows['C{i}'] = _runRow('C{i}', []," in script


def test_seven_write_rows_run_with_no_slot_limit(monkeypatch):
    """AC24: the retired ≤5 write-capable-executor cap leaves no runtime
    slot-limit machinery behind -- no `_writeSlots`-named binding, no
    write-capable/write-slot concept in the composed script at all."""
    waves = [[_write_row(f"C{i}") for i in range(1, 8)]]
    script = compose_script(waves, name="wf", description="seven write rows")

    assert "_writeSlots" not in script
    assert "writeCapable" not in script
    assert "_acquireWriteSlot" not in script
    assert "_releaseWriteSlot" not in script
    assert "WRITE_SLOT_CAP" not in script


def test_each_rows_runRow_call_lists_exactly_its_after_ids():
    waves = [
        [_write_row("C1", writes=["pkg/shared.py"])],
        [_write_row("C2", writes=["pkg/shared.py"])],
    ]
    script = compose_script(waves, name="wf", description="dependent rows")

    assert "_rows['C1'] = _runRow('C1', []," in script
    assert "_rows['C2'] = _runRow('C2', [_rows['C1']]," in script


def test_marker_paths_equal_commit_pathspec_or_none_after_gitignore_filter():
    waves = [[_write_row("C1", writes=["pkg/c1.py"])]]
    script = compose_script(
        waves,
        name="wf",
        description="one row",
        session_id="sess-1",
        deliverable_id="deliv-1",
    )
    request = parse_marker(script)
    assert request is not None
    assert len(request.chunks) == 1
    assert request.chunks[0].id == "C1"
    assert "pkg/c1.py" in request.chunks[0].paths
    assert request.session_id == "sess-1"
    assert request.deliverable_id == "deliv-1"


def test_all_empty_writes_spine_carries_no_marker():
    waves = [[_write_row("C1", writes=[])]]
    script = compose_script(waves, name="wf", description="empty writes")
    assert parse_marker(script) is None


def test_all_undeclared_spine_still_raises_no_writes_declared():
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED

    waves = [[_write_row("C1", writes=UNDECLARED)]]
    with pytest.raises(NoWritesDeclaredError):
        compose_script(waves, name="wf", description="undeclared writes")


def test_run_base_sha_appears_in_the_prep_narration(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import emit as emit_mod

    monkeypatch.setattr(emit_mod, "head_sha", lambda repo: "deadbeefcafef00d")
    waves = [[_write_row("C1", writes=["pkg/c1.py"])]]
    sha = emit_mod.head_sha(tmp_path)
    script = compose_script(waves, name="wf", description="sha row", run_base_sha=sha)
    assert "deadbeefcafef00d" in script


def test_stop_rule_row_leaves_not_yet_started_rows_undispatched():
    waves = [
        [_write_row("C1", writes=["pkg/c1.py"])],
        [_write_row("C2", writes=["pkg/c2.py"], depends_on=["C1"])],
    ]
    script = compose_script(waves, name="wf", description="stop rule chain")

    assert "_notStarted.push(id);" in script
    assert "BLOCKED: run halted by stop rule" in script


def test_emitted_dag_fixture_passes_run_checks_with_zero_errors():
    waves = [[_write_row(f"C{i}") for i in range(1, 4)]]
    script = compose_script(waves, name="wf", description="run-checks fixture")
    findings = run_checks(script)
    errors = [f for f in findings if f.severity == Severity.ERROR]
    assert errors == []


def test_verified_row_carries_exactly_one_verify_call_gated_on_done(tmp_path):
    target = tmp_path / "pkg" / "c1.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    test_target = tmp_path / "pkg" / "tests" / "test_c1.py"
    test_target.parent.mkdir(parents=True)
    test_target.write_text("def test_x(): pass\n", encoding="utf-8")

    waves = [[_write_row("C1", writes=["pkg/c1.py"])]]
    script = compose_script(
        waves, name="wf", description="verified row", repo_root=tmp_path
    )
    assert script.count("label: 'verify:' + id") == 1
    assert "if (!incomplete && verifyScope)" in script
