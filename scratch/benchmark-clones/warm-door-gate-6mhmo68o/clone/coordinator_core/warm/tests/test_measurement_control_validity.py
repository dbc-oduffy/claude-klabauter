"""M6 control-validity check: a measurement control arm must be structurally
distinguishable from the treatment arm.

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
§ Hard constraints (M6), § C9.

WHAT THIS PINS. Staff-data-sci review M6's executable form: every
measurement AC's control arm must report `len(sys.modules)`, its
`coordinator_core.*` subset, and spawn count for BOTH arms, and a control
structurally indistinguishable from the treatment is a FAILED control --
the measurement it backs is void. The incident this guards against is
recorded in P8: a `COORDINATOR_WARM=0` control "paid the same import tax as
the treatment until `d450109ab`" -- i.e. it LOOKED like a control (a
different env var) while being structurally identical to the arm it was
supposed to isolate against.

No `coordinator_core.warm` measurement-reporting helper exists in this tree
yet (see `test_measurement_helper_names_clone.py`'s own module docstring for
why) -- this file pins the CONTRACT such a helper's control-validity check
must satisfy, importable by that future code rather than re-derived.

NEGATIVE-SPEC:
    - Does NOT validate any REAL measurement artifact on disk -- pins the
      reusable validator and proves it rejects the exact shape M6 names.
    - Does NOT judge whether the treatment arm's OWN numbers are correct --
      only whether control and treatment are structurally distinguishable
      from each other on the three named axes.
"""

from __future__ import annotations

import pytest


def assert_control_is_structurally_distinct(control: dict, treatment: dict) -> None:
    """Raise `ValueError` if `control` is structurally indistinguishable
    from `treatment` on all three of M6's named axes: total module count,
    `coordinator_core.*` module count, and spawn count.

    Each arm dict is expected to carry `module_count`, `coordinator_core_module_count`,
    and `spawn_count`. A control that matches the treatment on ALL THREE is
    the exact "paid the same import tax as the treatment" failure P8
    records -- a control differing on even one axis is doing SOME isolating
    work and is accepted here; judging whether that one axis is the right
    one is a human review call, not this guard's job.
    """
    axes = ("module_count", "coordinator_core_module_count", "spawn_count")
    missing = [axis for axis in axes if axis not in control or axis not in treatment]
    if missing:
        raise ValueError(
            f"control-validity check needs all three M6 axes on both arms; missing {missing}"
        )
    if all(control[axis] == treatment[axis] for axis in axes):
        raise ValueError(
            f"control arm {control!r} is structurally indistinguishable from "
            f"treatment arm {treatment!r} on all three M6 axes (module_count, "
            "coordinator_core_module_count, spawn_count) -- this is a FAILED "
            "control and the measurement it backs is void. See P8: a "
            "COORDINATOR_WARM=0 control paid the same import tax as the "
            "treatment until d450109ab."
        )


def test_a_control_identical_to_treatment_on_all_three_axes_is_rejected():
    treatment = {"module_count": 527, "coordinator_core_module_count": 316, "spawn_count": 1}
    control = {"module_count": 527, "coordinator_core_module_count": 316, "spawn_count": 1}
    with pytest.raises(ValueError, match="structurally indistinguishable"):
        assert_control_is_structurally_distinct(control, treatment)


def test_a_control_differing_on_module_count_is_accepted():
    treatment = {"module_count": 527, "coordinator_core_module_count": 316, "spawn_count": 1}
    control = {"module_count": 4, "coordinator_core_module_count": 316, "spawn_count": 1}
    assert_control_is_structurally_distinct(control, treatment) is None


def test_a_control_missing_an_axis_is_rejected_rather_than_silently_skipped():
    treatment = {"module_count": 527, "coordinator_core_module_count": 316, "spawn_count": 1}
    control = {"module_count": 4, "coordinator_core_module_count": 316}
    with pytest.raises(ValueError, match="missing"):
        assert_control_is_structurally_distinct(control, treatment)
