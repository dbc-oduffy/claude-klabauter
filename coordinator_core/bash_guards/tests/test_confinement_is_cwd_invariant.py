from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as guard
from coordinator_core.subagent_sandbox import engine as _sandbox_engine
from coordinator_core.bash_guards import _helpers
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CMD = "sed -n 1,10p README.md"


def _payload(agent_id, agent_type, cwd, session_id):
    p = {
        "tool_name": "Bash",
        "agent_id": agent_id,
        "session_id": session_id,
        "agent_type": agent_type,
        "tool_input": {"command": _CMD},
    }
    if cwd is not None:
        p["cwd"] = cwd
    return p


@pytest.fixture
def named_dispatch(tmp_path, monkeypatch):
    session_id = "1617ff7f-e12a-40db-a9d8-0f63a351914d"
    name = "parity-plans"
    raw_agent_id = f"a{name}-0123456789abcdef"
    canonical = guard._resolve_subagent_identity(raw_agent_id, session_id)
    assert canonical, "fixture premise: the named-teammate id must resolve"

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(root)],
        check=True, capture_output=True,
    **no_console_creationflags())
    _sandbox_engine.reset_resolve_git_root_cache()

    sessions = root / ".git" / "coordinator-sessions"
    (sessions / ".agents" / canonical).mkdir(parents=True, exist_ok=True)
    (sessions / ".agents" / canonical / "em-session-id.txt").write_text(
        session_id + "\n", encoding="utf-8"
    )
    (sessions / session_id).mkdir(parents=True, exist_ok=True)
    (sessions / session_id / "dispatched-agents.txt").write_text(
        f"{canonical}\topus\tgeneral-purpose\t1787487417\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        _helpers,
        "_resolve_roster_accessor",
        lambda: (lambda: (frozenset({"general-purpose", "coordinator:code-reviewer"}), None)),
    )
    monkeypatch.chdir(root)
    return raw_agent_id, name, session_id, root


def test_named_dispatch_is_allowed_from_a_cwd_outside_the_repo(named_dispatch, tmp_path):
    raw_agent_id, name, session_id, root = named_dispatch
    scratch = tmp_path / "scratchpad"
    scratch.mkdir()

    verdicts = {
        label: guard.check(_payload(raw_agent_id, name, cwd, session_id))
        for label, cwd in [
            ("repo", str(root)),
            ("scratchpad", str(scratch)),
            ("empty", ""),
            ("absent", None),
        ]
    }
    denied = sorted(k for k, v in verdicts.items() if v is not None)
    assert denied == [], (
        f"confinement verdict varies with cwd -- denied from {denied}. The agent's "
        "working directory must not decide which ruleset governs it."
    )


def test_a_real_confined_agent_stays_confined_from_every_cwd(named_dispatch, tmp_path):
    _, _, session_id, root = named_dispatch
    scratch = tmp_path / "scratchpad"
    scratch.mkdir()

    for label, cwd in [("repo", str(root)), ("scratchpad", str(scratch))]:
        verdict = guard.check(
            _payload("abcdef0123456789", "coordinator:code-reviewer", cwd, session_id)
        )
        assert verdict is not None, (
            f"coordinator:code-reviewer escaped confinement from cwd={label} -- "
            "the cwd fallback must not relax the confined set."
        )


def test_a_type_unknown_on_both_legs_stays_fail_closed(named_dispatch, tmp_path):
    _, _, session_id, root = named_dispatch
    verdict = guard.check(
        _payload("afabricated-0123456789abcdef", "totally-made-up", str(root), session_id)
    )
    assert verdict is not None, (
        "a dispatch whose type is unknown on BOTH legs must remain confined -- "
        "recovering git_root must not become a confinement bypass."
    )
