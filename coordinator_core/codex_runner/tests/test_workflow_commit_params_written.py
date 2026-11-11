"""run_agent writes the terminal-commit params file for a git-commit-agent child on a hookless backend."""
import json
import subprocess
import sys

import pytest

from coordinator_core.codex_runner import workflow
from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

_SCRIPT = "scratch/warp/a.workflow.mjs"
_RUN_ID = "ask-1"
_STUB = "import sys\nsys.stdout.write('{\"text\": \"{}\", \"stopReason\": \"end_turn\"}')\n"


@pytest.fixture
def stub(tmp_path, monkeypatch):
    script = tmp_path / "stub_grok.py"
    script.write_text(_STUB, encoding="utf-8")
    existed_at_spawn = []

    def spawn(argv, prompt, *, env, repo_root, stdout, stderr, **_kw):
        existed_at_spawn.append(chan.params_path(repo_root, _RUN_ID).exists())
        child = subprocess.Popen([sys.executable, str(script)], env=env, stdout=stdout, stderr=stderr, cwd=repo_root)
        child.wait()
        return child

    monkeypatch.setattr(workflow, "_run_admitted", spawn)
    monkeypatch.setattr(workflow, "_persona", lambda _agent_type: "PERSONA TEXT")
    monkeypatch.setattr(workflow, "resolve_grok", lambda: [str(script)])
    return existed_at_spawn


def _prompt(repo, blocks=1):
    token = run_token.mint(repo, _RUN_ID, _SCRIPT)
    block = chan.render_params_block({"run_id": _RUN_ID, "run_token": token, "script_path": _SCRIPT})
    return "Commit.\n" + "\n".join([block] * blocks)


def _run(repo, prompt, agent_type="coordinator:git-commit-agent"):
    return workflow.run_agent(prompt, {"agentType": agent_type}, repo_root=repo, artifact_dir=repo / "art",
                              backend="grok", parent_session_id="sid")


def test_hookless_grok_commit_child_finds_the_params_file_written_before_it_runs(tmp_path, stub):
    (tmp_path / ".git").mkdir()
    prompt = _prompt(tmp_path)
    _run(tmp_path, prompt)
    path = chan.params_path(tmp_path.resolve(), _RUN_ID)
    assert stub == [True]
    assert json.loads(path.read_text(encoding="utf-8")) == chan.prompt_params(prompt)
    stdout, stderr, code = _dispatch_argv(["ping", "--params-file", str(path)], str(tmp_path), allow_warm=False)
    assert code == 0, stderr
    assert json.loads(stdout)["result"]["ok"] is True


def test_two_block_prompt_writes_nothing_and_never_blocks_the_spawn(tmp_path, stub):
    (tmp_path / ".git").mkdir()
    _run(tmp_path, _prompt(tmp_path, blocks=2))
    assert stub == [False]
    assert not list(tmp_path.rglob(chan.PARAMS_FILE_NAME))


def test_unminted_token_leaves_the_runner_refusal_note(tmp_path, stub):
    (tmp_path / ".git").mkdir()
    block = chan.render_params_block({"run_id": _RUN_ID, "run_token": "f" * 32, "script_path": _SCRIPT})
    _run(tmp_path, "Commit.\n" + block)
    path = chan.params_path(tmp_path.resolve(), _RUN_ID)
    assert not path.exists()
    assert path.with_name(chan.REFUSAL_FILE_NAME).read_text(encoding="utf-8") == "runner: params-token"
    assert "runner: params-token" in chan.missing_file_diagnosis(str(path))


def test_a_successful_write_clears_an_earlier_refusal_note(tmp_path, stub):
    (tmp_path / ".git").mkdir()
    prompt = _prompt(tmp_path)
    path = chan.params_path(tmp_path.resolve(), _RUN_ID)
    path.parent.mkdir(parents=True)
    chan.note_refusal(str(path), "stale")
    _run(tmp_path, prompt)
    assert path.exists() and not path.with_name(chan.REFUSAL_FILE_NAME).exists()


def test_no_git_entry_skips_the_write_and_the_note(tmp_path, stub):
    (tmp_path / ".git").mkdir()
    prompt = _prompt(tmp_path)
    (tmp_path / ".git").rmdir()
    _run(tmp_path, prompt)
    assert stub == [False]
    assert not (tmp_path / "scratch").exists()


def test_other_agent_types_write_nothing(tmp_path, stub):
    (tmp_path / ".git").mkdir()
    _run(tmp_path, _prompt(tmp_path), agent_type="coordinator:executor")
    assert stub == [False]
    assert not list(tmp_path.rglob(chan.PARAMS_FILE_NAME))


def test_persona_text_is_never_scanned_for_a_params_block(tmp_path, stub, monkeypatch):
    (tmp_path / ".git").mkdir()
    block = chan.render_params_block(
        {"run_id": _RUN_ID, "run_token": run_token.mint(tmp_path, _RUN_ID, _SCRIPT), "script_path": _SCRIPT})
    monkeypatch.setattr(workflow, "_persona", lambda _agent_type: block)
    _run(tmp_path, "Commit with no block.")
    assert stub == [False]
