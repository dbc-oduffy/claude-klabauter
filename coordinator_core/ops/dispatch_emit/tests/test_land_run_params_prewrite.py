"""A composed commit brief, the SubagentStart pre-spawn write and `dispatch.land_run --params-file` agree.

The composed prompts are evaluated by node (the script's own JS), fed to `compose_catering` as a
git-commit-agent spawn, and the file the hook wrote is read back through the invoke module's
`--params-file` branch.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core import ipc
from coordinator_core.hooks import cater_subagent_start
from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.ops.dispatch_emit import ask_compose, run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process]

_RUN_ID = "ask-1"
_SCRIPT = "scratch/warp/a.workflow.mjs"
_HOSTILE = {"note": "it's", "quoted": 'say "hi"', "multiline": "a\nb", "mixed": "x'y \"z\"\nw {}"}
_AGENT = "coordinator:git-commit-agent"

_NODE_PROLOGUE = """
let captured = null;
const agent = (prompt, _opts) => { captured = prompt; return {}; };
const _runId = %(run_id)s;
const _RUN_TOKEN = %(token)s;
const _nextAction = { kind: 'terminal_commit', params: %(hostile)s };
const _testResult = null;
const _retestAttempts = 0;
const _halted = %(hostile)s;
const _consumedReturns = [];
const _planRel = null;
const _refireNextAction = () => {};
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture(scope="module")
def node():
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node unavailable")
    return exe


def _evaluate(node, js_call: str, tail: str, token: str, hostile: dict) -> str:
    prog = (
        chan.PARAMS_BLOCK_JS
        + _NODE_PROLOGUE % {"run_id": json.dumps(_RUN_ID), "token": json.dumps(token), "hostile": json.dumps(hostile)}
        + "(async () => {\n"
        + tail.replace("__CALL__", js_call)
        + "\nprocess.stdout.write(JSON.stringify(captured));\n})();"
    )
    out = subprocess.run(
        [node, "-"], input=prog.encode("utf-8"), capture_output=True, timeout=30, **no_console_creationflags()
    )
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    return json.loads(out.stdout.decode("utf-8"))


def _commit_prompt(node, repo, token, hostile=_HOSTILE) -> str:
    call = ask_compose._commit_stage_call(head="H", anchor="A", repo_root=str(repo), run_id=_RUN_ID, script_path=_SCRIPT)
    return _evaluate(node, call, "__CALL__;", token, hostile)


def _halt_prompt(node, repo, token, hostile=_HOSTILE) -> str:
    fn = ask_compose._halt_record_commit_fn(
        head="H", anchor="A", script_path=_SCRIPT, repo_root=str(repo), run_id=_RUN_ID
    )
    return _evaluate(node, fn, "__CALL__\nawait _commitHaltRecord();", token, hostile)


def _spawn_payload(repo, prompt, agent_type=_AGENT) -> dict:
    transcript = repo.parent / "proj" / "sess" / "subagents" / "workflows" / "wf_x" / "agent-a1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": "user", "message": {"role": "user", "content": prompt}}
    transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return {
        "agent_type": agent_type,
        "agent_id": "a1",
        "agent_transcript_path": str(transcript),
        "cwd": str(repo),
    }


def _landed_params(monkeypatch, repo, path) -> dict:
    """The params `dispatch.land_run` receives when invoked with `--params-file <path>`."""
    seen = []

    def probe(params, repo_root=None):
        seen.append(params)
        return {"ok": True}

    monkeypatch.setitem(ipc._REGISTRY, OP_NAME, probe)
    stdout, stderr, code = _dispatch_argv(
        [OP_NAME, "--params-file", str(path), "--repo", str(repo)], str(repo), allow_warm=False
    )
    assert code == 0, stderr or stdout
    assert len(seen) == 1
    return seen[0]


def _carrier(params: dict, prompt_for) -> dict:
    """Where the hostile values ride: top level for the commit stage, the halted record for a halt."""
    return params if prompt_for is _commit_prompt else params[ask_compose.HALTED_RECORD_PARAM]


def _holds_hostile(carrier: dict) -> bool:
    return all(carrier.get(key) == value for key, value in _HOSTILE.items())


@pytest.mark.parametrize("prompt_for", [_commit_prompt, _halt_prompt], ids=["commit", "halt-commit"])
def test_hostile_params_round_trip_from_composed_prompt_through_land_run(node, repo, monkeypatch, prompt_for):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    prompt = prompt_for(node, repo, token)
    path = chan.params_path(repo, _RUN_ID)
    assert not path.exists()

    context = cater_subagent_start.compose_catering(_spawn_payload(repo, prompt), cwd=str(repo))

    assert "params file not written" not in context
    assert path.is_file()
    expected = chan.prompt_params(prompt)
    assert expected is not None
    assert _holds_hostile(_carrier(expected, prompt_for))
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    received = _landed_params(monkeypatch, repo, path)
    assert _holds_hostile(_carrier(received, prompt_for))
    assert received == expected


@pytest.mark.parametrize("prompt_for", [_commit_prompt, _halt_prompt], ids=["commit", "halt-commit"])
def test_composed_prompt_names_only_the_params_file_command(node, repo, prompt_for):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    prompt = prompt_for(node, repo, token)
    commands = re.findall(r"coordinator-invoke \S+ [^\n]*", prompt)
    assert commands[0] == (
        f"coordinator-invoke {OP_NAME} --params-file {chan.params_path(repo, _RUN_ID).as_posix()} "
        f"--repo {repo.as_posix()}"
    )
    assert prompt.count(f"coordinator-invoke {OP_NAME}") == 1
    assert "'{" not in prompt
    assert "already written to" in prompt


def test_a_refused_token_writes_nothing_and_tells_the_agent_to_stop(node, repo):
    prompt = _commit_prompt(node, repo, "f" * 32)
    path = chan.params_path(repo, _RUN_ID)

    context = cater_subagent_start.compose_catering(_spawn_payload(repo, prompt), cwd=str(repo))

    assert "params file not written (params-token); return error naming it and run nothing." in context
    assert not path.exists()


def test_a_non_commit_agent_spawn_writes_nothing(node, repo):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    prompt = _commit_prompt(node, repo, token)

    context = cater_subagent_start.compose_catering(
        _spawn_payload(repo, prompt, agent_type="coordinator:executor"), cwd=str(repo)
    )

    assert "params file not written" not in context
    assert not chan.params_path(repo, _RUN_ID).exists()


def test_an_unwritable_params_location_reaches_the_agent_before_any_shell_call(node, repo):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    prompt = _commit_prompt(node, repo, token)
    blocker = repo / "scratch"
    blocker.write_text("not a directory", encoding="utf-8")

    context = cater_subagent_start.compose_catering(_spawn_payload(repo, prompt), cwd=str(repo))

    assert "params file not written (params-write); return error naming it and run nothing." in context


@pytest.mark.parametrize("bad_run_id", ["../x", "a/b", "", "-x"])
def test_an_unbound_run_id_refuses_at_compose_time(repo, bad_run_id):
    with pytest.raises(ask_compose.AskComposeRefused):
        ask_compose._commit_stage_call(head="H", anchor="A", repo_root=str(repo), run_id=bad_run_id)
    with pytest.raises(ask_compose.AskComposeRefused):
        ask_compose._halt_record_commit_fn(
            head="H", anchor="A", script_path=_SCRIPT, repo_root=str(repo), run_id=bad_run_id
        )


def test_a_missing_pre_spawn_writer_refuses_at_compose_time(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(chan, "PRE_SPAWN_WRITER_FILE", tmp_path / "no-such-hook.py")
    with pytest.raises(ask_compose.AskComposeRefused):
        ask_compose._commit_stage_call(head="H", anchor="A", repo_root=str(repo), run_id=_RUN_ID)
    with pytest.raises(ask_compose.AskComposeRefused):
        ask_compose._halt_record_commit_fn(
            head="H", anchor="A", script_path=_SCRIPT, repo_root=str(repo), run_id=_RUN_ID
        )


def test_the_shipped_hook_is_the_bound_writer(repo):
    assert chan.params_writer_bound(repo, _RUN_ID)
    ask_compose._commit_stage_call(head="H", anchor="A", repo_root=str(repo), run_id=_RUN_ID)
