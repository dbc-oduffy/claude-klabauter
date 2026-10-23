"""Audience pins for the single-purpose guards that return an AlternativeRecord.

Each converted guard is driven through its production `check`; the SUBAGENT render
must carry an escalation route and no override text, and the non-matching input
must still allow.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import guard_host_subagent_bash_ban as ban
from coordinator_core.bash_guards import guard_host_subagent_bash_spawn_shapes as spawn_shapes
from coordinator_core.bash_guards import block_illegal_filename as illegal_filename
from coordinator_core.bash_guards import block_noncanonical_branch_creation as noncanonical
from coordinator_core.bash_guards import block_venv_creation as venv
from coordinator_core.bash_guards import guard_no_optional_locks as locks
from coordinator_core.bash_guards import guard_powershell_via_bash as ps_via_bash
from coordinator_core.bash_guards import guard_reap_stale_git_lock as reap
from coordinator_core.bash_guards import guard_repo_setup_claude_home_refusal as claude_home_refusal
from coordinator_core.bash_guards import p4_verb_fence as p4_fence
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.testing import envelope_of


def _reason(envelope) -> str:
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


def _opted_in(tmp_path: Path, key: str) -> Path:
    (tmp_path / "coordinator.local.md").write_text("---\n%s: deny\n---\n" % key, encoding="utf-8")
    return tmp_path


def _payload(cwd: Path, command: str, agent_id="agent-abc123") -> dict:
    body = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    if agent_id:
        body["agent_id"] = agent_id
    return body


@pytest.mark.parametrize(
    "guard, key, command",
    [
        (ban, "subagent_bash_policy", "ls"),
        (spawn_shapes, "subagent_bash_spawn_shapes", "for f in *.py; do echo $f; done"),
    ],
)
def test_denial_is_a_record_with_a_route_and_no_override(tmp_path, guard, key, command):
    result = guard.check(_payload(_opted_in(tmp_path, key), command))
    assert isinstance(result, AlternativeRecord)
    assert result.verdict_kind == "deny"
    assert any(isinstance(m, EscalationRoute) for m in result.members)
    assert not any(isinstance(m, Override) for m in result.members)
    rendered = _reason(envelope_of(result, "subagent"))
    assert "Report " in rendered
    assert "override" not in rendered.lower()


@pytest.mark.parametrize(
    "guard, key, command",
    [
        (ban, "subagent_bash_policy", "ls"),
        (spawn_shapes, "subagent_bash_spawn_shapes", "for f in *.py; do echo $f; done"),
    ],
)
def test_em_and_unopted_hosts_still_allow(tmp_path, tmp_path_factory, guard, key, command):
    assert guard.check(_payload(_opted_in(tmp_path, key), command, agent_id=None)) is None
    assert guard.check(_payload(tmp_path_factory.mktemp("unopted"), command)) is None


_CLAUDE_HOME_SCAFFOLD = "python3 -m coordinator_core.install.scaffold_structure --manifest-root x"


def _fire(name: str, mp, tmp_path: Path):
    """Drive one EM-speaking guard's production `check`; return (result, allowed_result)."""
    if name == "noncanonical":
        mp.setattr(noncanonical, "resolve_git_root", lambda cwd=None: "/repo")
        mp.setattr(noncanonical, "_is_hazard_repo", lambda root: True)
        p = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": "/repo"}
        return noncanonical.check(p("git checkout -b fix/foo")), noncanonical.check(p("git status"))
    if name == "venv":
        p = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}}
        return venv.check(p("python3 -m venv .venv")), venv.check(p("python3 -m pytest"))
    if name == "powershell":
        p = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}}
        return (
            ps_via_bash.check(p('powershell -Command "Write-Host $HOME"')),
            ps_via_bash.check(p("powershell -Command 'Write-Host $HOME'")),
        )
    if name == "p4":
        mp.setattr(p4_fence, "_is_p4_gated", lambda cwd: True)
        p = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": "/repo"}
        return p4_fence.check(p("p4 submit -c 1")), p4_fence.check(p("echo hi"))
    if name == "claude_home":
        home = tmp_path / "home"
        claude_home = home / ".claude"
        claude_home.mkdir(parents=True)
        env = {"HOME": str(home), "USERPROFILE": str(home)}
        p = lambda c, cwd: {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(cwd), "env": env}
        return (
            claude_home_refusal.check(p(_CLAUDE_HOME_SCAFFOLD, claude_home)),
            claude_home_refusal.check(p(_CLAUDE_HOME_SCAFFOLD, tmp_path)),
        )
    if name == "illegal_filename":
        p = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}}
        return illegal_filename.check(p("echo x > bad:name.txt")), illegal_filename.check(p("echo x > good.txt"))
    p = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}}
    return locks.check_git_no_optional_locks("git status", "s", payload=p("git status")), locks.check_git_no_optional_locks(
        "git log", "s", payload=p("git log")
    )


_EM_SPEAKING = [
    ("noncanonical", "deny"),
    ("venv", "deny"),
    ("powershell", "advisory"),
    ("p4", "deny"),
    ("claude_home", "deny"),
    ("illegal_filename", "rewrite"),
    ("locks", "rewrite"),
]


@pytest.mark.parametrize("name, kind", _EM_SPEAKING)
def test_em_speaking_guard_returns_a_typed_record(name, kind, tmp_path, monkeypatch):
    fired, _ = _fire(name, monkeypatch, tmp_path)
    assert isinstance(fired, AlternativeRecord)
    assert fired.verdict_kind == kind
    assert fired.em_text


@pytest.mark.parametrize("name, kind", _EM_SPEAKING)
def test_em_speaking_guard_subagent_render_has_route_and_no_override(name, kind, tmp_path, monkeypatch):
    fired, _ = _fire(name, monkeypatch, tmp_path)
    hso = envelope_of(fired, "subagent")["hookSpecificOutput"]
    text = hso.get("permissionDecisionReason") or hso.get("additionalContext")
    assert "Report " in text
    assert "COORDINATOR_" not in text
    assert "override" not in text.lower()


@pytest.mark.parametrize("name, kind", _EM_SPEAKING)
def test_em_render_is_the_legacy_text_verbatim(name, kind, tmp_path, monkeypatch):
    fired, _ = _fire(name, monkeypatch, tmp_path)
    hso = envelope_of(fired, "em")["hookSpecificOutput"]
    assert (hso.get("permissionDecisionReason") or hso.get("additionalContext")) == fired.em_text


@pytest.mark.parametrize("name, kind", _EM_SPEAKING)
def test_em_speaking_guard_still_allows_the_non_matching_input(name, kind, tmp_path, monkeypatch):
    _, allowed = _fire(name, monkeypatch, tmp_path)
    assert allowed is None


def test_rewrite_allow_matches_head(tmp_path, monkeypatch):
    illegal, _ = _fire("illegal_filename", monkeypatch, tmp_path)
    lock, _ = _fire("locks", monkeypatch, tmp_path)
    assert illegal.rewrite_allow is False
    assert lock.rewrite_allow is True
    assert "permissionDecision" not in envelope_of(illegal, "em")["hookSpecificOutput"]
    assert envelope_of(lock, "em")["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_reap_stale_git_lock_has_no_message_site(tmp_path):
    assert reap.check_reap_stale_git_lock("git status", str(tmp_path), "s") is None
