"""Tests for the TIER-T CONCURRENCY leg (leg 0.5) of check_test_suite_invocation.

What this leg must do: route a DISPATCHED caller's scoped test run through
``with-tier-t-slot``, so the box can bound how many execute at once.

What it must NOT do, and what half these tests are here to pin: narrow the
Tier-T carve-out. Scoped runs stay permitted for everyone -- the leg is a
resource control, not an authority one, and the EM is untouched. A future
reader who "simplifies" this into an authority check breaks DoE's R9 ruling and
the pre-existing-failure verification workflow it protects; the tests below are
what should stop them. ``test_this_leg_never_denies`` is the load-bearing one:
the first version of this leg DID deny, and turned 29 existing assertions red
at once. Those assertions were right and the leg was wrong.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard


def _payload(command, *, agent=True, cwd=None, tool="Bash"):
    p = {"tool_name": tool, "tool_input": {"command": command}}
    if cwd is not None:
        p["cwd"] = cwd
    if agent:
        p["agent_id"] = "a-dispatched-agent-1"
    return p


def _verdict(command, *, agent=True, cwd=None):
    return guard.check(_payload(command, agent=agent, cwd=cwd))


def _decision(out):
    if out is None:
        return "none"
    return out["hookSpecificOutput"]["permissionDecision"]


def _rewrite(out):
    """The command the guard handed back, or None if it rewrote nothing."""
    if out is None:
        return None
    return out["hookSpecificOutput"].get("updatedInput", {}).get("command")


def _slotted(command, *, cwd=None, agent=True):
    """Did the guard route this command through the slot wrapper?"""
    out = _verdict(command, agent=agent, cwd=cwd)
    assert _decision(out) != "deny", "this leg must never deny"
    return (_rewrite(out) or "").startswith("with-tier-t-slot -- ")


@pytest.fixture
def repo(tmp_path):
    """A repo root with a pytest testpaths config, so scoping classifies."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / "tests").mkdir()
    return str(tmp_path)


# --- the leg fires on the incident's shape -------------------------------

@pytest.mark.parametrize("command", [
    "python3 -m pytest tests/test_a.py",
    "python3 -m pytest tests/test_a.py::test_case",
    "pytest tests/test_a.py",
    "pytest -k some_expression",
])
def test_subagent_scoped_run_is_routed_through_the_slot_wrapper(command, repo):
    assert _slotted(command, cwd=repo)


def test_this_leg_never_denies(repo):
    """The property that keeps it a RESOURCE control rather than an authority one.

    If this ever fails, the leg has become a refusal and DR-088's Tier-T
    carve-out has moved -- which is not this repo's call to make.
    """
    for command in ("pytest tests/test_a.py", "pytest -k expr",
                    "cd sub && pytest tests/test_a.py"):
        assert _decision(_verdict(command, cwd=repo)) != "deny"


def test_the_rewrite_preserves_the_callers_command_exactly(repo):
    """A rewrite that alters the command would be a silent corruption hazard."""
    out = _verdict("pytest tests/test_a.py::test_case -q", cwd=repo)
    assert _rewrite(out) == "with-tier-t-slot -- pytest tests/test_a.py::test_case -q"


def test_the_note_says_nothing_is_refused(repo):
    """An agent that reads this as a refusal goes looking for a way around it."""
    out = _verdict("pytest tests/test_a.py", cwd=repo)
    note = out["hookSpecificOutput"]["additionalContext"].lower()
    assert "nothing is being refused" in note
    assert "nothing to ask for" in note


def test_a_chained_command_is_advised_not_rewritten(repo):
    """BX-12's single-segment rule: never substitute a chain we did not parse."""
    out = _verdict("cd sub && pytest tests/test_a.py", cwd=repo)
    assert _decision(out) == "allow"
    assert _rewrite(out) is None
    assert "with-tier-t-slot" in out["hookSpecificOutput"]["additionalContext"]


# --- the leg allows what it should ---------------------------------------

@pytest.mark.parametrize("command", [
    "with-tier-t-slot -- pytest tests/test_a.py",
    "with-tier-t-slot -- python3 -m pytest tests/test_a.py::test_case",
    "cd sub && with-tier-t-slot -- pytest tests/test_a.py",
    "with-tier-t-slot --wait 60 -- pytest tests/test_a.py",
])
def test_already_wrapped_runs_are_left_completely_alone(command, repo):
    assert _verdict(command, cwd=repo) is None


def test_a_decoy_wrapped_segment_does_not_license_a_bare_runner(repo):
    """`with-tier-t-slot -- true && pytest ...` wraps a no-op; the real run is bare.

    The sibling suite-mutex wrapper leg shipped with exactly this hole and had
    to be fixed after review. Pinned here so this one never grows it: the
    unwrapped `pytest` segment must still draw the leg's attention.
    """
    out = _verdict("with-tier-t-slot -- true && pytest tests/test_a.py", cwd=repo)
    assert out is not None
    assert "with-tier-t-slot" in out["hookSpecificOutput"].get("additionalContext", "")


@pytest.mark.parametrize("command", [
    "git status",
    "echo pytest",
    "ls tests/",
    "cat tests/test_a.py",
])
def test_non_runner_commands_are_untouched(command, repo):
    assert _verdict(command, cwd=repo) is None


# --- the carve-out is NOT narrowed ---------------------------------------

@pytest.mark.parametrize("command", [
    "pytest tests/test_a.py",
    "python3 -m pytest tests/test_a.py::test_case",
    "pytest -k some_expression",
])
def test_the_em_is_unaffected_by_this_leg(command, repo):
    """One session cannot fan out; its scoped runs are serial by construction.

    Matches leg 0's subagent-only scope. The EM's command is not even
    rewritten -- nothing mediates the top-level session's scoped runs.
    """
    assert _verdict(command, agent=False, cwd=repo) is None


def test_scoped_runs_remain_permitted_for_subagents(repo):
    """R9: a node id stays legal for a subagent regardless of its touched set.

    The whole point of the leg being a rewrite rather than a refusal: the
    command a dispatched caller asked for still runs, unchanged, on the far
    side of a slot.
    """
    bare = "pytest tests/test_a.py::test_the_agent_did_not_author"
    out = _verdict(bare, cwd=repo)
    assert _decision(out) == "allow"
    assert _rewrite(out) == f"with-tier-t-slot -- {bare}"


def test_leg_does_not_restate_a_deny_the_identity_leg_owns(repo):
    """A suite-shaped subagent command is an identity problem and must say so."""
    out = guard.check(_payload("pytest", cwd=repo))
    assert out is not None
    text = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "with-tier-t-slot" not in text


def test_override_env_var_still_disarms_the_whole_guard(repo):
    """Documented behaviour of the existing override; the new leg is not exempt."""
    p = _payload("pytest tests/test_a.py", cwd=repo)
    p["env"] = {"COORDINATOR_OVERRIDE_TEST_SUITE_INVOCATION": "1"}
    assert guard.check(p) is None


def test_powershell_dialect_is_classified_too(repo):
    assert _slotted("pytest tests/test_a.py", cwd=repo)
    p = _payload("pytest tests/test_a.py", cwd=repo, tool="PowerShell")
    assert guard.check(p) is not None
