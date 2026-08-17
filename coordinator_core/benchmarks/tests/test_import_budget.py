"""Unit + regrowth-tripwire tests for coordinator_core.benchmarks.import_budget.

Covers chunk C4's AC9/AC11: a committed test asserting a MODULE-COUNT CEILING with stated
headroom for each named hot-path entrypoint (`coordinator_core.hooks`,
`coordinator_core.write_guards.engine`, `coordinator_core.ipc`), gated on `len(sys.modules)`
delta (deterministic) rather than wall-clock (flaky under parallel test load) -- see
`import_budget.py`'s module docstring for the full rationale and the schema-vs-sibling decision.

Spec backlink: pln-windows-hot-path-cost-less-wor-0ec8ea chunk C4
(AC9, AC11).
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.benchmarks.import_budget import (
    ImportCost,
    interpreter_drift_note,
    load_manifest,
    measure_import_subprocess,
    resolve_baseline_python,
    resolve_ceiling,
    running_python_version,
)

_ENTRYPOINTS = [
    "coordinator_core.hooks",
    "coordinator_core.write_guards.engine",
    "coordinator_core.ipc",
]


def test_manifest_carries_entry_for_every_minimum_entrypoint():
    manifest = load_manifest()
    for entrypoint in _ENTRYPOINTS:
        assert entrypoint in manifest["entrypoints"], (
            f"import-budget-manifest.json is missing a slot for {entrypoint!r}"
        )


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_every_entrypoint_records_the_interpreter_its_baseline_was_measured_under(entrypoint):
    """A module-count baseline is only meaningful against a named interpreter.

    2026-08-17: `coordinator_core.ipc` breached its ceiling with no code change,
    because Python 3.14 routed `bz2`/`lzma` onto the new `compression` package that
    `shutil` <- `tempfile` reaches. The manifest recorded `measured_on` (the OS) but
    not the interpreter, so nothing on disk could distinguish that from real
    regrowth, and the breach message asserted regrowth outright. This test stops a
    future re-baseline from dropping the field back off.
    """
    recorded = resolve_baseline_python(entrypoint)
    assert recorded, (
        f"import-budget entry for {entrypoint!r} records no 'measured_under_python'. "
        f"Re-measure under a known interpreter and record it (this box: "
        f"{running_python_version()}) — a bare module count cannot be compared across "
        f"interpreter versions."
    )


def test_drift_note_distinguishes_matched_from_mismatched_interpreter():
    """The clause must name interpreter drift as a candidate cause when the versions
    differ, and rule it out when they match — never assert regrowth blindly."""
    manifest = {
        "schema_version": 1,
        "entrypoints": {
            "matched": {"module_count_ceiling": 10, "measured_under_python": running_python_version()},
            "mismatched": {"module_count_ceiling": 10, "measured_under_python": "3.11.0"},
            "unrecorded": {"module_count_ceiling": 10},
        },
    }
    matched = interpreter_drift_note("matched", manifest=manifest)
    assert "ruled out" in matched and "real regrowth" in matched

    mismatched = interpreter_drift_note("mismatched", manifest=manifest)
    assert "3.11.0" in mismatched and running_python_version() in mismatched
    assert "own_module_count" in mismatched, (
        "a mismatched-interpreter breach must point the reader at the own-vs-stdlib "
        "split, which is the measurement that actually discriminates the two causes"
    )

    unrecorded = interpreter_drift_note("unrecorded", manifest=manifest)
    assert "cannot be ruled out" in unrecorded


def test_probe_reports_own_module_share():
    """The probe's third field is what stops the next investigator re-deriving the
    own-vs-stdlib split with a throwaway script."""
    cost = measure_import_subprocess("coordinator_core.ipc")
    assert cost.own_module_count is not None
    assert 0 < cost.own_module_count <= cost.module_count


def test_resolve_ceiling_raises_for_unknown_entrypoint():
    manifest = {"schema_version": 1, "entrypoints": {}}
    with pytest.raises(KeyError):
        resolve_ceiling("not.a.real.entrypoint", manifest=manifest)


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_module_count_stays_under_ceiling(entrypoint):
    """AC9/AC11: the primary assertion is module count, not wall-clock -- see import_budget.py's
    module docstring for why. `measure_import_subprocess` runs in a fresh interpreter so the
    `sys.modules` delta is trustworthy (not undercounted by this test process's own prior
    imports). Reports the elapsed wall-clock as informational only; never asserted."""
    cost: ImportCost = measure_import_subprocess(entrypoint)
    ceiling = resolve_ceiling(entrypoint)
    own = "unknown" if cost.own_module_count is None else str(cost.own_module_count)
    stdlib = (
        "unknown"
        if cost.own_module_count is None
        else str(cost.module_count - cost.own_module_count)
    )
    assert cost.module_count <= ceiling, (
        f"{entrypoint} import pulled in {cost.module_count} modules ({own} "
        f"coordinator_core.*, {stdlib} stdlib/third-party), exceeding the "
        f"regrowth-tripwire ceiling of {ceiling} (elapsed {cost.elapsed_ms:.2f}ms). "
        "This ceiling carries deliberate headroom over the recorded baseline (see "
        "import-budget-manifest.json)." + interpreter_drift_note(entrypoint) + " Either way "
        "the fix is to defer or drop an import, not to widen the ceiling to make this pass; "
        "re-baseline only when the growth is expected AND reviewed."
    )


def test_resolve_ceiling_raises_for_entry_missing_ceiling_field():
    manifest = {
        "schema_version": 1,
        "entrypoints": {"some.module": {"baseline_module_count": 10, "headroom": 2}},
    }
    with pytest.raises(ValueError):
        resolve_ceiling("some.module", manifest=manifest)


def test_measure_import_subprocess_ignores_ambient_lazy_ops_override(monkeypatch):
    """Env-purity regression: `COORDINATOR_CORE_LAZY_OPS=1` in the PARENT (this test's own)
    environment must not leak into the probe subprocess and switch it onto the lazy path.
    `coordinator_core.hooks` measures ~5 modules lazy vs ~111+ eager -- against the fix, this
    subprocess would inherit the ambient var and report a lazy-sized count; against the
    unfixed code (env = dict(os.environ) with no pop), this assertion fails because the
    measured count collapses to the lazy ~5-module figure."""
    monkeypatch.setenv("COORDINATOR_CORE_LAZY_OPS", "1")
    cost = measure_import_subprocess("coordinator_core.hooks")
    assert cost.module_count > 50, (
        f"expected an eager-path module count (~111+), got {cost.module_count} -- the ambient "
        "COORDINATOR_CORE_LAZY_OPS=1 override leaked into the probe subprocess and switched it "
        "onto the lazy path (~5 modules)"
    )


def test_manifest_ceiling_exceeds_recorded_baseline_by_stated_headroom():
    """The ceiling isn't a magic number -- it must equal baseline + headroom, both recorded in
    the manifest, so the headroom choice is auditable rather than hand-tuned per entry."""
    manifest = load_manifest()
    for entrypoint, entry in manifest["entrypoints"].items():
        expected_ceiling = entry["baseline_module_count"] + entry["headroom"]
        assert entry["module_count_ceiling"] == expected_ceiling, (
            f"{entrypoint}: module_count_ceiling ({entry['module_count_ceiling']}) does not "
            f"equal baseline_module_count + headroom ({expected_ceiling})"
        )


def test_manifest_is_valid_json_on_disk():
    from coordinator_core.benchmarks.import_budget import _MANIFEST_PATH

    with open(_MANIFEST_PATH, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest["schema_version"] == 1
