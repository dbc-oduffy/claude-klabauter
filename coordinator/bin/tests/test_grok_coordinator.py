"""grok-coordinator launches grok as a child and does not shadow a bare grok."""

import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "grok-coordinator.py"
spec = importlib.util.spec_from_file_location("grok_coordinator", BIN)
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


def test_interactive_argv_skips_permissions_and_does_not_resume():
    from coordinator_core.agent_pipe.model_tiers import GROK_EM_EFFORT, model_for_role

    argv = gc.interactive_argv("grok", ["--cwd", "/tmp/work"])
    assert argv[:2] == ["grok", "--dangerously-skip-permissions"]
    assert "-p" not in argv and "-r" not in argv
    assert argv[-2:] == ["--cwd", "/tmp/work"]
    assert argv[argv.index("--model") + 1] == model_for_role("grok", "em")
    assert argv[argv.index("--reasoning-effort") + 1] == GROK_EM_EFFORT


def test_interactive_argv_keeps_a_caller_model():
    argv = gc.interactive_argv("grok", ["--model", "grok-4.5", "--cwd", "/tmp/work"])
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == "grok-4.5"


def test_headless_argv_is_one_turn_without_resume():
    from coordinator_core.agent_pipe.model_tiers import model_for_role

    argv = gc.headless_argv("grok", "/work", "hello")
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("-p") + 1] == "hello"
    assert argv[argv.index("--cwd") + 1] == "/work"
    assert argv[argv.index("--model") + 1] == model_for_role("grok", "em")
    assert "-r" not in argv and "--fork-session" not in argv


def test_strip_coordinator_is_not_forwarded():
    assert gc.strip_coordinator(["--coordinator", "--cwd", "/tmp/work"]) == ["--cwd", "/tmp/work"]
    assert "--coordinator" not in gc.interactive_argv("grok", gc.strip_coordinator(["--coordinator"]))


def test_stop_hook_file_names_the_script(tmp_path):
    path = gc.ensure_stop_hook(tmp_path, command="/engine/bin/unimatrix-grok-stop.py")
    body = path.read_text(encoding="utf-8")
    assert path.parent.name == "hooks"
    assert "/engine/bin/unimatrix-grok-stop.py" in body
    assert "Stop" in body


def test_session_for_pid_matches_only_the_child():
    rows = [{"session_id": "aaa", "pid": 5}, {"session_id": "bbb", "pid": 9}]
    assert gc.session_for_pid(rows, 9)["session_id"] == "bbb"
    assert gc.session_for_pid(rows, 3) is None
    assert gc.session_for_pid({"nope": True}, 5) is None
