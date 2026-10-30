"""hooks.workflow_stall_stop op, the drive-root leg, and the Stop fan-in leg-list parity pin."""

from __future__ import annotations

import asyncio
import json
import os
import types

import pytest

from coordinator_core.hooks import postuse_advisory_dispatch as pad
from coordinator_core.hooks import stop_dispatch as sd
from coordinator_core.ipc import _REGISTRY

SID = "sess-1"
NOW = 2_000_000_000.0
OP = "hooks.workflow_stall_stop"


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(pad, "_managed_tmpdir", lambda: str(d))
    monkeypatch.setattr(sd, "time", types.SimpleNamespace(time=lambda: NOW))
    return d


def _stale_run(tmp_path, tdir):
    run_dir = tmp_path / "r1"
    (run_dir / "subagents").mkdir(parents=True)
    journal = run_dir / "journal.jsonl"
    journal.write_text(
        json.dumps({"type": "started", "agentId": "a1", "key": "k1", "label": "scout"}) + "\n",
        encoding="utf-8",
    )
    transcript = run_dir / "subagents" / "agent-a1.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    for f in (journal, transcript):
        os.utime(f, (NOW - 7200, NOW - 7200))
    rec = {
        "run_id": "wf_1", "scriptPath": "scripts/x.workflow.mjs",
        "args": {"repoRoot": str(tdir)}, "fired_at": NOW - 3600,
        "session_id": SID, "task_id": "t1", "tool_use_id": "tu1",
        "transcript_dir": str(run_dir),
    }
    (tdir / f"workflow-run-{SID}-t1.json").write_text(json.dumps(rec), encoding="utf-8")


def _blocked(result):
    return sd._extract_advisory(result)[0]


def _call(params):
    handler = _REGISTRY[OP]
    result = handler(params)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return result


def test_op_blocks_on_stale_run(tmp_path, tdir):
    _stale_run(tmp_path, tdir)
    result = _call({"payload": {"session_id": SID, "transcript_path": "", "stop_hook_active": False}})
    assert result["decision"] == "block"


def test_op_flat_params_also_block(tmp_path, tdir):
    _stale_run(tmp_path, tdir)
    result = _call({"session_id": SID, "transcript_path": "", "stop_hook_active": False})
    assert result["decision"] == "block"


def test_op_chained_stop_downgrades_to_advisory(tmp_path, tdir):
    _stale_run(tmp_path, tdir)
    result = _call({"payload": {"session_id": SID, "stop_hook_active": True}})
    assert not _blocked(result)
    assert result["hookSpecificOutput"]["additionalContext"]


def test_op_passes_with_no_stale_run(tdir):
    result = _call({"payload": {"session_id": SID, "transcript_path": "", "stop_hook_active": False}})
    assert not _blocked(result)
    assert sd._extract_advisory(result) == (False, None)


def test_fan_in_calls_the_same_function():
    assert dict(sd._FOLDED_LEGS)["workflow_stall_resume"]
    assert _REGISTRY[OP] is sd._workflow_stall_stop_op


def test_stop_dispatch_composes_exactly_these_legs_in_order():
    assert tuple(n for n, _ in sd._FOLDED_LEGS) == (
        "runtime_tripwire_em_check",
        "watchdog_undischarged_next_move",
        "guard_manufactured_blocker",
        "guard_terminal_review",
        "guard_drive_root_new_dir",
        "stop_em_report_altitude",
        "nudge_harness_directive_dispatch",
        "nudge_unrouted_sizing",
        "workflow_stall_resume",
        "deferred_release_drain",
        "pm_directive_unmapped",
    )
    assert sd.PRODUCER_LEG_NAMES == ("receiver_state_sensor", "group_em_park_spool")


@pytest.fixture
def drive(tmp_path, monkeypatch):
    root = tmp_path / "drive"
    root.mkdir()
    (root / "existing").mkdir()
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOTS", str(root))
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOT_BASELINE", str(tmp_path / "base.json"))
    monkeypatch.delenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", raising=False)
    monkeypatch.setattr(sd, "record_fire", lambda *a, **k: "nonce1")
    return root


def _drive_params(tmp_path, **extra):
    return {"payload": {"cwd": str(tmp_path), "session_id": SID, **extra}}


def test_drive_root_first_sight_seeds_silently(tmp_path, drive):
    res = sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path))
    assert not _blocked(res)


def test_drive_root_new_dir_blocks_once(tmp_path, drive):
    sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path))
    (drive / "stray").mkdir()
    res = sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path))
    assert res["decision"] == "block"
    assert "stray" in res["reason"]
    again = sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path))
    assert not _blocked(again)


def test_drive_root_override_and_checkout_and_chained_stop_pass(tmp_path, drive, monkeypatch):
    sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path))
    (drive / "placed").mkdir()
    (drive / "placed" / ".git").mkdir()
    res = sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path))
    assert not _blocked(res)
    (drive / "stray2").mkdir()
    chained = sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path, stop_hook_active=True))
    assert not _blocked(chained)
    monkeypatch.setenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", "1")
    res = sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path))
    assert not _blocked(res)


def test_drive_root_override_is_caller_keyed_not_ambient(tmp_path, drive, monkeypatch):
    sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path))
    (drive / "stray3").mkdir()
    monkeypatch.setenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", "1")
    res = sd._guard_drive_root_new_dir_handler(_drive_params(tmp_path, env={"PATH": "x"}))
    assert res["decision"] == "block"
    assert "stray3" in res["reason"]
