"""
coordinator_core.hooks.tests.test_watchdog_undischarged_next_move — Tier-T test
for the watchdog-undischarged-next-move port (chunk C4,
docs/reference/warm-hook-migration.md).

Three obligations, per this chunk's dispatch brief (none catches the others):
  (a) the op is registered and resolvable through `warm.hook_http.op_for_path`;
  (b) it is CLASSIFIED — an explicit assertion of the `classify()` call/result;
  (c) it returns the source script's shape for one real, firing payload, on
      BOTH legs (PostToolUse emission, Stop report).
"""

from __future__ import annotations

import importlib
import json
import os

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path
from coordinator_core.session import machinery_paths

_OP_NAME = "hooks.watchdog_undischarged_next_move"


def _make_repo(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    return str(repo_root)


def _ledger_path(repo_root: str, session_id: str) -> str:
    return machinery_paths.ledger_path(repo_root, session_id)


def test_op_registers_and_resolves_through_op_for_path() -> None:
    module = importlib.import_module(
        "coordinator_core.hooks.watchdog_undischarged_next_move"
    )
    assert hasattr(module, "_handler")

    from coordinator_core.ipc import _REGISTRY

    assert _OP_NAME in _REGISTRY

    resolved = op_for_path(HOOK_PATH + "/" + _OP_NAME)
    assert resolved == _OP_NAME


def test_op_is_classified_mutating() -> None:
    # Explicit assertion of the classify() call/result — routing alone (a
    # `hooks.` prefix match) never reaches `_is_compute_only`, so an absent
    # classification would pass every routing test and still be a
    # dispatch-time authz gap.
    assert classify(_OP_NAME) is OpClass.MUTATING


def test_post_tool_use_leg_is_always_silent(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    payload = {
        "session_id": "sid-pickup",
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:pickup"},
    }
    result = _handler({"payload": payload})
    assert result == {}

    # But the ledger side-effect landed — the PostToolUse leg is silent
    # bookkeeping, not a no-op.
    ledger = _ledger_path(repo_root, "sid-pickup")
    assert os.path.isfile(ledger)
    with open(ledger, "r", encoding="utf-8") as fh:
        record = json.loads(fh.readline())
    assert record["seam"] == "pickup->next-move"
    assert record["discharged_at"] is None
    assert record["fired"] is False


def test_stop_leg_reports_undischarged_obligation_at_precision(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    session_id = "sid-stop-fire"

    open_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:pickup"},
    }
    assert _handler({"payload": open_payload}) == {}

    # No coordinator.local.md / identity file resolvable for this synthetic
    # cwd -> posture fails open to "precision" -> non-blocking allow_advisory,
    # the same hookSpecificOutput shape the source script prints to stdout at
    # posture "precision".
    stop_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    result = _handler({"payload": stop_payload})
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "Stop"
    assert hso["permissionDecision"] == "allow"
    assert "Skill|Agent(the narrated next move)" in hso["additionalContext"]

    # One-fire-per-obligation latch: a second Stop for the same undischarged
    # obligation must stay silent.
    assert _handler({"payload": stop_payload}) == {}


def test_stop_leg_blocks_at_default_posture(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    with open(os.path.join(repo_root, "coordinator.local.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nengagement_posture: default\n---\n")

    session_id = "sid-stop-block"
    open_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:pickup"},
    }
    assert _handler({"payload": open_payload}) == {}

    stop_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    result = _handler({"payload": stop_payload})
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "Stop"
    assert hso["permissionDecision"] == "deny"
    assert "Invoke it now" in hso["permissionDecisionReason"]


def test_discharge_closes_the_obligation_before_stop_fires(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    session_id = "sid-discharge"

    open_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:review"},
    }
    assert _handler({"payload": open_payload}) == {}

    # review-a1-a2 discharges on ANY subsequent Agent call.
    discharge_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Agent",
        "tool_input": {},
    }
    assert _handler({"payload": discharge_payload}) == {}

    stop_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    assert _handler({"payload": stop_payload}) == {}


def test_post_tool_use_suppresses_on_agent_id() -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    payload = {"agent_id": "some-subagent", "session_id": "sid", "tool_name": "Skill"}
    assert _handler({"payload": payload}) == {}


# Review: coordinator:code-reviewer Finding 3 -- the sizing-route resolution
# path (`_newest_touched_sizing_path` / `_sizing_route_and_exemption` /
# `_extract_scalar` / `_extract_detents`, exercised from `_handle_post_tool_use`'s
# coordinator:sizing/coordinator:plan branch) had zero coverage; add one test
# per `_ROUTE_TERMINAL` entry, one for the appetite/post-size-prompt exemption,
# and the negative "spec-dispatch does not open plan->review" case.


def _write_touch_record(repo_root: str, session_id: str, rel_sizing_path: str) -> None:
    git_dir = os.path.join(repo_root, ".git")
    session_dir = os.path.join(git_dir, "coordinator-sessions", session_id)
    os.makedirs(session_dir, exist_ok=True)
    with open(os.path.join(session_dir, "touch-record.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"path": rel_sizing_path}) + "\n")


def _write_sizing(repo_root: str, rel_path: str, route: str, detents=None) -> None:
    full_path = os.path.join(repo_root, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    lines = [f"route: {route}\n"]
    if detents:
        lines.append("detents:\n")
        lines.extend(f"  - {d}\n" for d in detents)
    with open(full_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def _sizing_open_ledger_action(tmp_path, route: str, skill: str):
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    session_id = "sid-sizing"
    rel_path = "state/sizings/thing.yaml"
    _write_sizing(repo_root, rel_path, route)
    _write_touch_record(repo_root, session_id, rel_path)

    payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": skill},
    }
    assert _handler({"payload": payload}) == {}
    ledger = _ledger_path(repo_root, session_id)
    if not os.path.isfile(ledger):
        return None
    with open(ledger, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_sizing_route_dispatch_opens_sizing_routed_obligation(tmp_path) -> None:
    records = _sizing_open_ledger_action(tmp_path, "dispatch", "coordinator:sizing")
    assert records and records[0]["seam"] == "sizing-routed"
    assert records[0]["next_action"] == "Agent(coordinator:executor)"


def test_sizing_route_spec_dispatch_opens_sizing_routed_obligation(tmp_path) -> None:
    records = _sizing_open_ledger_action(tmp_path, "spec-dispatch", "coordinator:sizing")
    assert records and records[0]["next_action"] == "Agent(coordinator:executor)"


def test_sizing_route_plan_opens_sizing_routed_obligation(tmp_path) -> None:
    records = _sizing_open_ledger_action(tmp_path, "plan", "coordinator:sizing")
    assert records and records[0]["next_action"] == "Skill(coordinator:plan)"


def test_sizing_route_shape_opens_sizing_routed_obligation(tmp_path) -> None:
    records = _sizing_open_ledger_action(tmp_path, "shape", "coordinator:sizing")
    assert records and records[0]["next_action"] == "Skill(coordinator:plan)"


def test_sizing_route_roadmap_opens_sizing_routed_obligation(tmp_path) -> None:
    records = _sizing_open_ledger_action(tmp_path, "roadmap", "coordinator:sizing")
    assert records and records[0]["next_action"] == "Skill(coordinator:plan)"


def test_sizing_route_exemption_suppresses_the_obligation(tmp_path) -> None:
    # appetite_exceeded detent with a null fork holds the exemption open --
    # no obligation should be opened even though the route resolves.
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    session_id = "sid-exempt"
    rel_path = "state/sizings/thing.yaml"
    _write_sizing(repo_root, rel_path, "dispatch", detents=["appetite_exceeded"])
    _write_touch_record(repo_root, session_id, rel_path)

    payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:sizing"},
    }
    assert _handler({"payload": payload}) == {}
    assert not os.path.isfile(_ledger_path(repo_root, session_id))


def test_plan_skill_spec_dispatch_route_does_not_open_plan_review(tmp_path) -> None:
    # Only the FULL "plan" terminal opens plan->review; "spec-dispatch" must
    # not, even though it is a valid _ROUTE_TERMINAL entry for coordinator:sizing.
    records = _sizing_open_ledger_action(tmp_path, "spec-dispatch", "coordinator:plan")
    assert records is None


def test_plan_skill_plan_route_opens_plan_review(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    session_id = "sid-plan-review"
    rel_path = "state/sizings/thing.yaml"
    _write_sizing(repo_root, rel_path, "plan")
    _write_touch_record(repo_root, session_id, rel_path)

    payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:plan"},
    }
    assert _handler({"payload": payload}) == {}
    with open(_ledger_path(repo_root, session_id), "r", encoding="utf-8") as fh:
        record = json.loads(fh.readline())
    assert record["seam"] == "plan->review"
    assert record["next_action"] == "Skill(coordinator:review)"


# Review: coordinator:code-reviewer Finding 4 -- `_drain_intake`'s fold-and-
# delete behavior (the module's cross-plane consumption contract with
# `coordinator_core.group_em.obligations`'s producer) had zero coverage.


def _intake_path(repo_root: str, session_id: str) -> str:
    return machinery_paths.intake_path(repo_root, session_id)


def test_drain_intake_folds_open_and_discharge_rows_and_removes_the_file(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _drain_intake

    repo_root = _make_repo(tmp_path)
    session_id = "sid-intake"
    os.makedirs(os.path.dirname(_intake_path(repo_root, session_id)), exist_ok=True)

    rows = [
        {
            "schema": 1,
            "session_id": session_id,
            "op": "open",
            "obligation_id": "obl-a",
            "seam": "seam-a",
            "next_action": "do-a",
        },
        {
            "schema": 1,
            "session_id": session_id,
            "op": "discharge",
            "obligation_id": "obl-b",
        },
        # Malformed row (missing obligation_id) -- must be skipped without
        # aborting the fold of the rows around it.
        {"schema": 1, "session_id": session_id, "op": "open"},
    ]
    with open(_intake_path(repo_root, session_id), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    # Pre-seed obl-b as an open obligation so the discharge row has an effect.
    with open(_ledger_path(repo_root, session_id), "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "obligation_id": "obl-b",
                    "seam": "seam-b",
                    "next_action": "do-b",
                    "opened_at": "2026-01-01T00:00:00Z",
                    "progressed_at": None,
                    "blocked_at": None,
                    "blocked_on_session_id": None,
                    "blocked_on_name": None,
                    "discharged_at": None,
                    "fired": False,
                }
            )
            + "\n"
        )

    _drain_intake(repo_root, session_id)

    assert not os.path.isfile(_intake_path(repo_root, session_id))
    with open(_ledger_path(repo_root, session_id), "r", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    by_id = {r["obligation_id"]: r for r in records}
    assert by_id["obl-a"]["discharged_at"] is None
    assert by_id["obl-b"]["discharged_at"] is not None


def test_drain_intake_progress_and_blocked_rows_are_consumed_without_effect(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _drain_intake

    repo_root = _make_repo(tmp_path)
    session_id = "sid-intake-noop"
    os.makedirs(os.path.dirname(_intake_path(repo_root, session_id)), exist_ok=True)

    rows = [
        {"schema": 1, "session_id": session_id, "op": "progress", "obligation_id": "obl-x"},
        {
            "schema": 1,
            "session_id": session_id,
            "op": "blocked",
            "obligation_id": "obl-x",
            "blocked_on_session_id": "other-session",
        },
    ]
    with open(_intake_path(repo_root, session_id), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    _drain_intake(repo_root, session_id)

    # Rows had no observable effect on the ledger (no record ever opened),
    # but the intake file is still consumed -- not left to accumulate.
    assert not os.path.isfile(_intake_path(repo_root, session_id))
    assert not os.path.isfile(_ledger_path(repo_root, session_id))


def test_session_id_is_read_from_payload_never_from_environment(tmp_path, monkeypatch) -> None:
    """The ledger must be keyed on params["payload"]["session_id"], never on
    this process's own environment (trap 1 in the module docstring)."""
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "wrong-session-from-env")
    repo_root = _make_repo(tmp_path)
    payload = {
        "session_id": "sid-from-payload",
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:pickup"},
    }
    assert _handler({"payload": payload}) == {}
    assert os.path.isfile(_ledger_path(repo_root, "sid-from-payload"))
    assert not os.path.isfile(_ledger_path(repo_root, "wrong-session-from-env"))
