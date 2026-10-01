"""Tests for `PLAN_TASKS_SPINE_SEQUENCE` / `plan_tasks_spine_errors` — the
shared leg-sequencing definition P084-C1 introduces
(docs/plans/2026-09-11-route-all-three-plan-tasks-spine-validat.md, C1).

Behavioural budget under test: `check_plan_tasks_source`, rewired onto the
driver, returns the byte-identical `ErrorDict | None` it returned before
this change for every source — that is the acceptance test, not a hope.
"""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter.schema_validate import (
    PLAN_TASKS_SPINE_SEQUENCE,
    _PLAN_TASKS_SCHEMA_DICT,
    check_plan_tasks_source,
    plan_tasks_spine_errors,
)


def _plan(tasks_yaml: str, *, frontmatter: str | None = None, prose: str = '') -> str:
    head = f"---\n{frontmatter}\n---\n" if frontmatter is not None else ''
    return (
        f"{head}"
        "# A plan\n\n"
        f"{prose}"
        "## Tasks\n\n"
        "```yaml plan-tasks\n"
        f"{tasks_yaml}\n"
        "```\n"
    )


_CLEAN = (
    "- id: C1\n"
    "  title: live\n"
    "  change_kind: code-edit\n"
    "  surface: some/path.py\n"
    "  writes: []\n"
    "  disposition: open\n"
)

# ORDERING-violating: a `backlogged` (defer) row precedes an `open` (do) row.
# Legacy plan, so the grouping-approval leg is silent (no gated disposition
# scan on a legacy plan) and the violation this source carries is ordering
# ONLY — isolating which leg's error surfaces first.
_ORDERING_VIOLATING = (
    "- id: C1\n"
    "  title: deferred first\n"
    "  change_kind: code-edit\n"
    "  surface: some/path.py\n"
    "  writes: []\n"
    "  disposition: backlogged\n"
    "  case_against: not worth it\n"
    "  pm_approved: true\n"
    "- id: C2\n"
    "  title: live second\n"
    "  change_kind: code-edit\n"
    "  surface: some/other.py\n"
    "  writes: []\n"
    "  disposition: open\n"
)

# GROUPING-violating: ordering-VALID (open before backlogged), on a GOVERNED
# plan (bare `grouping_approvals` key, per `is_governed_plan`) whose `defer`
# block is absent/pending — the grouping-approval leg refuses this, and the
# ordering leg has nothing to say about it.
_GROUPING_VIOLATING = (
    "- id: C1\n"
    "  title: live\n"
    "  change_kind: code-edit\n"
    "  surface: some/path.py\n"
    "  writes: []\n"
    "  disposition: open\n"
    "- id: C2\n"
    "  title: deferred, ungoverned approval\n"
    "  change_kind: code-edit\n"
    "  surface: some/other.py\n"
    "  writes: []\n"
    "  disposition: backlogged\n"
    "  case_against: not worth it\n"
)
_GOVERNED_FRONTMATTER = "grouping_approvals: {}"

_BAD_ROW = (
    "- id: C1\n"
    "  title: missing change_kind\n"
    "  surface: some/path.py\n"
    "  writes: []\n"
    "  disposition: open\n"
)

# MALFORMED-fence: no `## Tasks` heading at all, so `locate_fenced_block`
# cannot locate the block under it.
_MALFORMED_FENCE_SOURCE = (
    "# A plan\n\n"
    "## Not Tasks\n\n"
    "```yaml plan-tasks\n"
    f"{_CLEAN}\n"
    "```\n"
)

# UNPARSEABLE-block: located under `## Tasks`, but not valid YAML.
_UNPARSEABLE_BLOCK_SOURCE = (
    "# A plan\n\n"
    "## Tasks\n\n"
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: [unterminated\n"
    "```\n"
)


class TestSequenceOrder:
    def test_sequence_names_and_kinds(self):
        names = [name for name, _kind in PLAN_TASKS_SPINE_SEQUENCE]
        assert names == ["integrity", "ordering", "grouping_approval", "per_row"]
        kinds = {kind for _name, kind in PLAN_TASKS_SPINE_SEQUENCE}
        assert kinds == {"whole_source", "per_row"}

    def test_driver_runs_legs_in_sequence_order_not_param_order(self):
        """`legs` declares `per_row` before `ordering`; the driver still
        runs ordering first (per `PLAN_TASKS_SPINE_SEQUENCE`'s order), so
        the ordering error is `errors[0]`.
        """
        source = _plan(_ORDERING_VIOLATING)
        errors, rows = plan_tasks_spine_errors(
            source, None,
            plan_tasks_schema=_PLAN_TASKS_SCHEMA_DICT,
            legs=("per_row", "grouping_approval", "ordering"),
        )
        assert rows is not None
        assert errors
        assert errors[0]["field"] == "plan-tasks"
        assert "D5" in errors[0]["error"]

    def test_unknown_leg_name_raises(self):
        source = _plan(_CLEAN)
        with pytest.raises(ValueError):
            plan_tasks_spine_errors(
                source, None,
                plan_tasks_schema=_PLAN_TASKS_SCHEMA_DICT,
                legs=("not_a_real_leg",),
            )

    def test_unknown_out_of_band_leg_name_also_raises(self):
        source = _plan(_CLEAN)
        with pytest.raises(ValueError):
            plan_tasks_spine_errors(
                source, None,
                plan_tasks_schema=_PLAN_TASKS_SCHEMA_DICT,
                legs=("ordering",),
                legs_out_of_band=("not_a_real_leg",),
            )

    def test_declared_subset_skips_the_omitted_leg(self):
        source = _plan(_ORDERING_VIOLATING)
        errors, rows = plan_tasks_spine_errors(
            source, None,
            plan_tasks_schema=_PLAN_TASKS_SCHEMA_DICT,
            legs=("per_row",),
        )
        assert rows is not None
        assert all("D5" not in e["error"] for e in errors)


class TestCheckPlanTasksSourceRewiring:

    def test_ordering_violation_is_first_error(self):
        error = check_plan_tasks_source(_plan(_ORDERING_VIOLATING))
        assert error is not None
        assert error["field"] == "plan-tasks"

    def test_grouping_violation_is_first_error(self):
        source = _plan(_GROUPING_VIOLATING, frontmatter=_GOVERNED_FRONTMATTER)
        error = check_plan_tasks_source(source)
        assert error is not None
        assert error["field"] == "grouping_approvals.defer"

    def test_bad_row_is_first_error(self):
        error = check_plan_tasks_source(_plan(_BAD_ROW))
        assert error is not None
        assert error["field"] == "change_kind"

    def test_clean_source_is_none(self):
        assert check_plan_tasks_source(_plan(_CLEAN)) is None

    def test_malformed_fence_still_returns_none(self):
        assert check_plan_tasks_source(_MALFORMED_FENCE_SOURCE) is None

    def test_unparseable_block_still_returns_none(self):
        assert check_plan_tasks_source(_UNPARSEABLE_BLOCK_SOURCE) is None


class TestRowLabelFmt:
    def test_none_gives_bare_field(self):
        errors, _rows = plan_tasks_spine_errors(
            _plan(_BAD_ROW), None,
            plan_tasks_schema=_PLAN_TASKS_SCHEMA_DICT,
            legs=("per_row",),
        )
        assert errors
        assert errors[0]["field"] == "change_kind"

    def test_guard_format_carries_the_row_prefix(self):
        errors, _rows = plan_tasks_spine_errors(
            _plan(_BAD_ROW), None,
            plan_tasks_schema=_PLAN_TASKS_SCHEMA_DICT,
            legs=("per_row",),
            row_label_fmt="tasks[{id}].{field}",
        )
        assert errors
        assert errors[0]["field"] == "tasks[C1].change_kind"


_ROW_MATRIX = [
    # open, clean
    {"id": "C1", "title": "t", "change_kind": "code-edit", "surface": "a.py",
     "writes": [], "disposition": "open"},
    # shape error (missing change_kind)
    {"id": "C1", "title": "t", "surface": "a.py", "writes": [], "disposition": "open"},
    # shape error (closed enum)
    {"id": "C1", "title": "t", "change_kind": "nope", "surface": "a.py",
     "writes": [], "disposition": "open"},
    # cross-field: non-open row with no disposition_detail
    {"id": "C1", "title": "t", "change_kind": "code-edit", "surface": "a.py",
     "writes": [], "disposition": "coded"},
    # closed disposition, pm_approved absent (schema branch vs governed)
    {"id": "C1", "title": "t", "change_kind": "code-edit", "surface": "a.py",
     "writes": [], "disposition": "spun_off", "disposition_detail": "elsewhere"},
    # closed disposition, pm_approved present
    {"id": "C1", "title": "t", "change_kind": "code-edit", "surface": "a.py",
     "writes": [], "disposition": "spun_off", "disposition_detail": "elsewhere",
     "pm_approved": True},
    # open row without writes (cross-field gated on plan_created)
    {"id": "C1", "title": "t", "change_kind": "code-edit", "surface": "a.py",
     "disposition": "open"},
]


def _strip_label(field: str) -> str:
    prefix = "tasks[C1]."
    return field[len(prefix):] if field.startswith(prefix) else field


def _canon(errors):
    return [{**e, "field": _strip_label(e["field"])} for e in errors]


class TestPerRowCopiesAgree:
    """The per-row shape-then-cross-field sequence is run by the spine
    driver (check_plan_tasks_source + both write guards) and by
    plan_tasks_mutate._validate_row. One fixture matrix, every copy, same
    errors in the same order."""

    @pytest.mark.parametrize("row", _ROW_MATRIX)
    @pytest.mark.parametrize("created", ["2026-01-01", "2099-01-01"])
    @pytest.mark.parametrize("governed", [False, True])
    def test_mutate_and_guards_agree_with_driver(self, row, created, governed):
        import yaml

        from coordinator_core.frontmatter.schema_validate import (
            _PLAN_TASKS_SCHEMA_DICT as base,
        )
        from coordinator_core.ops.plan_tasks_mutate import _validate_row
        from coordinator_core.write_guards import (
            validate_frontmatter_schema_advisory as advisory,
        )
        from coordinator_core.write_guards import (
            validate_frontmatter_schema_deny as deny,
        )

        fm = {"created": created}
        if governed:
            fm["grouping_approvals"] = {}
        fm_text = yaml.safe_dump(fm)
        source = _plan(yaml.safe_dump([row]), frontmatter=fm_text.rstrip("\n"))

        driver_errors, _rows = plan_tasks_spine_errors(
            source, fm, plan_tasks_schema=base, legs=("per_row",),
        )
        mutate_errors = _validate_row(row, governed=governed, plan_created=created)
        assert _canon(driver_errors) == mutate_errors

        schemas = {"plan-tasks": base}
        assert _canon(deny._plan_tasks_spine_errors(source, schemas, fm)) == mutate_errors
        advisory_errors = [
            e for e in advisory._plan_tasks_spine_errors(source, schemas, fm)
            if "D5" not in e.get("error", "")
        ]
        assert _canon(advisory_errors) == mutate_errors
