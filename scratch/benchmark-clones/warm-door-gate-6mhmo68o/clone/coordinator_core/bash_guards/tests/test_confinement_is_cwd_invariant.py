"""
The confinement verdict must not depend on where the agent happens to be working.

Subject: the P1 filed as
``state/bug-backlog/2026-08-21-bash-guard-applies-code-reviewer-allowlist-to-other-agents-intermittently.yaml``,
reported by three sessions as intermittent and unreproducible. It was neither. The
verdict was a deterministic function of ``payload["cwd"]``: ``check()`` derived
``git_root`` solely from it, so an agent running a command from its own scratchpad
(outside the repo) failed back-pointer resolution, fell back to the caller-chosen
teammate NAME as its effective type, and was confined by roster-absence -- handed
``coordinator:code-reviewer``'s allowlist. The same agent running the same command
from the repo root was allowed. That is the whole "denies then permits the
byte-identical command" behaviour, and why it looked like a flap.

These tests pin BOTH halves. Widening where the back-pointer store is looked for
would be a bad trade if it also relaxed confinement, so the posture cases below are
not decoration -- they are the reason the fix is safe. ``_read_backpointer_subagent_type``
cross-checks the resolved em-session against this payload's own ``session_id``, so a
recovered root can only ever find a TRUE identity or no row at all.
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as guard
from coordinator_core.subagent_sandbox import engine as _sandbox_engine
from coordinator_core.bash_guards import _helpers

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
    """A NAMED dispatch of a roster-enumerated, non-confined type, with its
    back-pointer and dispatch row on disk exactly as a real dispatch leaves them."""
    session_id = "1617ff7f-e12a-40db-a9d8-0f63a351914d"
    name = "parity-plans"
    raw_agent_id = f"a{name}-0123456789abcdef"
    canonical = guard._resolve_subagent_identity(raw_agent_id, session_id)
    assert canonical, "fixture premise: the named-teammate id must resolve"

    root = tmp_path / "repo"
    root.mkdir()
    # A REAL repo: `resolve_git_root` shells out to `git rev-parse --show-toplevel`,
    # so a bare directory resolves for NO cwd and the test would pass/fail for the
    # wrong reason -- every verdict DENY, including the control.
    subprocess.run(
        ["git", "init", "-q", str(root)],
        check=True, capture_output=True,
    )
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
    # Pin the roster. `resolve_roster()` discovers agent definitions by walking the
    # tree it is invoked from, so inside a tmp_path repo it finds none and EVERY type
    # -- including the back-pointer-resolved `general-purpose` -- reads as unknown,
    # confining all four cases and passing this test for the wrong reason. The subject
    # here is cwd-invariance, not roster discovery, so the roster is held fixed and
    # realistic: `general-purpose` enumerated, the teammate NAME absent, which is
    # exactly the production shape.
    monkeypatch.setattr(
        _helpers,
        "_resolve_roster_accessor",
        lambda: (lambda: (frozenset({"general-purpose", "coordinator:code-reviewer"}), None)),
    )
    monkeypatch.chdir(root)
    return raw_agent_id, name, session_id, root


def test_named_dispatch_is_allowed_from_a_cwd_outside_the_repo(named_dispatch, tmp_path):
    """The regression itself: a scratchpad cwd must not confine a non-confined type."""
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
    """Posture: the fix must not hand an unrestricted surface to a confined type."""
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
    """Posture: Divergence 18's deliberate fail-closed-on-unresolved is untouched."""
    _, _, session_id, root = named_dispatch
    verdict = guard.check(
        _payload("afabricated-0123456789abcdef", "totally-made-up", str(root), session_id)
    )
    assert verdict is not None, (
        "a dispatch whose type is unknown on BOTH legs must remain confined -- "
        "recovering git_root must not become a confinement bypass."
    )
