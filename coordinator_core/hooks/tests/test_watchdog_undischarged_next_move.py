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


def test_stop_leg_blocks_via_claude_home_from_payload_env(tmp_path) -> None:
    """No coordinator.local.md in the repo; the Stop payload's env carries
    CLAUDE_HOME pointing at an identity file reading "default". This pins that
    the watchdog passes the payload env through to the posture resolver."""
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)

    claude_home = tmp_path / "claude-home"
    identity_dir = claude_home / ".claude"
    identity_dir.mkdir(parents=True)
    with open(identity_dir / "coordinator-identity.yaml", "w", encoding="utf-8") as fh:
        fh.write("engagement_posture: default\n")

    session_id = "sid-stop-block-claude-home"
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
        "env": {"CLAUDE_HOME": str(claude_home)},
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


# per `_ROUTE_TERMINAL` entry, one for the appetite/post-size-prompt exemption,


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


def test_touched_txt_paths_reads_touch_record_jsonl_not_touched_txt(tmp_path) -> None:
    """P143-T36: `_touched_txt_paths` is the sibling leg to
    `_touch_record_jsonl_paths` in `_newest_touched_sizing_path`'s
    source-then-recency fallback — it must read `touch-record.jsonl` too, not
    the retired `touched.txt`. No non-test writer of `touched.txt` exists, so
    a session dir carrying only `touch-record.jsonl` (the real-session shape)
    used to read as empty from this leg."""
    from coordinator_core.hooks.watchdog_undischarged_next_move import _touched_txt_paths

    repo_root = _make_repo(tmp_path)
    session_id = "sid-touched-txt-paths"
    rel_path = "state/sizings/thing.yaml"
    session_dir = os.path.join(repo_root, ".git", "coordinator-sessions", session_id)
    _write_touch_record(repo_root, session_id, rel_path)
    assert not os.path.exists(os.path.join(session_dir, "touched.txt"))

    assert _touched_txt_paths(session_dir) == [rel_path]


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
        {"schema": 1, "session_id": session_id, "op": "open"},
    ]
    with open(_intake_path(repo_root, session_id), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

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

    assert not os.path.isfile(_intake_path(repo_root, session_id))
    assert not os.path.isfile(_ledger_path(repo_root, session_id))


def test_session_id_is_read_from_payload_never_from_environment(tmp_path, monkeypatch) -> None:
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
