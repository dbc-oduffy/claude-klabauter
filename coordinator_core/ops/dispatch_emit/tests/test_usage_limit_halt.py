"""halt_run_on_usage_limit over tmp_path run dirs built from the recorded usage-limit hit."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.halt_refire import HALT_USAGE_LIMIT, is_refire_next_action
from coordinator_core.ops.dispatch_emit.usage_limit_halt import halt_run_on_usage_limit

FIXTURE = json.loads(
    (Path(__file__).parents[3] / "workflow_watch" / "tests" / "fixtures" / "usage_limit_hit.json")
    .read_text(encoding="utf-8"))
RESETS_AT = FIXTURE["quotaLimits"]["resetsAt"]
RATE_LIMIT_TURN = {"type": "assistant", "message": {"content": [{"type": "text", "text": "limit"}]},
                   "isApiErrorMessage": True, **FIXTURE}
NORMAL_TURN = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
SIZING = "state/sizings/2026-10-29-x.yaml"


def _run(tmp_path, events, transcripts):
    run_dir = tmp_path / "runs" / "wf_abc"
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    for agent_id, turns in transcripts.items():
        (run_dir / f"agent-{agent_id}.jsonl").write_text(
            "\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")
    record_dir = tmp_path / "scratch" / "warp" / "ask-20261029T100000-abcdef"
    record_dir.mkdir(parents=True)
    return record_dir, str(run_dir / "journal.jsonl")


def _started(agent_id):
    return {"type": "started", "agentId": agent_id, "label": f"leg:{agent_id}", "key": f"v2:{agent_id}"}


def _halt(record_dir, journal, now):
    return halt_run_on_usage_limit(record_dir, journal, sizing_rel=SIZING, script_path="wf.js", now=now)


def test_future_reset_writes_usage_limit_marker(tmp_path):
    record_dir, journal = _run(tmp_path, [_started("a1")], {"a1": [NORMAL_TURN, RATE_LIMIT_TURN]})
    target = _halt(record_dir, journal, RESETS_AT - 3600.0)
    assert target == record_dir / "halted.json"
    marker = json.loads(target.read_text(encoding="utf-8"))
    assert marker["status"] == "halted"
    assert marker["halted"] == HALT_USAGE_LIMIT
    assert marker["sizing_rel"] == SIZING
    assert "leg:a1" in marker["reason"] and "2026-10-06T18:40:00Z" in marker["reason"]
    assert is_refire_next_action(marker["next_action"])


def test_past_reset_still_writes_marker(tmp_path):
    record_dir, journal = _run(tmp_path, [_started("a1")], {"a1": [RATE_LIMIT_TURN]})
    assert _halt(record_dir, journal, RESETS_AT + 7200.0) is not None
    assert halted_run.read_strict(record_dir)["halted"] == HALT_USAGE_LIMIT


def test_no_sizing_writes_marker_without_next_action(tmp_path):
    record_dir, journal = _run(tmp_path, [_started("a1")], {"a1": [RATE_LIMIT_TURN]})
    assert halt_run_on_usage_limit(record_dir, journal, sizing_rel=None, script_path=None, now=RESETS_AT) is not None
    assert "next_action" not in halted_run.read_strict(record_dir)


def test_existing_marker_of_any_status_is_untouched(tmp_path):
    for status in ("halted", "resumed", "landed"):
        sub = tmp_path / status
        sub.mkdir()
        record_dir, journal = _run(sub, [_started("a1")], {"a1": [RATE_LIMIT_TURN]})
        marker = {"status": status, "halted": "design_review", "sizing_rel": SIZING}
        (record_dir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
        assert _halt(record_dir, journal, RESETS_AT + 1) is None
        assert json.loads((record_dir / "halted.json").read_text(encoding="utf-8")) == marker


def test_unreadable_marker_is_untouched(tmp_path):
    record_dir, journal = _run(tmp_path, [_started("a1")], {"a1": [RATE_LIMIT_TURN]})
    (record_dir / "halted.json").write_text("{not json", encoding="utf-8")
    assert _halt(record_dir, journal, RESETS_AT + 1) is None
    assert (record_dir / "halted.json").read_text(encoding="utf-8") == "{not json"


def test_no_rate_limit_leg_writes_nothing(tmp_path):
    record_dir, journal = _run(tmp_path, [_started("a1")], {"a1": [NORMAL_TURN]})
    assert _halt(record_dir, journal, RESETS_AT + 1) is None
    assert not (record_dir / "halted.json").exists()


def test_settled_leg_writes_nothing(tmp_path):
    events = [_started("a1"), {"type": "result", "agentId": "a1", "key": "v2:a1", "result": "ok"}]
    record_dir, journal = _run(tmp_path, events, {"a1": [RATE_LIMIT_TURN]})
    assert _halt(record_dir, journal, RESETS_AT + 1) is None
    assert not (record_dir / "halted.json").exists()


def test_require_all_legs_refuses_when_one_leg_is_still_working(tmp_path):
    record_dir, journal = _run(
        tmp_path, [_started("a1"), _started("a2")], {"a1": [RATE_LIMIT_TURN], "a2": [NORMAL_TURN]})
    assert halt_run_on_usage_limit(
        record_dir, journal, sizing_rel=SIZING, script_path="wf.js", now=RESETS_AT, require_all_legs=True) is None
    assert not (record_dir / "halted.json").exists()
    assert _halt(record_dir, journal, RESETS_AT) is not None


def test_rejected_quota_without_429_error_writes_nothing(tmp_path):
    no_error = {k: v for k, v in RATE_LIMIT_TURN.items() if k not in ("error", "apiErrorStatus")}
    record_dir, journal = _run(tmp_path, [_started("a1")], {"a1": [no_error]})
    assert _halt(record_dir, journal, RESETS_AT + 1) is None
