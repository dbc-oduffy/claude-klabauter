"""Admissibility of `_sanctioned_remedy.probe_remedy` against the refusing guards.

Every exemplar must be admitted (`check()` returns None) by each in-process
refusing guard that applies to its caller class and dialect; the reviewer class
has no command exemplar because its shipped policy admits no interpreter script.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as reviewer_guard,
    block_subagent_destructive_action as destructive_guard,
    guard_host_subagent_bash_ban as ban_guard,
    guard_host_subagent_bash_spawn_shapes as spawn_guard,
)
from coordinator_core.bash_guards._dialect import Dialect
from coordinator_core.bash_guards._sanctioned_remedy import (
    CALLER_CLASSES,
    CALLER_EM,
    CALLER_REVIEWER,
    CALLER_SUBAGENT,
    probe_remedy,
)

_TOOL = {Dialect.BASH: "Bash", Dialect.POWERSHELL: "PowerShell"}
_REVIEWER_TYPE = "coordinator:code-reviewer"
_BANNER = 'echo "=== facts ==="; pwd; whoami; git status --short; date'


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A cwd whose coordinator.local.md opts into both subagent bash policies,
    and a reviewer guard whose identity lookup resolves to the type in `kind`."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_policy: deny\nsubagent_bash_spawn_shapes: deny\n---\n",
        encoding="utf-8",
    )
    kind = {"type": "general-purpose"}
    monkeypatch.setattr(reviewer_guard, "is_confined_by_roster_absence", lambda t: False)
    monkeypatch.setattr(reviewer_guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        reviewer_guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        reviewer_guard, "_read_backpointer_subagent_type", lambda root, aid, **kw: kind["type"]
    )

    def payload(caller_class, dialect, command):
        p = {
            "tool_name": _TOOL[dialect],
            "tool_input": {"command": command},
            "session_id": "sess1",
            "cwd": str(tmp_path),
        }
        if caller_class != CALLER_EM:
            p["agent_id"] = "deadbeef0123"
            p["agent_type"] = _REVIEWER_TYPE if caller_class == CALLER_REVIEWER else "general-purpose"
            kind["type"] = p["agent_type"]
        return p

    return payload


def _denied(result):
    return result is not None and result["hookSpecificOutput"]["permissionDecision"] == "deny"


_CHECKS = (
    ("spawn-shapes", spawn_guard.check),
    ("destructive-action", destructive_guard.check),
    ("reviewer-allowlist", reviewer_guard.check),
)


@pytest.mark.parametrize("dialect", list(Dialect))
@pytest.mark.parametrize("caller_class", [CALLER_EM, CALLER_SUBAGENT])
def test_command_exemplar_is_admitted_by_every_refusing_guard(host, caller_class, dialect, tmp_path):
    _, exemplar = probe_remedy(caller_class, dialect, str(tmp_path / "multiprobe.py"))
    payload = host(caller_class, dialect, exemplar)
    for name, check in _CHECKS:
        assert check(dict(payload)) is None, name
    if dialect is Dialect.POWERSHELL:
        assert ban_guard.check(dict(payload)) is None


def test_subagent_bash_tool_is_refused_wholesale_under_the_ban_policy(host, tmp_path):
    """The ban refuses the tool, not the shape: no Bash exemplar passes it, and
    the PowerShell exemplar is the outlet."""
    _, bash_exemplar = probe_remedy(CALLER_SUBAGENT, Dialect.BASH, str(tmp_path / "multiprobe.py"))
    assert _denied(ban_guard.check(host(CALLER_SUBAGENT, Dialect.BASH, bash_exemplar)))
    _, ps_exemplar = probe_remedy(CALLER_SUBAGENT, Dialect.POWERSHELL, str(tmp_path / "multiprobe.py"))
    assert ban_guard.check(host(CALLER_SUBAGENT, Dialect.POWERSHELL, ps_exemplar)) is None


@pytest.mark.parametrize("dialect", list(Dialect))
def test_the_refused_banner_shape_is_the_control_the_exemplar_escapes(host, dialect):
    assert _denied(spawn_guard.check(host(CALLER_SUBAGENT, dialect, _BANNER)))


@pytest.mark.parametrize("dialect", list(Dialect))
def test_reviewer_has_no_command_exemplar_and_names_the_harness_tools(dialect):
    text, exemplar = probe_remedy(CALLER_REVIEWER, dialect)
    assert exemplar == ""
    assert all(tool in text for tool in ("Read", "Grep", "Glob"))


@pytest.mark.parametrize("dialect", list(Dialect))
def test_multiprobe_banner_script_remedy_is_refused_for_reviewer(host, dialect, tmp_path):
    """The subagent-class banner remedy (`python3 <script>`) is not performable by
    a reviewer: the shipped policy leaves `interpreter_allow_scripts` false, and
    inline `python3 -c` is refused unconditionally."""
    _, script_remedy = probe_remedy(CALLER_SUBAGENT, dialect, str(tmp_path / "multiprobe.py"))
    assert _denied(reviewer_guard.check(host(CALLER_REVIEWER, dialect, script_remedy)))
    _, inline_remedy = probe_remedy(CALLER_EM, dialect)
    assert _denied(reviewer_guard.check(host(CALLER_REVIEWER, dialect, inline_remedy)))


def test_script_hint_with_whitespace_is_quoted():
    _, exemplar = probe_remedy(CALLER_SUBAGENT, Dialect.POWERSHELL, "C:/Users/a b/multiprobe.py")
    assert exemplar == 'python3 "C:/Users/a b/multiprobe.py"'


def test_blank_hint_renders_a_placeholder_path():
    text, exemplar = probe_remedy(CALLER_SUBAGENT, Dialect.BASH)
    assert "multiprobe.py" in text and exemplar.startswith("python3 ")


@pytest.mark.parametrize("caller_class", CALLER_CLASSES)
def test_remedy_text_carries_no_override_key(caller_class):
    text, _ = probe_remedy(caller_class, Dialect.BASH)
    assert "COORDINATOR_" not in text and "override" not in text.lower()


def test_unknown_inputs_raise():
    with pytest.raises(ValueError):
        probe_remedy("operator", Dialect.BASH)
    with pytest.raises(ValueError):
        probe_remedy(CALLER_EM, "bash")  # type: ignore[arg-type]
