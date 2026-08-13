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
    load_manifest,
    measure_import_subprocess,
    resolve_ceiling,
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
    assert cost.module_count <= ceiling, (
        f"{entrypoint} import pulled in {cost.module_count} modules, exceeding the "
        f"regrowth-tripwire ceiling of {ceiling} (elapsed {cost.elapsed_ms:.2f}ms). "
        "This ceiling carries deliberate headroom over the recorded baseline (see "
        "import-budget-manifest.json) -- a failure here means a real import-cost regrowth, "
        "not a tight-pin false positive. Re-baseline the manifest if the growth is expected "
        "and reviewed, do not just widen the ceiling to make this pass."
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
