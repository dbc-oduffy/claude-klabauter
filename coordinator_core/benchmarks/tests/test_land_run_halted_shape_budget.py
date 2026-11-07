"""A land_run of the halted run's shape reaches and passes import_closure inside its budget."""

from __future__ import annotations

from contextlib import ExitStack

import pytest

from coordinator_core.benchmarks import land_run_leg_budget_profile as prof
from coordinator_core.benchmarks.land_run_slow_disk_profile import _bind_session


pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

PER_LEG_BAR_MS = 500.0
IMPORT_CLOSURE_LEGS = (
    prof.LEG_AXIS_PREFIX + "land_run._integrity.import_closure",
    prof.LEG_AXIS_PREFIX + "authoring_leaks.import_closure._read_text",
)


@pytest.fixture(autouse=True)
def _env_restored(monkeypatch):
    for name in ("COORDINATOR_INVOCATION_ORIGIN", "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(name, "x")


def test_halted_shape_landing_reaches_import_closure_under_the_per_leg_bar(tmp_path):
    prof.declare_benchmark_origin()
    _bind_session()
    with ExitStack() as stack:
        shape = prof.build_halted_shape_fixture(tmp_path, stack)
        rows = prof.profile_land(shape["root"], shape["base"], attribute=False, stop_before_commit=True, legs=True)
    whole = rows[0]
    assert whole.attribution["land_deadline_halt"] is False, whole.attribution["outcome"]
    by_axis = {r.axis: r for r in rows}
    for axis in IMPORT_CLOSURE_LEGS:
        row = by_axis[axis]
        assert row.attribution["measured"] and not row.attribution["not_reached"], f"{axis} not reached"
    over = [
        f"{r.axis}={r.process_time_ms}ms"
        for r in rows
        if r.axis.startswith(prof.LEG_AXIS_PREFIX) and not r.attribution.get("excepted") and r.process_time_ms >= PER_LEG_BAR_MS
    ]
    assert not over, f"legs at or over {PER_LEG_BAR_MS}ms process time: {over}"


HALTED_SIBLINGS = 6


def _add_halted_siblings(root, count: int) -> list:
    """`count` in-window halted sibling runs, each with a marker and an index entry withholding a path the landing does not touch."""
    import time

    from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    ids = []
    for i in range(count):
        run_id = f"ask-{stamp}-{i:06x}"
        record = root / RECORD_DIR_ROOT / run_id
        record.mkdir(parents=True)
        halted_run._write(record, {"status": halted_run.STATUS_HALTED, "halted": "commit", "sizing_rel": f"state/sizings/halted-{i}.yaml"})
        halted_withheld_index.add_run(root, run_id, [f"halted/other_{i}.txt"], sizing_rel=f"state/sizings/halted-{i}.yaml")
        ids.append(run_id)
    return ids


def _land_with_halted_siblings(tmp_path, count: int):
    from coordinator_core.ops.dispatch_emit import halted_run

    reads = []
    real = halted_run.read_strict
    halted_run.read_strict = lambda record_dir: (reads.append(str(record_dir)), real(record_dir))[1]
    try:
        prof.declare_benchmark_origin()
        _bind_session()
        with ExitStack() as stack:
            shape = prof.build_halted_shape_fixture(tmp_path, stack)
            _add_halted_siblings(shape["root"], count)
            rows = prof.profile_land(shape["root"], shape["base"], attribute=False, stop_before_commit=True, legs=True)
    finally:
        halted_run.read_strict = real
    by_axis = {r.axis: r for r in rows}
    return rows[0], by_axis, len(reads)


def _land_at_n_and_2n(tmp_path):
    one_dir, two_dir = tmp_path / "n", tmp_path / "n2"
    one_dir.mkdir()
    two_dir.mkdir()
    one, one_axes, one_reads = _land_with_halted_siblings(one_dir, HALTED_SIBLINGS)
    two, two_axes, two_reads = _land_with_halted_siblings(two_dir, 2 * HALTED_SIBLINGS)
    for whole, axes in ((one, one_axes), (two, two_axes)):
        assert whole.attribution["land_deadline_halt"] is False, whole.attribution["outcome"]
        for axis in IMPORT_CLOSURE_LEGS:
            assert axes[axis].attribution["measured"] and not axes[axis].attribution["not_reached"], f"{axis} not reached"
    return one, one_reads, two, two_reads


def test_landing_spawns_are_flat_at_double_the_halted_runs(tmp_path):
    one, _, two, _ = _land_at_n_and_2n(tmp_path)
    assert one.spawns == two.spawns


def test_halted_record_reads_are_flat_at_double_the_halted_runs(tmp_path):
    """Indexed halted runs are answered from their fresh `halted_withheld_index` entry: no `halted.json` strict read."""
    _, one_reads, _, two_reads = _land_at_n_and_2n(tmp_path)
    assert one_reads == two_reads
