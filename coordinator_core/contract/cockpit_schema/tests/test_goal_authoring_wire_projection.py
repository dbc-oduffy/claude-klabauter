"""
test_goal_authoring_wire_projection — executable spec fixtures for the
authoring→wire projection (C11's field map) and fleet-keying (C12).

Pytest port of example-doctrine-repo `coordinator/cockpit-contract/test/goal-authoring-wire-projection.test.ts`.

This package does not host the emit engine (DR-047: claude-klabauter's `artifact.emit`
is the engine-tier producer — this same package, DR-047 post-boundary-redraw).
These tests pin the CONTRACT the engine must satisfy: given an authoring-side
goal artifact shape, the projected wire record must match this exact
transform. `_project_goal_artifact_to_wire` below is a minimal reference
projection — encodes ONLY the field map from C11, not a production emitter —
so the assertions are executable rather than prose.

Field map (C11, docs/plans/2026-07-13-close-weekly-goal-loop.md § C11):
  weekly_perceptible          — 1:1 passthrough, no OR-over-KRs derivation
  key_results[] → key_results_status[] — DROP evidence_source + per-KR
                                 weekly_perceptible; keep {id,text,kind,status}
  parent_goal_id               — D9 present-as-null: emit null when absent,
                                 NEVER omit the key (nullable, not optional)
  objective → text: "${goal_id}: ${objective}" — folded into `text` via
                                 string interpolation with the goal_id prefix
"""
from __future__ import annotations

from typing import Any

from coordinator_core.contract.cockpit_schema.entities.goal import Goal
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    load_fixture,
    skip_no_fixtures,
    zod_safe_parse_ok,
)


def _project_goal_artifact_to_wire(a: dict[str, Any]) -> dict[str, Any]:
    """Reference projection encoding the C11 field map exactly — NOT the
    production emitter. Exists so the field map is an executable, testable
    spec rather than prose."""
    wire: dict[str, Any] = {
        "goal_id": a["goal_id"],
        "repo": a["repo"],
        "coordinator_root_path": a["coordinator_root_path"],
        "period": a["period"],
        "period_value": a["period_value"],
        "declared_by_machine": a["declared_by_machine"],
        "declared_at": a["declared_at"],
        "text": f"{a['goal_id']}: {a['objective']}",
        "status": a["status"],
        "provenance": a["provenance"],
        # D9: present-as-null when absent — the key is ALWAYS emitted.
        "parent_goal_id": a.get("parent_goal_id"),
    }

    # weekly_perceptible: absent-when-absent (1:1 passthrough, no OR-over-KRs).
    if "weekly_perceptible" in a:
        wire["weekly_perceptible"] = a["weekly_perceptible"]

    # key_results[] -> key_results_status[]: absent-when-absent; drop
    # evidence_source + per-KR weekly_perceptible, keep {id,text,kind,status}.
    if "key_results" in a:
        wire["key_results_status"] = [
            {"id": kr["id"], "text": kr["text"], "kind": kr["kind"], "status": kr["status"]}
            for kr in a["key_results"]
        ]

    return wire


def _base_authoring_artifact() -> dict[str, Any]:
    return {
        "goal_id": "2026-W29-close-the-loop",
        "repo": "dbc-example-operator/.example-doctrine-mirror-repo",
        "coordinator_root_path": ".",
        "period": "week",
        "period_value": "2026-W29",
        "declared_by_machine": "machine-b",
        "declared_at": "2026-07-13T08:00:00Z",
        "objective": "Close the weekly goal loop end to end.",
        "status": "active",
        "provenance": {
            "source_kind": "coordinator_artifact",
            "repo": "dbc-example-operator/.example-doctrine-mirror-repo",
            "ref": None,
            "path": "state/goals/2026-W29.yaml",
            "observed_at": "2026-07-13T08:00:01Z",
            "derivation": "parsed",
            "entity_anchor": None,
        },
    }


# ===========================================================================
# weekly_perceptible
# ===========================================================================


def test_weekly_perceptible_true_is_1to1_passthrough_no_or_over_krs():
    artifact = {
        **_base_authoring_artifact(),
        "weekly_perceptible": True,
        "key_results": [
            {
                "id": "kr-1",
                "text": "Ship the thing",
                "kind": "metric",
                "status": "on_track",
                "weekly_perceptible": False,
            }
        ],
    }
    wire = _project_goal_artifact_to_wire(artifact)
    # Goal-level flag is NOT derived from (OR'd over) per-KR weekly_perceptible.
    assert wire["weekly_perceptible"] is True
    assert zod_safe_parse_ok(Goal, wire)


def test_weekly_perceptible_false_stays_false_even_when_kr_sets_true():
    artifact = {
        **_base_authoring_artifact(),
        "weekly_perceptible": False,
        "key_results": [
            {
                "id": "kr-1",
                "text": "Ship the thing",
                "kind": "metric",
                "status": "on_track",
                "weekly_perceptible": True,
            }
        ],
    }
    wire = _project_goal_artifact_to_wire(artifact)
    assert wire["weekly_perceptible"] is False
    assert zod_safe_parse_ok(Goal, wire)


def test_weekly_perceptible_absent_on_authoring_stays_absent_on_wire():
    wire = _project_goal_artifact_to_wire(_base_authoring_artifact())
    assert "weekly_perceptible" not in wire
    assert zod_safe_parse_ok(Goal, wire)


# ===========================================================================
# key_results[] -> key_results_status[]
# ===========================================================================


def test_key_results_drops_evidence_source_and_per_kr_weekly_perceptible():
    artifact = {
        **_base_authoring_artifact(),
        "key_results": [
            {
                "id": "kr-1",
                "text": "Ship the thing",
                "kind": "metric",
                "status": "on_track",
                "weekly_perceptible": True,
                "evidence_source": "state/goals/evidence/kr-1.md",
            }
        ],
    }
    wire = _project_goal_artifact_to_wire(artifact)
    projected = wire["key_results_status"][0]

    assert sorted(projected.keys()) == ["id", "kind", "status", "text"]
    assert "evidence_source" not in projected
    assert "weekly_perceptible" not in projected
    assert projected == {
        "id": "kr-1",
        "text": "Ship the thing",
        "kind": "metric",
        "status": "on_track",
    }
    assert zod_safe_parse_ok(Goal, wire)


def test_key_results_absent_leaves_key_results_status_absent_on_wire():
    wire = _project_goal_artifact_to_wire(_base_authoring_artifact())
    assert "key_results_status" not in wire
    assert zod_safe_parse_ok(Goal, wire)


# ===========================================================================
# parent_goal_id D9 present-as-null
# ===========================================================================


def test_no_parent_goal_id_on_authoring_emits_null_on_wire_and_passes():
    artifact = _base_authoring_artifact()
    assert "parent_goal_id" not in artifact

    wire = _project_goal_artifact_to_wire(artifact)
    assert "parent_goal_id" in wire
    assert wire["parent_goal_id"] is None
    assert zod_safe_parse_ok(Goal, wire)


@skip_no_fixtures
def test_fixture_omitting_parent_goal_id_entirely_fails_validation_d9():
    """Asserts the schema-level obligation directly (not via the reference
    projection, which always emits the key) — a naive emitter that drops the
    key on absence, rather than emitting null, must fail."""
    wire = load_fixture("goal")
    del wire["parent_goal_id"]
    assert not zod_safe_parse_ok(Goal, wire)


def test_declared_parent_goal_id_passes_through_1to1():
    artifact = {**_base_authoring_artifact(), "parent_goal_id": "2026-Q3-okr-close-the-loop"}
    wire = _project_goal_artifact_to_wire(artifact)
    assert wire["parent_goal_id"] == "2026-Q3-okr-close-the-loop"
    assert zod_safe_parse_ok(Goal, wire)


# ===========================================================================
# fleet keying — non-meta repo emits its own period=week goals keyed on
# owner-qualified repo + declared_by_machine
# ===========================================================================


def test_two_distinct_repos_same_machine_produce_distinct_independently_valid_goals():
    repo_a = {
        **_base_authoring_artifact(),
        "goal_id": "2026-W29-repo-a-goal",
        "repo": "dbc-example-operator/example-retrieval-repo",
        "declared_by_machine": "machine-b",
        "period": "week",
        "period_value": "2026-W29",
    }
    repo_b = {
        **_base_authoring_artifact(),
        "goal_id": "2026-W29-repo-b-goal",
        "repo": "dbc-example-operator/claude-klabauter",
        "declared_by_machine": "machine-b",
        "period": "week",
        "period_value": "2026-W29",
    }

    wire_a = _project_goal_artifact_to_wire(repo_a)
    wire_b = _project_goal_artifact_to_wire(repo_b)

    assert zod_safe_parse_ok(Goal, wire_a)
    assert zod_safe_parse_ok(Goal, wire_b)

    # Owner-qualified repo + declared_by_machine is the join anchor (C12) —
    # distinct repos must not collapse onto the same key.
    assert wire_a["repo"] != wire_b["repo"]
    assert wire_a["declared_by_machine"] == wire_b["declared_by_machine"]
    assert wire_a["goal_id"] != wire_b["goal_id"]


def test_non_meta_repo_week_goal_not_cross_repo_aggregate_sentinel():
    artifact = {
        **_base_authoring_artifact(),
        "repo": "dbc-example-operator/example-cockpit-repo",
        "period": "week",
        "period_value": "2026-W29",
    }
    wire = _project_goal_artifact_to_wire(artifact)
    assert wire["repo"] != ""
    assert zod_safe_parse_ok(Goal, wire)


def test_emitted_machine_goal_same_repo_different_machine_is_distinct_declaration():
    from_machine_b = {
        **_base_authoring_artifact(),
        "goal_id": "2026-W29-shared-repo-goal-b",
        "repo": "dbc-example-operator/example-retrieval-repo",
        "declared_by_machine": "machine-b",
    }
    from_machine_a = {
        **_base_authoring_artifact(),
        "goal_id": "2026-W29-shared-repo-goal-machine-a",
        "repo": "dbc-example-operator/example-retrieval-repo",
        "declared_by_machine": "machine-a",
    }

    wire_b = _project_goal_artifact_to_wire(from_machine_b)
    wire_machine_a = _project_goal_artifact_to_wire(from_machine_a)

    assert zod_safe_parse_ok(Goal, wire_b)
    assert zod_safe_parse_ok(Goal, wire_machine_a)
    assert wire_b["repo"] == wire_machine_a["repo"]
    assert wire_b["declared_by_machine"] != wire_machine_a["declared_by_machine"]
