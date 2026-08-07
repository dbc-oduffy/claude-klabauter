#!/usr/bin/env python3
"""
test_coordinator_harvest_deferrals.py — selection-rule tests for
coordinator-harvest-deferrals, covering the C5b widening (D8).

Spec backlink: docs/plans/2026-07-27-plan-line-item-resolution-model.md § C5b,
§ D8 (AC18).

C5b widened `coordinator-harvest-deferrals` selection from
"`deferred: true` AND `pm_approved: true`" to "`disposition: backlogged` AND
`pm_approved: true` — OR, for a row carrying no `disposition` at all,
`deferred: true` AND `pm_approved: true` treated as legacy-equivalent." This
file exercises that selection rule at two altitudes:

  1. `_select_harvest_candidates` directly (in-process, via
     `importlib.machinery.SourceFileLoader` — the script has no `.py`
     extension, so `spec_from_file_location` alone yields a `None` loader;
     an explicit `SourceFileLoader` is required, mirroring
     `plan_tasks_mutate._load_harvest_module`'s own load pattern).
  2. The whole CLI, end-to-end, via `--dry-run` subprocess invocation against
     a synthetic plan fixture — the AC18 acceptance-level check.

Tests:
  1. Row with `disposition: backlogged` + `pm_approved: true` is selected
     (disposition-selected row).
  2. Row with `deferred: true` + `pm_approved: true` and NO `disposition` key
     is selected (legacy deferred-only row).
  3. Row with BOTH `deferred: true` AND `disposition: backlogged` (+
     `pm_approved: true`) is selected exactly once — not twice (both-set row).
  4. Row with `disposition: backlogged` but `pm_approved: false` is NOT
     selected (backlogged row lacking pm_approved).
  5. Row with `disposition: open` and `deferred: true` + `pm_approved: true`
     is NOT selected — a non-backlogged disposition always wins over a
     legacy-equivalent deferred reading, even when deferred is also true.
  6. Row with `deferred: true` but `pm_approved` absent (no disposition) is
     NOT selected — the legacy branch still requires pm_approved.
  7. `_REQUIRED_ROW_FIELDS` no longer includes "deferred" — a well-formed row
     lacking a `deferred` key entirely (disposition-only authoring) is not
     flagged malformed.
  8. End-to-end `--dry-run`: a plan fixture combining rows 1-4 above reports
     exactly 3 queued items (disposition-selected, legacy-only, both-set —
     once each) and excludes the pm_approved:false row.

Run with: python3 -m pytest coordinator/bin/test_coordinator_harvest_deferrals.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest
from coordinator_core.win_portability import no_console_creationflags


def _script_path() -> str:
    """Return the absolute path to coordinator-harvest-deferrals."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator-harvest-deferrals")


def _python() -> str:
    """Return the Python interpreter to use for subprocess invocations.

    Uses sys.executable — the interpreter running this test script is always a
    valid Python interpreter (Windows-compatible zero-probe pattern, matches
    test_coordinator_lesson_promote.py's own helper).
    """
    return sys.executable


def _load_harvest_module():
    """Load coordinator-harvest-deferrals in-process.

    The script has no `.py` extension, so `importlib.util.spec_from_file_location`
    alone infers no loader (returns `spec.loader is None`) and
    `module_from_spec` raises `AttributeError`. An explicit
    `importlib.machinery.SourceFileLoader` sidesteps the extension-based
    inference entirely — the same fix `plan_tasks_mutate._load_harvest_module`
    needs for the identical reason (see that function's own docstring).
    """
    path = _script_path()
    loader = importlib.machinery.SourceFileLoader("_test_harvest_deferrals_cli", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harvest_mod():
    return _load_harvest_module()


# ---------------------------------------------------------------------------
# _select_harvest_candidates — direct unit tests
# ---------------------------------------------------------------------------


def _row(row_id: str, **overrides) -> dict:
    base = {
        "id": row_id,
        "title": f"title-{row_id}",
        "change_kind": "code-edit",
        "surface": "some/surface.py",
        "body": "body text",
    }
    base.update(overrides)
    return base


def test_disposition_selected_row(harvest_mod):
    rows = [_row("D1", disposition="backlogged", pm_approved=True)]
    candidates, warnings, malformed = harvest_mod._select_harvest_candidates(rows)
    assert [r["id"] for r in candidates] == ["D1"]
    assert warnings == []
    assert malformed == 0


def test_legacy_deferred_only_row(harvest_mod):
    rows = [_row("D2", deferred=True, pm_approved=True)]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    assert [r["id"] for r in candidates] == ["D2"]


def test_both_set_row_harvested_once(harvest_mod):
    rows = [
        _row(
            "D3",
            deferred=True,
            disposition="backlogged",
            pm_approved=True,
        )
    ]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    assert [r["id"] for r in candidates] == ["D3"]
    assert len(candidates) == 1


def test_backlogged_row_lacking_pm_approved_not_harvested(harvest_mod):
    rows = [_row("D4", disposition="backlogged", pm_approved=False)]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    assert candidates == []


def test_non_backlogged_disposition_never_selected_even_with_legacy_deferred(harvest_mod):
    rows = [_row("D5", disposition="open", deferred=True, pm_approved=True)]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    assert candidates == []


def test_legacy_deferred_without_pm_approved_not_selected(harvest_mod):
    rows = [_row("D6", deferred=True)]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    assert candidates == []


def test_required_row_fields_no_longer_includes_deferred(harvest_mod):
    assert "deferred" not in harvest_mod._REQUIRED_ROW_FIELDS
    row = _row("D7", disposition="backlogged", pm_approved=True)
    assert "deferred" not in row
    problem = harvest_mod._row_is_well_formed(row)
    assert problem is None


# ---------------------------------------------------------------------------
# End-to-end --dry-run subprocess test (AC18 acceptance level)
# ---------------------------------------------------------------------------


_PLAN_FIXTURE = textwrap.dedent(
    """\
    ---
    plan_id: "pln-test-c5b-selection-000001"
    ---

    # Test plan for C5b selection

    ## Tasks

    ```yaml plan-tasks
    - id: D1
      title: Disposition selected row
      change_kind: code-edit
      surface: some/surface1.py
      disposition: backlogged
      disposition_ref: state/improvement-queue/fixture-d1.yaml
      disposition_detail: test detail
      pm_approved: true
      body: |
        Disposition-selected row body.
    - id: D2
      title: Legacy deferred-only row
      change_kind: code-edit
      surface: some/surface2.py
      deferred: true
      pm_approved: true
      body: |
        Legacy deferred-only row body.
    - id: D3
      title: Both-set row harvested once
      change_kind: code-edit
      surface: some/surface3.py
      deferred: true
      disposition: backlogged
      disposition_ref: state/improvement-queue/fixture-d3.yaml
      disposition_detail: test detail
      pm_approved: true
      body: |
        Both-set row body.
    - id: D4
      title: Backlogged row lacking pm_approved
      change_kind: code-edit
      surface: some/surface4.py
      disposition: backlogged
      disposition_ref: state/improvement-queue/fixture-d4.yaml
      disposition_detail: test detail
      pm_approved: false
      body: |
        Backlogged row lacking pm_approved body.
    ```
    """
)


def test_end_to_end_dry_run_selection(tmp_path):
    plan_path = tmp_path / "2026-07-27-c5b-selection-fixture.md"
    plan_path.write_text(_PLAN_FIXTURE, encoding="utf-8")

    result = subprocess.run(
        [_python(), _script_path(), "--plan", str(plan_path), "--dry-run"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )

    assert result.returncode == 0, result.stderr
    assert "Queued 3 deferred items:" in result.stdout
    assert "D1" in result.stdout
    assert "D2" in result.stdout
    assert "D3" in result.stdout
    assert "D4" not in result.stdout


# ---------------------------------------------------------------------------
# Unroutable change_kind — a row selected as a harvest candidate but whose
# change_kind matches neither write seam. See _harvest's skipped_unroutable
# return value: a pm_approved:true row in this shape must fail loudly rather
# than evaporate on exit 0 (a PM-ratified deferral is not recoverable if
# silently dropped); a non-pm_approved row stays a soft warn.
# ---------------------------------------------------------------------------


def _unroutable_plan_fixture(*, pm_approved: bool) -> str:
    return textwrap.dedent(
        f"""\
        ---
        plan_id: "pln-test-unroutable-change-kind-000001"
        ---

        # Test plan for unroutable change_kind

        ## Tasks

        ```yaml plan-tasks
        - id: W-windows-verify
          title: Hardware-gated Windows verification follow-on
          change_kind: script-port
          surface: some/surface.py
          deferred: true
          pm_approved: {"true" if pm_approved else "false"}
          body: |
            Unroutable row body.
        ```
        """
    )


def test_pm_approved_unroutable_row_fails_loud(tmp_path):
    plan_path = tmp_path / "2026-07-29-unroutable-pm-approved-fixture.md"
    plan_path.write_text(_unroutable_plan_fixture(pm_approved=True), encoding="utf-8")

    result = subprocess.run(
        [_python(), _script_path(), "--plan", str(plan_path), "--dry-run"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )

    assert result.returncode != 0
    assert "W-windows-verify" in result.stdout
    assert "script-port" in result.stdout
    assert "W-windows-verify" in result.stderr
    assert "SILENTLY LOST" in result.stderr


def test_non_pm_approved_unroutable_row_stays_exit_zero(tmp_path):
    plan_path = tmp_path / "2026-07-29-unroutable-non-pm-approved-fixture.md"
    plan_path.write_text(_unroutable_plan_fixture(pm_approved=False), encoding="utf-8")

    result = subprocess.run(
        [_python(), _script_path(), "--plan", str(plan_path), "--dry-run"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )

    # pm_approved:false means the row is not even a harvest candidate (D8
    # selection rule requires pm_approved:true) — it never reaches _harvest's
    # unroutable branch at all, and the run is a normal empty no-op.
    assert result.returncode == 0, result.stderr
    assert "Queued 0 deferred items: (none)" in result.stdout


def test_harvest_unroutable_not_pm_approved_stays_soft_warn(harvest_mod):
    rows = [
        _row(
            "D8",
            disposition="backlogged",
            pm_approved=True,
            change_kind="script-port",
        )
    ]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    assert [r["id"] for r in candidates] == ["D8"]

    # Simulate a row that reaches _harvest with pm_approved False directly
    # (defensive coverage of the dict-shape contract even though the CLI's
    # selection rule always gates on pm_approved:true before a row becomes a
    # candidate) — _harvest itself must not assume pm_approved is always True.
    row = dict(candidates[0])
    row["pm_approved"] = False
    queued_ids, deduped, failed, skipped_unroutable = harvest_mod._harvest(
        "pln-test-unroutable-000002", [row], dry_run=True
    )
    assert queued_ids == []
    assert deduped == 0
    assert failed == 0
    assert skipped_unroutable == [
        {"id": "D8", "change_kind": "script-port", "pm_approved": False}
    ]


def test_harvest_unroutable_pm_approved_recorded(harvest_mod):
    rows = [
        _row(
            "W-windows-verify",
            disposition="backlogged",
            pm_approved=True,
            change_kind="script-port",
        )
    ]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)

    queued_ids, deduped, failed, skipped_unroutable = harvest_mod._harvest(
        "pln-test-unroutable-000003", candidates, dry_run=True
    )
    assert queued_ids == []
    assert deduped == 0
    assert failed == 0
    assert skipped_unroutable == [
        {"id": "W-windows-verify", "change_kind": "script-port", "pm_approved": True}
    ]


def test_config_edit_routes_to_queue_append(harvest_mod):
    """`config-edit` is in BOTH the plan-tasks and improvement-queue enums but
    matched neither routing branch before it was added to
    `_QUEUE_ELIGIBLE_CHANGE_KINDS` — a `backlogged` resolve on such a row hit
    `_dispatch_backlogged`'s abort path in plan_tasks_mutate.py. Asserts it
    reaches `_harvest` as routable (queued, never `skipped_unroutable`)."""
    rows = [
        _row("CFG1", disposition="backlogged", pm_approved=True, change_kind="config-edit"),
    ]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    queued_ids, deduped, failed, skipped_unroutable = harvest_mod._harvest(
        "pln-test-config-edit-000005", candidates, dry_run=True
    )
    assert queued_ids == ["CFG1"]
    assert deduped == 0
    assert failed == 0
    assert skipped_unroutable == []


def test_verification_routes_to_queue_append(harvest_mod):
    """`verification` is in BOTH the plan-tasks and improvement-queue enums
    but matched neither routing branch before it was added to
    `_QUEUE_ELIGIBLE_CHANGE_KINDS` — the same abort-path shape as
    `config-edit` above. Asserts it reaches `_harvest` as routable (queued,
    never `skipped_unroutable`)."""
    rows = [
        _row("VER1", disposition="backlogged", pm_approved=True, change_kind="verification"),
    ]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    queued_ids, deduped, failed, skipped_unroutable = harvest_mod._harvest(
        "pln-test-verification-000006", candidates, dry_run=True
    )
    assert queued_ids == ["VER1"]
    assert deduped == 0
    assert failed == 0
    assert skipped_unroutable == []


def test_all_routable_behavior_unchanged(harvest_mod):
    """Existing all-routable end-to-end behavior (D1/D2/D3) is unaffected by
    the skipped_unroutable plumbing — same as test_end_to_end_dry_run_selection,
    re-asserted here alongside the new unroutable-row tests for locality."""
    rows = [
        _row("R1", disposition="backlogged", pm_approved=True, change_kind="code-edit"),
        _row("R2", deferred=True, pm_approved=True, change_kind="doc-edit"),
    ]
    candidates, _warnings, _malformed = harvest_mod._select_harvest_candidates(rows)
    queued_ids, deduped, failed, skipped_unroutable = harvest_mod._harvest(
        "pln-test-all-routable-000004", candidates, dry_run=True
    )
    assert set(queued_ids) == {"R1", "R2"}
    assert deduped == 0
    assert failed == 0
    assert skipped_unroutable == []


# ---------------------------------------------------------------------------
# Routing-set / vendored-schema agreement
#
# The two frozensets are a hand-maintained mirror of example-doctrine-repo-owned enums, and the
# comment above `_QUEUE_ELIGIBLE_CHANGE_KINDS` states that mirror obligation in
# prose. Prose is not a discharge: on 2026-07-29 the `verification` member was
# added to the frozenset while the vendored improvement-queue schema was left
# behind, which is strictly worse than the drift it replaced. A row now ROUTED
# to a write seam that then REJECTS it fails at the write instead of at the
# routing decision, so the operator reads a queue-append validation error
# rather than "unroutable change_kind".
#
# These two tests are that discharge. They are deliberately asymmetric,
# because the two enums stand in different relations to the routing sets:
#   - improvement-queue is the WRITE TARGET for `_QUEUE_ELIGIBLE_*`, so the
#     relation is EQUALITY. A member either set does not share is a live break
#     in one direction or dead routing in the other.
#   - plan-tasks is the AUTHORING enum, a superset spanning both seams, so the
#     relation is that the union of both routing sets EQUALS it. An authorable
#     value routing nowhere is exactly the W-windows-verify failure.
# ---------------------------------------------------------------------------


def _vendored_enum(schema_name: str) -> set[str]:
    """Read `change_kind`'s enum out of a vendored schema.

    Reads the vendored bytes directly rather than going through
    `schema_validate`: the subject under test is what the harvest CLI's routing
    sets are mirrored FROM, so an indirection that could normalize or default
    the value would weaken the assertion.
    """
    schema_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "coordinator_core"
        / "frontmatter"
        / "schemas"
        / f"{schema_name}.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return set(schema["properties"]["change_kind"]["enum"])


def test_queue_eligible_set_equals_improvement_queue_schema_enum(harvest_mod):
    """`_QUEUE_ELIGIBLE_CHANGE_KINDS` must equal the improvement-queue enum.

    Failure means a `verification`-shaped half-sync: either the harvest routes
    a value the write target rejects, or the write target accepts a value the
    harvest calls unroutable. Fix by re-vendoring
    (`python3 bin/claude-klabauter-revendor-schema.py improvement-queue --reason ...`)
    and mirroring the member into the frozenset — both, not either.
    """
    assert harvest_mod._QUEUE_ELIGIBLE_CHANGE_KINDS == _vendored_enum("improvement-queue")


def test_routing_sets_cover_the_plan_tasks_authoring_enum(harvest_mod):
    """Every authorable `change_kind` must route to exactly one write seam.

    plan-tasks is the wider universal enum an author writes against; the two
    routing sets partition it. A value in the schema but in neither set is
    authorable-but-unroutable — the exact shape that dropped a PM-ratified row
    (`W-windows-verify`) and prompted example-doctrine-repo 1239761c1. A value in a routing set
    but not the schema is unreachable routing for a value nobody can author.
    """
    routable = harvest_mod._QUEUE_ELIGIBLE_CHANGE_KINDS | harvest_mod._LESSON_PROMOTE_CHANGE_KINDS
    assert routable == _vendored_enum("plan-tasks")


def test_routing_sets_are_disjoint(harvest_mod):
    """No `change_kind` may route to both seams.

    `_harvest` tests lesson-promote first and queue-append second, so an
    overlapping member would silently take the lesson-promote branch and the
    queue-append intent would be unreachable — a routing ambiguity resolved by
    statement order rather than by design.
    """
    assert not (
        harvest_mod._QUEUE_ELIGIBLE_CHANGE_KINDS & harvest_mod._LESSON_PROMOTE_CHANGE_KINDS
    )
