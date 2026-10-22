"""The terminal_commit command line holds no escaped backslash pair a retype or unquoted heredoc can halve."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, sibling_split, wake_digest
from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path, render_params_block
from coordinator_core.win_portability import no_console_creationflags

PARAMS = {
    "run_id": "r1",
    "run_token": "tok",
    "script_path": r"C:\work\.coordinator-local\y.md",  # abs-path-ok: escape fixture
    "note": 'quote " and \\n literal and newline\n',
}


SIDECAR_PARAMS = {
    "run_id": "r1",
    "run_token": "tok",
    "script_path": r"D:\scratch\run\y.mjs",  # abs-path-ok: escape fixture
    "wave_sidecar_paths": [r"E:\x\.coordinator-local\s.md"],  # abs-path-ok: escape fixture
}
OUT = r"C:\Users\x\tasks\abc.output"  # abs-path-ok: task-output fixture
WITH_PATH = {**SIDECAR_PARAMS, "task_output_path": OUT}
HOSTILE_PATH = {**SIDECAR_PARAMS, "task_output_path": "/t/a&b<c>'d.output"}
ALL_PARAMS = [PARAMS, SIDECAR_PARAMS, WITH_PATH, HOSTILE_PATH]


@pytest.mark.parametrize("params", ALL_PARAMS)
def test_python_cli_has_no_html_escapable_character_and_no_heredoc(params):
    cli = sibling_split._cli(params)
    assert not set(cli) & set("<>&")
    assert "\n" not in cli and "JSON" not in cli


def test_python_cli_names_the_task_output_path_as_one_quoted_argument():
    assert sibling_split._cli(WITH_PATH) == (
        f"coordinator-invoke dispatch.terminal_commit --from-task-output '{OUT}'"
    )


@pytest.mark.parametrize("params", [PARAMS, SIDECAR_PARAMS, HOSTILE_PATH])
def test_a_missing_or_unquotable_path_yields_the_placeholder_token(params):
    assert sibling_split._cli(params).endswith(" --from-task-output TASK_OUTPUT_PATH")


def _halve(text: str) -> str:
    """The lossy hop: every escaped backslash pair collapses to one backslash."""
    return text.replace("\\\\", "\\")


def test_no_params_yields_no_line():
    assert sibling_split._cli(None) is None and sibling_split._cli({}) is None


_RUN_ID = "ask-pin"
_SCRIPT = "scratch/warp/pin.workflow.mjs"
LEDGER_PARAMS = {
    "ledger_verdict": "inline_review",
    "wave_sidecar_paths": [r"E:\x\.coordinator-local\review.md"],  # abs-path-ok: escape fixture
    "note": "C1's half",
}


@pytest.fixture
def commit_stage(tmp_path, monkeypatch):
    from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as roster_guard
    from coordinator_core.bash_guards import block_subagent_commit as guard

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(roster_guard, "is_confined_by_roster_absence", lambda effective_type: False)
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: "")
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    return tmp_path, run_token.mint(tmp_path, _RUN_ID, _SCRIPT)


def _agent_bash(cmd: str, cwd: Path) -> str:
    return json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": "s",
            "cwd": str(cwd),
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:git-commit-agent",
        }
    )


_HARNESS_HEADER = "[Workflow harness — computed task] The task text below was computed at runtime."


def _spawn_transcript(root: Path, prompt: str) -> Path:
    """First record of a fixture Workflow-spawn transcript: the harness-indented prompt."""
    indented = _HARNESS_HEADER + "\n" + "\n".join("  " + ln for ln in prompt.splitlines())
    path = root / "proj" / "sess" / "subagents" / "workflows" / "wf_pin" / "agent-pin.jsonl"
    path.parent.mkdir(parents=True)
    record = {"type": "user", "message": {"role": "user", "content": indented}}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _commit_prompt(root: Path, params: dict) -> str:
    """The commit-stage prompt as the emitter composes it: literal command plus params block."""
    cmd = f"coordinator-invoke dispatch.terminal_commit --params-file {params_path(root, _RUN_ID).as_posix()}"
    return f"phase: commit\n\n{cmd}\n\nThe engine reads the params block below.\n\n{render_params_block(params)}\n"


def _typed_command(prompt: str) -> str:
    lines = [ln for ln in prompt.splitlines() if ln.startswith("coordinator-invoke ")]
    assert len(lines) == 1
    return lines[0]


def _bash_payload(cmd: str, cwd: Path, transcript: Path) -> str:
    payload = json.loads(_agent_bash(cmd, cwd))
    payload["transcript_path"] = str(transcript)
    return json.dumps(payload)


@pytest.mark.parametrize("params", [PARAMS, SIDECAR_PARAMS, LEDGER_PARAMS])
def test_backslash_params_reach_terminal_commit_through_the_commit_stage(commit_stage, params):
    from coordinator_core.bash_guards import dispatch

    root, token = commit_stage
    params = {**params, "run_id": _RUN_ID, "run_token": token, "script_path": _SCRIPT, "incomplete_chunks": []}
    prompt = _commit_prompt(root, params)
    transcript = _spawn_transcript(root, prompt)

    typed = _typed_command(prompt)
    assert "{" not in typed and "\\" not in typed
    assert _halve(typed) == typed

    out = dispatch.evaluate_payload_json(_bash_payload(typed, root, transcript))
    if isinstance(out, dict):
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny", out
        assert "updatedInput" not in out["hookSpecificOutput"]

    file_arg = typed.rsplit(" ", 1)[1]
    reached = json.loads(Path(file_arg).read_text(encoding="utf-8"))
    assert reached == params


def test_altered_run_token_is_denied_and_writes_no_file(commit_stage):
    from coordinator_core.bash_guards import dispatch

    root, token = commit_stage
    params = {**SIDECAR_PARAMS, "run_id": _RUN_ID, "run_token": token + "x", "script_path": _SCRIPT}
    prompt = _commit_prompt(root, params)
    transcript = _spawn_transcript(root, prompt)

    out = dispatch.evaluate_payload_json(_bash_payload(_typed_command(prompt), root, transcript))
    assert isinstance(out, dict), out
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    assert not params_path(root, _RUN_ID).exists()


def test_ask_compose_still_wires_the_shared_cli_helper_not_a_local_copy():
    from coordinator_core.ops.dispatch_emit import ask_compose

    src = Path(ask_compose.__file__).read_text(encoding="utf-8")
    assert "_terminalCommitCli(" in src
    assert src.count("function _terminalCommitCli") == 0
    assert "TERMINAL_COMMIT_CLI_HELPER_JS" in src


def _js_cli(params) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    prog = (
        wake_digest.TERMINAL_COMMIT_CLI_HELPER_JS
        + f"\nprocess.stdout.write(_terminalCommitCli({json.dumps(params)}));"
    )
    out = subprocess.run(
        [node, "-"], input=prog, capture_output=True, text=True, timeout=30, encoding="utf-8",
        **no_console_creationflags(),
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.spawns_process
@pytest.mark.parametrize("params", ALL_PARAMS)
def test_js_and_python_helpers_agree(params):
    assert _js_cli(params) == sibling_split._cli(params)


@pytest.mark.spawns_process
@pytest.mark.parametrize("params", ALL_PARAMS)
def test_js_line_has_no_html_escapable_character(params):
    assert not set(_js_cli(params)) & set("<>&")


def test_ask_compose_in_run_getter_routes_through_the_helper():
    from coordinator_core.ops.dispatch_emit import ask_compose

    src = Path(ask_compose.__file__).read_text(encoding="utf-8")
    assert "? _terminalCommitCli(this.next_action.params) : null" in src
