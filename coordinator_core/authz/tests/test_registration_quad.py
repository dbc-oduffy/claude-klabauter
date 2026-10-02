"""
coordinator_core.authz.tests.test_registration_quad — tests for the five-surface
op-registration completeness gate (public symbol still `check_registration_quad`).

Purpose: proves `check_registration_quad()` actually fires on an incomplete
registration (a planted violation, not merely a green-on-current-tree assertion —
DEC-4, docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md:272)
and that the known-debt baseline `_KNOWN_UNCLASSIFIED_OPS_DEBT` can only shrink,
never grow, without a plan amendment (AC5). `TestEagerOpModulesSurface` covers the
fifth surface, `_EAGER_OP_MODULES` (`coordinator_core/ops/__init__.py`), added to close
the gap `roadmap.link_stubs` demonstrated live on 2026-08-05 — see
`registration_quad.py`'s module docstring § Fifth surface.

Spec backlink: pln-registration-quad-completeness-bf0d39 § C3,
AC5, AC6, AC7
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md
"""

from __future__ import annotations

from coordinator_core.authz.registration_quad import (
    _KNOWN_INCOMPLETE_REGISTRATIONS,
    _KNOWN_UNCLASSIFIED_OPS_DEBT,
    QuadViolation,
    check_registration_quad,
    filter_known_violations,
    partition_suspended,
    prune_known_incomplete,
)


def _qv(op_key: str) -> QuadViolation:
    return QuadViolation(
        op_key=op_key,
        surfaces_present=(),
        surfaces_missing=("OP_CLASSIFICATION",),
        missing_surface_files=(("OP_CLASSIFICATION", "coordinator_core/authz/classification.py"),),
    )


class TestSuspendedPartition:

    def test_partition_splits_by_op_key_preserving_order(self) -> None:
        a, b, c, d = _qv("a.op"), _qv("b.op"), _qv("c.op"), _qv("d.op")
        live, by_design = partition_suspended([a, b, c, d], {"b.op", "d.op"})
        assert live == [a, c]
        assert by_design == [b, d]

    def test_filter_default_resolves_live_suspended_ops(self, monkeypatch) -> None:
        from coordinator_core import op_budget_suspension

        monkeypatch.setattr(op_budget_suspension, "SUSPENDED_OPS", {"susp.op": {}})
        raw = [_qv("susp.op"), _qv("other.op")]
        assert filter_known_violations(raw) == [raw[1]]
        assert filter_known_violations(raw, suspended=frozenset()) == raw


class TestGateDetectsPlantedViolation:

    def test_gate_detects_a_planted_violation(self) -> None:
        registry = {"planted.op": object(), "complete.op": object()}
        classification = {"complete.op": "COMPUTE_ONLY"}
        scope = {"planted.op": "common", "complete.op": "common"}
        module_map = {"planted.op": "coordinator_core.ops.planted", "complete.op": "coordinator_core.ops.complete"}
        eager_modules = {"coordinator_core.ops.planted", "coordinator_core.ops.complete"}

        violations = check_registration_quad(
            registry=registry,
            classification=classification,
            scope=scope,
            module_map=module_map,
            eager_modules=eager_modules,
        )

        assert len(violations) == 1
        violation = violations[0]
        assert violation.op_key == "planted.op"
        # Pin the exact surfaces_missing shape, not just
        # membership, so a regression that also spuriously reports _OP_KEY_SCOPE or
        # OP_MODULE_MAP missing for planted.op (both of which the fixture supplies)
        # would fail this test instead of passing it.
        assert violation.surfaces_missing == ("OP_CLASSIFICATION",)
        assert dict(violation.missing_surface_files)["OP_CLASSIFICATION"] == (
            "coordinator_core/authz/classification.py"
        )


class TestGateVacuityGuards:

    def test_clean_fixture_returns_no_violations(self) -> None:
        registry = {"complete.op": object()}
        classification = {"complete.op": "COMPUTE_ONLY"}
        scope = {"complete.op": "common"}
        module_map = {"complete.op": "coordinator_core.ops.complete"}
        eager_modules = {"coordinator_core.ops.complete"}

        violations = check_registration_quad(
            registry=registry,
            classification=classification,
            scope=scope,
            module_map=module_map,
            eager_modules=eager_modules,
        )

        assert violations == []

    def test_empty_registry_returns_no_violations_by_contract(self) -> None:
        violations = check_registration_quad(
            registry={},
            classification={"stale.op": "COMPUTE_ONLY"},
            scope={},
            module_map={},
        )

        assert violations == []


class TestEagerOpModulesSurface:
    """Fifth surface: `_EAGER_OP_MODULES` membership of an op's `OP_MODULE_MAP` module
    path, resolved via injected `eager_modules` fixtures (no live repo needed — AC3).
    Covers the exact gap `roadmap.link_stubs` demonstrated live on 2026-08-05: an op
    correct on all four prior surfaces but absent from the eager-import list registers
    only under whichever import order happens to pull its module in.
    """

    def test_op_missing_from_eager_modules_is_reported(self) -> None:
        registry = {"complete.op": object()}
        classification = {"complete.op": "COMPUTE_ONLY"}
        scope = {"complete.op": "common"}
        module_map = {"complete.op": "coordinator_core.ops.complete"}
        eager_modules: set[str] = set()

        violations = check_registration_quad(
            registry=registry,
            classification=classification,
            scope=scope,
            module_map=module_map,
            eager_modules=eager_modules,
        )

        assert len(violations) == 1
        violation = violations[0]
        assert violation.op_key == "complete.op"
        assert violation.surfaces_missing == ("_EAGER_OP_MODULES",)
        assert dict(violation.missing_surface_files)["_EAGER_OP_MODULES"] == (
            "coordinator_core/ops/__init__.py"
        )

    def test_fully_registered_op_including_eager_is_not_reported(self) -> None:
        registry = {"complete.op": object()}
        classification = {"complete.op": "COMPUTE_ONLY"}
        scope = {"complete.op": "common"}
        module_map = {"complete.op": "coordinator_core.ops.complete"}
        eager_modules = {"coordinator_core.ops.complete"}

        violations = check_registration_quad(
            registry=registry,
            classification=classification,
            scope=scope,
            module_map=module_map,
            eager_modules=eager_modules,
        )

        assert violations == []

    def test_op_missing_module_map_is_not_double_reported_as_eager_miss(self) -> None:
        registry = {"orphan.op": object()}
        classification = {"orphan.op": "COMPUTE_ONLY"}
        scope = {"orphan.op": "common"}
        module_map: dict[str, str] = {}
        eager_modules: set[str] = set()

        violations = check_registration_quad(
            registry=registry,
            classification=classification,
            scope=scope,
            module_map=module_map,
            eager_modules=eager_modules,
        )

        assert len(violations) == 1
        violation = violations[0]
        assert violation.op_key == "orphan.op"
        assert violation.surfaces_missing == ("OP_MODULE_MAP",)


class TestUnclassifiedBaselineNeverGrows:

    def test_unclassified_baseline_never_grows(self) -> None:
        assert _KNOWN_UNCLASSIFIED_OPS_DEBT == frozenset()

        violations = check_registration_quad()
        # Ops already tracked by the fuller `_KNOWN_INCOMPLETE_REGISTRATIONS` ledger
        # (registration_quad.py, 2026-08-11) are excluded here — that ledger records
        # their exact missing-surface set (which may include OP_CLASSIFICATION
        # alongside _OP_KEY_SCOPE/OP_MODULE_MAP) with its own never-grows discipline
        # (`_KNOWN_INCOMPLETE_REGISTRATIONS` is a plain literal frozen at measurement
        # time, never appended to locally). Double-counting them here against a
        # narrower single-surface baseline that has no shape for their other missing
        # surfaces would either force growing THIS baseline (forbidden) or force
        # mis-tracking them as classification-only debt (inaccurate). See
        # `TestKnownIncompleteRegistrationsLedger` below for that ledger's own
        # never-grows coverage.
        live_unclassified = {
            violation.op_key
            for violation in violations
            if "OP_CLASSIFICATION" in violation.surfaces_missing
            and violation.op_key not in _KNOWN_INCOMPLETE_REGISTRATIONS
        }

        assert live_unclassified <= _KNOWN_UNCLASSIFIED_OPS_DEBT, (
            f"Ops missing OP_CLASSIFICATION but absent from the frozen baseline: "
            f"{live_unclassified - _KNOWN_UNCLASSIFIED_OPS_DEBT}. Adding to "
            "_KNOWN_UNCLASSIFIED_OPS_DEBT is a plan amendment, never a local executor "
            "call — classify the op in coordinator_core/authz/classification.py instead."
        )


# A separate test function, deliberately NOT
# folded into TestUnclassifiedBaselineNeverGrows.test_unclassified_baseline_never_grows
# above (which is currently RED on HEAD for an unrelated, pre-existing reason: three
# ops missing OP_CLASSIFICATION and absent from the frozen baseline). This assertion
# must be independent of that pre-existing failure so a live `_EAGER_OP_MODULES`
# regression of the `roadmap.link_stubs` shape (op fully complete on
# OP_CLASSIFICATION/_OP_KEY_SCOPE/OP_MODULE_MAP but absent from _EAGER_OP_MODULES) is
# caught even while the unrelated baseline test stays red. There is no frozen-baseline
# carve-out for this surface -- any live _EAGER_OP_MODULES-only violation is a bug,
# never tolerated debt.
class TestLiveTreeEagerModulesSurfaceNeverViolated:
    """`TestEagerOpModulesSurface` above only exercises the fifth surface against
    explicit `eager_modules=` fixtures -- never against the live tree. This closes
    that gap: a live op complete on the other four surfaces but missing from
    `_EAGER_OP_MODULES` must be visible to a test that resolves everything from the
    live in-process tables, the same way `check_registration_quad_completeness`'s
    stage-1.5 fast path in `commit_tripwires.py` now does."""

    def test_no_live_op_is_missing_only_the_eager_modules_surface(self) -> None:
        violations = check_registration_quad()
        live_eager_only_misses = {
            violation.op_key
            for violation in violations
            if violation.surfaces_missing == ("_EAGER_OP_MODULES",)
        }
        assert not live_eager_only_misses, (
            "Op(s) registered, classified, scoped, and module-mapped, but missing "
            f"from _EAGER_OP_MODULES: {live_eager_only_misses}. Add each op's owning "
            "module to coordinator_core/ops/__init__.py::_EAGER_OP_MODULES."
        )


class TestKnownIncompleteRegistrationsLedger:
    """`_KNOWN_INCOMPLETE_REGISTRATIONS` (frozen 2026-08-11 — see
    state/bug-backlog/2026-08-11-check-registration-quad-is-red-on-70-ops-0c14fa26f522.yaml)
    plus `_KNOWN_UNCLASSIFIED_OPS_DEBT`, combined via `filter_known_violations`, must
    make the live gate GREEN on today's known debt and RED on anything new — the whole
    point of freezing an allowlist instead of silently widening the check."""

    def test_live_tree_is_green_after_filtering_known_debt(self) -> None:
        violations = check_registration_quad()
        filtered = filter_known_violations(violations)
        assert filtered == [], (
            f"Non-allowlisted registration-quad violations on HEAD: "
            f"{[(v.op_key, v.surfaces_missing) for v in filtered]}"
        )

    def test_incomplete_registrations_ledger_stays_drained(self) -> None:
        assert _KNOWN_INCOMPLETE_REGISTRATIONS == {}

    def test_ledger_forgives_only_the_recorded_surface_not_the_whole_op(self) -> None:
        v = QuadViolation(
            op_key="distill.curate_clusters",
            surfaces_present=("OP_CLASSIFICATION", "_OP_KEY_SCOPE"),
            surfaces_missing=("OP_MODULE_MAP", "_EAGER_OP_MODULES"),
            missing_surface_files=(
                ("OP_MODULE_MAP", "coordinator_core/ops/_registry_map.py"),
                ("_EAGER_OP_MODULES", "coordinator_core/ops/__init__.py"),
            ),
        )
        # The baseline only records OP_MODULE_MAP for this op -- _EAGER_OP_MODULES is a
        # NEW, unrecorded gap and must survive pruning.
        pruned = prune_known_incomplete(v, {"distill.curate_clusters": ("OP_MODULE_MAP",)})
        assert pruned is not None
        assert pruned.surfaces_missing == ("_EAGER_OP_MODULES",)

    def test_control_a_planted_unallowlisted_op_still_trips_the_gate(self) -> None:
        v = QuadViolation(
            op_key="brand.new_unregistered_op",
            surfaces_present=(),
            surfaces_missing=("OP_CLASSIFICATION",),
            missing_surface_files=(("OP_CLASSIFICATION", "coordinator_core/authz/classification.py"),),
        )
        filtered = filter_known_violations([v])
        assert filtered == [v]

    def test_control_an_allowlisted_op_with_only_the_recorded_gap_is_dropped(self) -> None:
        v = QuadViolation(
            op_key="memo.fate_backfill",
            surfaces_present=("OP_CLASSIFICATION", "_OP_KEY_SCOPE"),
            surfaces_missing=("OP_MODULE_MAP",),
            missing_surface_files=(("OP_MODULE_MAP", "coordinator_core/ops/_registry_map.py"),),
        )
        baseline = {"memo.fate_backfill": ("OP_MODULE_MAP",)}
        assert filter_known_violations([v], incomplete_baseline=baseline, suspended=frozenset()) == []
