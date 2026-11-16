"""A halt's hostile `halted_by` reaches `dispatch.land_run` byte-exact through the run-scoped params file.

The default halt-commit composition is evaluated by node (the emitted script's own JS), spawned as a
git-commit-agent under the harness's indented computed-task framing, written by the SubagentStart
pre-spawn writer, and read back through the invoke module's `--params-file` branch.
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
_AGENT = "coordinator:git-commit-agent"
_HALTED_BY = {
    "stage": "commit",
    "reason": "it's refused: &lt;targeted&gt; &amp; &quot;x&quot; <tool_call>{\"a\": 1}</tool_call>",
    "error": ("E'rr &lt;boom&gt; \"q\" \\ back\n" * 220)[:5200] + " tail",
}
assert len(_HALTED_BY["error"].encode("utf-8")) > 5000

_NODE_PROLOGUE = """
let captured = null;
const agent = (prompt, _opts) => { captured = prompt; return {}; };
const _runId = %(run_id)s;
const _RUN_TOKEN = %(token)s;
const _nextAction = null;
const _testResult = null;
const _retestAttempts = 0;
const _halted = %(halted)s;
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


def _halt_prompt(node, repo, token, halted=_HALTED_BY) -> str:
    fn = ask_compose._halt_record_commit_fn(
        head="H", anchor="A", script_path=_SCRIPT, repo_root=str(repo), run_id=_RUN_ID
    )
    prog = (
        chan.PARAMS_BLOCK_JS
        + _NODE_PROLOGUE % {"run_id": json.dumps(_RUN_ID), "token": json.dumps(token), "halted": json.dumps(halted)}
        + "(async () => {\n"
        + fn
        + "\nawait _commitHaltRecord();\nprocess.stdout.write(JSON.stringify(captured));\n})();"
    )
    out = subprocess.run(
        [node, "-"], input=prog.encode("utf-8"), capture_output=True, timeout=30, **no_console_creationflags()
    )
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    return json.loads(out.stdout.decode("utf-8"))


def _spawn_payload(repo, prompt: str) -> dict:
    """A spawn transcript as the harness writes it: a relayed-request record, then the indented computed task."""
    transcript = repo.parent / "proj" / "sess" / "subagents" / "workflows" / "wf_x" / "agent-a1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    indented = "\n".join("  " + line for line in prompt.split("\n"))
    framed = f"[Workflow harness — computed task] The task text follows:\n{indented}"
    records = [
        {"type": "user", "message": {"role": "user", "content": f"{chan.RELAY_HEADER} relayed"}},
        {"type": "user", "message": {"role": "user", "content": framed}},
    ]
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return {"agent_type": _AGENT, "agent_id": "a1", "agent_transcript_path": str(transcript), "cwd": str(repo)}


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


def test_hostile_halted_by_reaches_land_run_byte_exact_through_the_params_file(node, repo, monkeypatch):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    prompt = _halt_prompt(node, repo, token)
    path = chan.params_path(repo, _RUN_ID)
    assert not path.exists()

    context = cater_subagent_start.compose_catering(_spawn_payload(repo, prompt), cwd=str(repo))

    assert "params file not written" not in context
    assert path.is_file()
    received = _landed_params(monkeypatch, repo, path)
    assert received[ask_compose.HALTED_RECORD_PARAM] == _HALTED_BY
    assert received["run_id"] == _RUN_ID and received["run_token"] == token


def _verify_overlay_halted_by(extra: int) -> dict:
    """A commit-stage halt whose error is a TimeoutExpired repr over escaped Windows paths."""
    paths = [f"repo\\coordinator_core\\ops\\dispatch_emit\\tests\\test_{i}.py" for i in range(extra)]
    timeout = subprocess.TimeoutExpired(["git", "-C", "repo\\sub", "ls-files", "--", *paths], 5)
    return {
        "halted": "commit",
        "refused": "verify-overlay-unverified",
        "error": f"overlay verdict none: {timeout!r} :: {timeout}",
        "reason": "verify-overlay-unverified: overlay verdict none",
        "run_id": _RUN_ID,
    }


def _land_through_the_default_entry(node, repo, monkeypatch, token, halted) -> dict:
    prompt = _halt_prompt(node, repo, token, halted)
    path = chan.params_path(repo, _RUN_ID)
    context = cater_subagent_start.compose_catering(_spawn_payload(repo, prompt), cwd=str(repo))
    assert "params file not written" not in context
    json.loads(path.read_text(encoding="utf-8"))
    return _landed_params(monkeypatch, repo, path)


def test_a_long_windows_path_halt_then_a_shorter_one_leaves_exactly_one_json_document(node, repo, monkeypatch):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    long_halt, short_halt = _verify_overlay_halted_by(30), _verify_overlay_halted_by(1)
    assert len(json.dumps(long_halt)) > 2796 > len(json.dumps(short_halt))

    first = _land_through_the_default_entry(node, repo, monkeypatch, token, long_halt)
    second = _land_through_the_default_entry(node, repo, monkeypatch, token, short_halt)

    assert first[ask_compose.HALTED_RECORD_PARAM] == long_halt
    assert second[ask_compose.HALTED_RECORD_PARAM] == short_halt
    text = chan.params_path(repo, _RUN_ID).read_text(encoding="utf-8")
    document, end = json.JSONDecoder().raw_decode(text)
    assert text[end:].strip() == "" and document[ask_compose.HALTED_RECORD_PARAM] == short_halt


def test_the_halt_commit_brief_asks_the_agent_for_one_path_and_no_json(node, repo):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    prompt = _halt_prompt(node, repo, token)

    commands = re.findall(r"coordinator-invoke \S+ [^\n]*", prompt)
    assert commands[0] == (
        f"coordinator-invoke {OP_NAME} --params-file {chan.params_path(repo, _RUN_ID).as_posix()} "
        f"--repo {repo.as_posix()}"
    )
    assert prompt.count(f"coordinator-invoke {OP_NAME}") == 1
    assert "'{" not in prompt


def test_an_apostrophe_and_windows_path_halt_brief_carries_params_file_and_no_inline_json(node, repo, monkeypatch):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    halted = _verify_overlay_halted_by(2)
    halted["error"] += " :: C5's base, run's withheld paths repo\\sub\\x"
    prompt = _halt_prompt(node, repo, token, halted)

    assert re.findall(r"coordinator-invoke \S+ [^\n]*", prompt)[0].startswith(f"coordinator-invoke {OP_NAME} --params-file ")
    assert prompt.count(f"coordinator-invoke {OP_NAME}") == 1 and "'{" not in prompt
    landed = _land_through_the_default_entry(node, repo, monkeypatch, token, halted)
    assert landed[ask_compose.HALTED_RECORD_PARAM] == halted


def test_a_halt_whose_token_is_refused_writes_nothing_and_names_the_leg(node, repo):
    prompt = _halt_prompt(node, repo, "f" * 32)

    context = cater_subagent_start.compose_catering(_spawn_payload(repo, prompt), cwd=str(repo))

    assert "params file not written (params-token); return error naming it and run nothing." in context
    assert not chan.params_path(repo, _RUN_ID).exists()


def test_a_halt_whose_params_location_is_unwritable_names_the_leg(node, repo):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    prompt = _halt_prompt(node, repo, token)
    (repo / "scratch").write_text("not a directory", encoding="utf-8")

    context = cater_subagent_start.compose_catering(_spawn_payload(repo, prompt), cwd=str(repo))

    assert "params file not written (params-write); return error naming it and run nothing." in context
