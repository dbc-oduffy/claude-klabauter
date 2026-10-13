"""Pins the warp --ask contract names and the StageManifest round trip."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit import commit_request


def _manifest():
    rows = (
        c.ManifestRow("C1", "executor", "sonnet", "scratch/warp/r/C1.md", ("a.py", "b.py"), 0),
        c.ManifestRow("C2", "executor", "opus", "scratch/warp/r/C2.md", (), 1, ("C1",)),
    )
    return c.StageManifest("scratch/warp/r", rows, ("a.py",), "scratch/warp/r/req.json")


def test_manifest_round_trip():
    m = _manifest()
    assert c.StageManifest.from_json(json.loads(json.dumps(m.to_json()))) == m


def test_phase_order_and_names():
    assert c.ASK_PHASES == ("size", "gate", "accept", "plan", "stage", "execute", "review")
    assert (c.OP_ASK_GATE, c.OP_ASK_STAGE) == ("dispatch.ask_gate", "dispatch.ask_stage")
    assert (c.HALT_ROOM, c.HALT_TOUCHPOINT, c.HALT_REFUSAL) == ("room", "touchpoint", "refusal")


def test_size_then_plan_names():
    assert c.STOP_AFTER_PLAN == "plan"
    assert c.SIZING_SCOUT_AGENT_TYPE == "coordinator:sizing-scout"
    assert c.NO_PLAN_HAND_OFF_FLAG == "--no-plan-hand-off"
    assert c.PM_EXIT_QUESTION_KEY == "pm_exit_question"
    assert c.STOP_AFTER_PLAN in c.ASK_PHASES


def test_marker_distinct_from_commit_request():
    assert c.ASK_MANIFEST_MARKER != commit_request.MARKER_PREFIX
    assert not c.ASK_MANIFEST_MARKER.startswith(commit_request.MARKER_PREFIX)
    assert not commit_request.MARKER_PREFIX.startswith(c.ASK_MANIFEST_MARKER)


def test_gate_verdict_to_json():
    v = c.GateVerdict(None, {"kind": c.HALT_ROOM, "reason": "x"})
    assert v.to_json() == {"arm": None, "halt": {"kind": "room", "reason": "x"}, "baton": None}


def test_gate_verdict_to_json_omits_acceptance_when_unset():
    assert "acceptance" not in c.GateVerdict("s", None).to_json()
    stamp = {"by": "engine-size-rule"}
    assert c.GateVerdict("s", None, acceptance=stamp).to_json()["acceptance"] == stamp


def test_record_dir_root_is_state_warp_and_run_dir_root_unchanged():
    assert c.RECORD_DIR_ROOT.endswith("/warp") and c.RECORD_DIR_ROOT.startswith("state")
    assert c.RUN_DIR_ROOT == "scratch/warp"


def test_record_dir_no_directory_on_disk(tmp_path):
    assert c.record_dir(tmp_path, "r1") == (tmp_path / c.RECORD_DIR_ROOT / "r1").resolve()


def test_record_dir_prefers_state_when_present(tmp_path):
    (tmp_path / c.RECORD_DIR_ROOT / "r1").mkdir(parents=True)
    (tmp_path / "scratch/warp/r1").mkdir(parents=True)
    (tmp_path / "scratch/warp/r1/manifest.json").write_text("{}")
    assert c.record_dir(tmp_path, "r1") == (tmp_path / c.RECORD_DIR_ROOT / "r1").resolve()


def test_record_dir_scratch_with_only_briefs_and_commit_request_is_state(tmp_path):
    scratch = tmp_path / "scratch/warp/r1"
    (scratch / "briefs").mkdir(parents=True)
    (scratch / "commit-request.txt").write_text("x")
    assert c.record_dir(tmp_path, "r1") == (tmp_path / c.RECORD_DIR_ROOT / "r1").resolve()


def test_record_dir_scratch_record_file_selects_scratch(tmp_path):
    scratch = tmp_path / "scratch/warp/r1"
    scratch.mkdir(parents=True)
    (scratch / c.RELAYED_ASK_FILE).write_text("x")
    assert c.record_dir(tmp_path, "r1") == scratch.resolve()


def test_record_dir_scratch_spine_only_selects_scratch(tmp_path):
    scratch = tmp_path / "scratch/warp/r1"
    scratch.mkdir(parents=True)
    (scratch / "r1.spine.md").write_text("x")
    assert c.record_dir(tmp_path, "r1") == scratch.resolve()


def test_record_dir_unsafe_run_id_raises(tmp_path):
    for bad in ("..", "a/b", ""):
        with pytest.raises(ValueError):
            c.record_dir(tmp_path, bad)
