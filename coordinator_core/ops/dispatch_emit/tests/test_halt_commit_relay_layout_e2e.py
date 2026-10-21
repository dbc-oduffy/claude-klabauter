"""A halt/grind commit agent whose spawn transcript opens with the harness's relayed user request
is admitted by the run-token leg, and the halted run's commit lands with its sizing."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.bash_guards.tests.test_block_subagent_commit_run_token import _COMMIT_AGENT, _SCRIPT
from coordinator_core.ops.dispatch_emit import ask_compose, land_run, run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261015T000000-aaaaaa"
SIZING = "state/sizings/own.yaml"
AGENT = "deadbeef0123"
HALTED_BY = {"halted": "ledger-check", "detail": "row R1 open"}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True)
    (root / SIZING).write_text("a: 1\n", encoding="utf-8")
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(root))
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: AGENT)
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda r, agent_id: "")
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    return root


def _halt_command(root: Path) -> str:
    js = ask_compose._halt_record_commit_fn(
        head="head", anchor="anchor", script_path="run.mjs", repo_root=root, run_id=RUN
    )
    match = re.search(r"coordinator-invoke dispatch\.land_run --params-file [^\s\\\"'`]+", js)
    assert match, js
    return match.group(0)


def _relay_spawn(root: Path, params: dict, *, relay_block: bool = False, command: str = "") -> str:
    """Parent-session transcript path; the spawn's own transcript sits under it with a relay record 0."""
    session = root.parent / "sess.jsonl"
    session.write_text("{}\n", encoding="utf-8")
    d = root.parent / "sess" / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True, exist_ok=True)
    block = chan.render_params_block(params)
    relay = f"{chan.RELAY_HEADER} halt it" + (f"\n{block}" if relay_block else "")
    body = "\n".join("  " + ln for ln in ["phase: commit", command, "", *(block if not relay_block else "x").split("\n")])
    computed = "[Workflow harness — computed task] computed at runtime.\n" + body
    records = [
        {"type": "user", "message": {"role": "user", "content": relay}},
        {"type": "user", "message": {"role": "user", "content": computed}},
    ]
    (d / f"agent-{AGENT}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(session)


def _check(command: str, transcript: str):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "s",
            "cwd": None,
            "agent_id": AGENT,
            "agent_type": _COMMIT_AGENT,
            "transcript_path": transcript,
        }
    )


def _halt_params(token: str) -> dict:
    return {"script_path": "run.mjs", "run_id": RUN, "run_token": token, HALTED_RECORD_PARAM: HALTED_BY}


def test_halt_commit_behind_a_relay_record_is_admitted_and_lands_with_its_sizing(repo):
    token = run_token.mint(repo, RUN, "run.mjs")
    command = _halt_command(repo)
    assert chan.params_path(repo, RUN).as_posix() in command
    transcript = _relay_spawn(repo, _halt_params(token), command=command)

    assert _check(command, transcript) is None
    written = json.loads(chan.params_path(repo, RUN).read_text(encoding="utf-8"))
    assert written == _halt_params(token)

    out = land_run._handler(written, repo_root=repo / ".git")
    assert out["committed"] is True, out
    names = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", out["sha"]], repo).split()
    assert SIZING in names


def test_grind_shaped_block_behind_a_relay_record_is_admitted(repo):
    token = run_token.mint(repo, RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": RUN, "run_token": token, "rows": [{"id": "R1"}]}
    command = f"coordinator-invoke dispatch.land_run --params-file {chan.params_path(repo, RUN).as_posix()}"
    assert _check(command, _relay_spawn(repo, params, command=command)) is None


def test_block_only_inside_the_relay_record_is_denied(repo):
    token = run_token.mint(repo, RUN, "run.mjs")
    command = _halt_command(repo)
    transcript = _relay_spawn(repo, _halt_params(token), relay_block=True, command=command)
    verdict = _check(command, transcript)
    assert verdict is not None
    assert "unreadable or holds no params block" in verdict["hookSpecificOutput"]["permissionDecisionReason"]


def test_forged_token_behind_a_relay_record_is_denied(repo):
    run_token.mint(repo, RUN, "run.mjs")
    command = _halt_command(repo)
    transcript = _relay_spawn(repo, _halt_params("forged-token"), command=command)
    verdict = _check(command, transcript)
    assert "run token is spent" in verdict["hookSpecificOutput"]["permissionDecisionReason"]


def test_another_agents_transcript_behind_a_relay_record_is_denied(repo):
    token = run_token.mint(repo, RUN, "run.mjs")
    command = _halt_command(repo)
    transcript = _relay_spawn(repo, _halt_params(token), command=command)
    spawn = Path(transcript).parent / "sess" / "subagents" / "workflows" / "wf_x" / f"agent-{AGENT}.jsonl"
    spawn.rename(spawn.with_name("agent-cafe00000001.jsonl"))
    assert _check(command, transcript) is not None
