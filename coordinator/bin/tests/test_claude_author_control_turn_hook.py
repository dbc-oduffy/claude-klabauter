"""Tests for the claude-author control-socket turn hook script."""
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "lib" / "claude_author_control" / "turn_hook.py"
sys.path.insert(0, str(HOOK.parent.parent))
from claude_author_control import protocol  # noqa: E402


def _run(tmp_path, payload, raw=None):
    stdin = raw if raw is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK), str(tmp_path)], input=stdin,
        capture_output=True, text=True, timeout=30,
    )


def _turn(tmp_path):
    return json.loads((tmp_path / "turn.json").read_text(encoding="utf-8"))


def test_file_name_matches_protocol():
    assert protocol.TURN_FILE == "turn.json"


def test_session_start(tmp_path):
    r = _run(tmp_path, {"hook_event_name": "SessionStart", "session_id": "abc"})
    assert (r.returncode, r.stdout) == (0, "")
    t = _turn(tmp_path)
    assert tuple(t) == protocol.TURN_FIELDS
    assert (t["session_id"], t["turn_active"], t["event"]) == ("abc", False, "SessionStart")
    assert t["ts"].endswith("Z")


def test_prompt_then_stop(tmp_path):
    _run(tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "abc"})
    assert _turn(tmp_path)["turn_active"] is True
    r = _run(tmp_path, {"hook_event_name": "Stop", "session_id": "abc"})
    assert r.returncode == 0 and r.stdout == ""
    t = _turn(tmp_path)
    assert (t["turn_active"], t["event"]) == (False, "Stop")
    assert not list(tmp_path.glob("*.tmp"))


def test_session_id_carried_when_absent(tmp_path):
    _run(tmp_path, {"hook_event_name": "SessionStart", "session_id": "abc"})
    _run(tmp_path, {"hook_event_name": "UserPromptSubmit"})
    assert _turn(tmp_path)["session_id"] == "abc"


def test_malformed_stdin_exits_zero(tmp_path):
    r = _run(tmp_path, None, raw="{not json")
    assert (r.returncode, r.stdout) == (0, "")
    assert not (tmp_path / "turn.json").exists()


def test_unknown_event_ignored(tmp_path):
    r = _run(tmp_path, {"hook_event_name": "PreToolUse", "session_id": "x"})
    assert r.returncode == 0 and not (tmp_path / "turn.json").exists()


def test_missing_instance_dir_exits_zero(tmp_path):
    r = subprocess.run(
        [sys.executable, str(HOOK), str(tmp_path / "nope")],
        input=json.dumps({"hook_event_name": "Stop"}), capture_output=True, text=True, timeout=30,
    )
    assert (r.returncode, r.stdout) == (0, "")


def test_no_args_exits_zero():
    r = subprocess.run([sys.executable, str(HOOK)], input="{}", capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0
