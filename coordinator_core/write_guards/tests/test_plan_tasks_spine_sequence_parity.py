"""Parity tests for P084-C2 — both write guards' `_plan_tasks_spine_errors`
routed through the shared `plan_tasks_spine_errors` driver
(`coordinator_core.frontmatter.schema_validate`), with the advisory guard
gaining the `"ordering"` leg the deny guard deliberately still omits (14
in-corpus plans fail the ordering lint today — Anti-scope row 1,
docs/plans/2026-09-11-route-all-three-plan-tasks-spine-validat.md).

C3 (a later chunk) adds the desync-regression test proper (asserting each
site's declared `legs` + `legs_out_of_band` union against an explicit
expected map). This file, as delivered by C2, proves the BEHAVIOURAL split
the routing produces: a D5-violating spine warns through the advisory guard
and does not block through the deny guard; a clean spine is silent on both;
and a multi-defect spine's all-rows reporting is unchanged for the deny
guard and gains exactly one appended ordering row for the advisory guard.
"""

from __future__ import annotations

import pytest

from coordinator_core.testing.doe_root import doe_root_and_present
from coordinator_core.write_guards import validate_frontmatter_schema_advisory as advisory_guard
from coordinator_core.write_guards import validate_frontmatter_schema_deny as deny_guard

_doe_root, _doe_present = doe_root_and_present()

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _pin_doe_root(monkeypatch):
    if not _doe_present:
        pytest.skip("sibling DoE-claude checkout not found")
    monkeypatch.setattr(deny_guard, "coordinator_doe_root", lambda: _doe_root)
    monkeypatch.setattr(advisory_guard, "coordinator_doe_root", lambda: _doe_root)


_FRONTMATTER = (
    "---\ntitle: Test plan\ncreated: 2026-07-29\nauthor: test\nstatus: draft\n---\n\n"
    "# Plan\n\n## Tasks\n"
)

# ORDERING-violating: a `backlogged` (defer) row precedes an `open` (do)
# row. Legacy plan (no `grouping_approvals` key), so the grouping-approval
# leg has nothing to say about this source — the violation is ordering
# only. Byte-for-byte the same fixture shape as
# coordinator_core/frontmatter/tests/test_plan_tasks_spine_sequence.py's
# `_ORDERING_VIOLATING`.
_ORDERING_VIOLATING = (
    "```yaml plan-tasks\n"
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
    "```\n"
)

_CLEAN = (
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: Do a thing\n"
    "  change_kind: script-edit\n"
    "  surface: coordinator/bin/foo\n"
    "```\n"
)

# MULTI-DEFECT, ordering-VALID: two per-row shape defects (bad change_kind,
# bad disposition), no ordering violation — isolates the per-row leg's
# all-rows accumulation from the ordering leg the two guards now disagree
# on declaring.
_MULTI_DEFECT_ORDERING_VALID = (
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: bad change_kind\n"
    "  change_kind: script-port\n"
    "  surface: coordinator/bin/foo\n"
    "- id: C2\n"
    "  title: bad disposition\n"
    "  change_kind: script-edit\n"
    "  surface: coordinator/bin/bar\n"
    "  disposition: done\n"
    "```\n"
)


def _plan_path(tmp_path):
    d = tmp_path / "docs" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d / "2026-07-29-test-plan.md"


def _payload(file_path, cwd, old_string, new_string):
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "old_string": old_string, "new_string": new_string},
        "cwd": cwd,
    }


def _write_and_check(guard, tmp_path, tasks_block):
    fp = _plan_path(tmp_path)
    old_content = _FRONTMATTER
    fp.write_text(old_content, encoding="utf-8")
    new_content = _FRONTMATTER + tasks_block
    return guard.check(_payload(str(fp), str(tmp_path), old_content, new_content))


class TestOrderingLegSplitByGuard:
    """The D5 gap the baton names: an ordering-violating spine now warns
    through the advisory guard (the leg it gains here) and stays silent —
    NOT a block — through the deny guard, which deliberately still omits
    `"ordering"` (Anti-scope row 1)."""

    def test_advisory_warns_on_ordering_violation(self, tmp_path):
        result = _write_and_check(advisory_guard, tmp_path, _ORDERING_VIOLATING)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        text = hso["additionalContext"]
        assert "plan-tasks" in text
        assert "disposition" in text and "backlogged" in text

    def test_deny_does_not_block_on_ordering_violation(self, tmp_path):
        # STRICT mode is the deny sibling's own upgraded-to-render mode for
        # this leg family; even there, the deny guard never runs "ordering"
        # (declared without it) so this source is silent through it.
        result = _write_and_check(deny_guard, tmp_path, _ORDERING_VIOLATING)
        assert result is None


class TestCleanSpineSilentOnBoth:
    def test_clean_spine_silent_advisory(self, tmp_path):
        assert _write_and_check(advisory_guard, tmp_path, _CLEAN) is None

    def test_clean_spine_silent_deny(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        assert _write_and_check(deny_guard, tmp_path, _CLEAN) is None


class TestMultiDefectAllRowsAccumulationUnchanged:
    """Ordering-VALID multi-defect spine: the DENY guard's all-rows finding
    set is unchanged by the routing (it never declared "ordering"); the
    ADVISORY guard's finding set is likewise unchanged here because this
    fixture carries no ordering violation for the newly-declared leg to
    find — proving the routing adds a row only when a violation is
    actually present, not on every multi-defect spine."""

    def test_deny_reports_both_rows_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        result = _write_and_check(deny_guard, tmp_path, _MULTI_DEFECT_ORDERING_VALID)
        assert result is not None
        hso = result["hookSpecificOutput"]
        # Schema-shape findings are always-advisory as of the 2026-08-06
        # ruling — rendered by THIS module only under STRICT, but still as
        # an `additionalContext` warning, never a `permissionDecision`.
        assert "permissionDecision" not in hso
        text = hso["additionalContext"]
        assert "tasks[C1].change_kind" in text
        assert "tasks[C2].disposition" in text
        assert "plan-tasks" not in text.replace("tasks[C1]", "").replace("tasks[C2]", "")

    def test_advisory_reports_both_rows_no_ordering_row(self, tmp_path):
        result = _write_and_check(advisory_guard, tmp_path, _MULTI_DEFECT_ORDERING_VALID)
        assert result is not None
        hso = result["hookSpecificOutput"]
        text = hso["additionalContext"]
        assert "tasks[C1].change_kind" in text
        assert "tasks[C2].disposition" in text
        assert "plan-tasks" not in text.replace("tasks[C1]", "").replace("tasks[C2]", "")


class TestSpineLegDeclarationDesyncRegression:
    """P084-C3 — the regression test the baton asks for: it fails if any
    single site (the two write guards' `_plan_tasks_spine_errors` helpers,
    and `check_plan_tasks_source`) stops running a check the others run.

    Asserts the UNION of each site's `legs` and `legs_out_of_band` — never
    `legs` alone, because the write guards' grouping-approval leg runs
    OUT-OF-BAND (at `_evaluate_grouping_approval` / `_grouping_approval_fires`,
    not through the driver); an assertion over `legs` alone would pin the
    false claim that they do not run it at all.

    Also asserts every declared name (both halves, every site) is a member
    of `PLAN_TASKS_SPINE_SEQUENCE`, so a site cannot invent a leg the
    definition does not know.

    The deny guard's "ordering" omission and `check_plan_tasks_source`'s
    "integrity" omission are asserted here AS expected exclusions, with
    their reasons named beside them — so flipping either is a one-line test
    edit beside a one-value code edit, never a silent desync.
    """

    # name -> (legs, legs_out_of_band) this test EXPECTS that site to
    # declare. `test_actual_declarations_match_expected` below captures what
    # each site ACTUALLY passes to the driver at call time (never read off
    # source text) and compares against this map — so a change at any of
    # the three call sites without a matching edit here fails loud.
    _EXPECTED = {
        "deny guard (_plan_tasks_spine_errors)": (
            ("integrity", "per_row"),
            ("grouping_approval",),
        ),
        "advisory guard (_plan_tasks_spine_errors)": (
            ("integrity", "ordering", "per_row"),
            ("grouping_approval",),
        ),
        "check_plan_tasks_source": (
            ("ordering", "grouping_approval", "per_row"),
            (),
        ),
    }

    def _capture_driver_call(self, monkeypatch, module, real_driver):
        captured = {}

        def _spy(*args, **kwargs):
            captured["legs"] = tuple(kwargs.get("legs", ()))
            captured["legs_out_of_band"] = tuple(kwargs.get("legs_out_of_band", ()))
            return real_driver(*args, **kwargs)

        monkeypatch.setattr(module, "_plan_tasks_spine_errors_driver", _spy, raising=True)
        return captured

    def test_deny_guard_actual_declaration_matches_expected(self, tmp_path, monkeypatch):
        from coordinator_core.frontmatter.schema_validate import plan_tasks_spine_errors as real

        captured = self._capture_driver_call(monkeypatch, deny_guard, real)
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        _write_and_check(deny_guard, tmp_path, _CLEAN)
        assert captured, "deny guard never reached the driver — fixture stopped short"
        expected_legs, expected_out_of_band = self._EXPECTED[
            "deny guard (_plan_tasks_spine_errors)"
        ]
        assert captured["legs"] == expected_legs
        assert captured["legs_out_of_band"] == expected_out_of_band

    def test_advisory_guard_actual_declaration_matches_expected(self, tmp_path, monkeypatch):
        from coordinator_core.frontmatter.schema_validate import plan_tasks_spine_errors as real

        captured = self._capture_driver_call(monkeypatch, advisory_guard, real)
        _write_and_check(advisory_guard, tmp_path, _CLEAN)
        assert captured, "advisory guard never reached the driver — fixture stopped short"
        expected_legs, expected_out_of_band = self._EXPECTED[
            "advisory guard (_plan_tasks_spine_errors)"
        ]
        assert captured["legs"] == expected_legs
        assert captured["legs_out_of_band"] == expected_out_of_band

    def test_check_plan_tasks_source_actual_declaration_matches_expected(self, monkeypatch):
        import coordinator_core.frontmatter.schema_validate as sv

        real = sv.plan_tasks_spine_errors
        captured = {}

        def _spy(*args, **kwargs):
            captured["legs"] = tuple(kwargs.get("legs", ()))
            captured["legs_out_of_band"] = tuple(kwargs.get("legs_out_of_band", ()))
            return real(*args, **kwargs)

        monkeypatch.setattr(sv, "plan_tasks_spine_errors", _spy, raising=True)
        sv.check_plan_tasks_source(_FRONTMATTER + "```yaml plan-tasks\n" + _CLEAN + "```\n")
        assert captured, "check_plan_tasks_source never reached the driver"
        expected_legs, expected_out_of_band = self._EXPECTED["check_plan_tasks_source"]
        assert captured["legs"] == expected_legs
        assert captured["legs_out_of_band"] == expected_out_of_band

    def test_deny_guard_omits_ordering_deliberately(self):
        """Anti-scope row 1: 14 in-corpus plans fail the ordering lint today;
        a deny-side leg would make those unwritable on next edit — a
        PM-gated follow-up, not this change."""
        legs, out_of_band = self._EXPECTED["deny guard (_plan_tasks_spine_errors)"]
        assert "ordering" not in legs
        assert "ordering" not in out_of_band

    def test_check_plan_tasks_source_omits_integrity_deliberately(self):
        """check_plan_tasks_source reaches rows through the lenient
        `_plan_tasks_spine_rows` fallback (silent None on a malformed fence
        or unparseable block); adding "integrity" would make it newly
        return an error on sources it passes today, and
        `spine_read.read_spine` consults it as an order-sensitive single-error
        preflight — a behavioural change to a live emitter path, not a
        consolidation."""
        legs, out_of_band = self._EXPECTED["check_plan_tasks_source"]
        assert "integrity" not in legs
        assert "integrity" not in out_of_band

    @pytest.mark.parametrize("site_name", list(_EXPECTED))
    def test_declared_names_are_known_legs(self, site_name):
        from coordinator_core.frontmatter.schema_validate import PLAN_TASKS_SPINE_SEQUENCE

        known = frozenset(name for name, _ in PLAN_TASKS_SPINE_SEQUENCE)
        legs, out_of_band = self._EXPECTED[site_name]
        for name in (*legs, *out_of_band):
            assert name in known, f"{site_name} declares unknown leg {name!r}"
