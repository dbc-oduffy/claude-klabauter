"""Every command ``check_test_suite_invocation`` PRINTS as an alternative
must be one the same guard ALLOWS, spelled with an entrypoint the install
surface actually provides.

Why this is its own gate rather than a case in
``test_check_test_suite_invocation.py``: that file is ``cadence``-marked and
``spawns_process``-marked, so it is out of the fast tier -- and the failure
this pins is a message defect, cheap to evaluate in-process, that an
operator hits on the first refusal of a session.

The defect it locks out (observed live, 2026-08-22, machine-b): the deny
text offered ``pytest -k the_behaviour_you_changed`` as the narrowest thing
to run, and the guard refused that command when it carried a redirection --
an operator following the message verbatim was denied twice, by the message
that told them what to run. Second leg of the same defect: three of the four
bullets named a bare ``pytest``, which ``scripts/setup.py`` deliberately does
not provision (§ ``--with-test-deps``: the installer provisions the engine,
not the dev loop), so on a correctly-installed box the offered command has no
resolvable entrypoint.

Distinct from ``_alternative_liveness.py``, which measures whether an
alternative EXECUTES: this asks whether the guard that printed it would let
it run at all, which no execution probe can answer. That module also records
this guard under ``UNTRIGGERED``, so its alternatives reach no gate there.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py
"""

from __future__ import annotations

import re
from typing import List

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard

_AGENT_ID = "a0123456789abcdef"

#: An offered alternative line: two-space indented, and a test-runner
#: invocation rather than a grant/override/diagnostic line.
_RUNNER_ALTERNATIVE_RE = re.compile(
    r"^ {2}((?:\S*python\S*\s+-m\s+)?(?:py\.test|pytest)\b.*)$", re.MULTILINE)

#: The interpreter spellings whose ``-m`` form the engine's own install
#: surface guarantees. A console script (bare ``pytest``) is NOT on this
#: list: the installer leaves the test extra unprovisioned by default, and
#: where a console script does exist it is pinned to whichever interpreter
#: created it and breaks when that interpreter moves.
_GUARANTEED_INTERPRETERS = ("python3", "python", "py")


def _payload(command: str, cwd, agent_id: str = None) -> dict:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess-alt-contract",
        "cwd": str(cwd),
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _alternatives(message: str) -> List[str]:
    found = [m.group(1).strip() for m in _RUNNER_ALTERNATIVE_RE.finditer(message)]
    assert found, "deny text offered no runner alternative at all:\n%s" % message
    return found


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo root shaped like this one -- the placeholder paths in the
    offered alternatives are classified against these ``testpaths``."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["coordinator_core", "coordinator/tests"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, None))
    return tmp_path


def _deny_messages() -> List[tuple]:
    """(label, message, agent_id) for every deny renderer that offers a
    runner alternative. ``agent_id`` is the identity that RECEIVES that
    message -- an alternative is only honest if it passes for the caller it
    was printed to."""
    return [
        ("subagent", guard._deny_reason_subagent("pytest", "pytest -q"), _AGENT_ID),
        ("grant", guard._deny_reason_grant(
            "python -m pytest", "python -m pytest -q", is_tie=False), None),
        ("grant-tie", guard._deny_reason_grant(
            "python -m pytest", "python -m pytest -q", is_tie=True), None),
        ("public-api", guard._remediation_text("U", "pytest"), _AGENT_ID),
    ]


@pytest.mark.parametrize("label,message,agent_id", _deny_messages(),
                         ids=lambda v: v if isinstance(v, str) and "\n" not in v else "")
def test_offered_alternative_is_allowed_by_this_guard(label, message, agent_id, repo):
    for alternative in _alternatives(message):
        assert guard.check(_payload(alternative, repo, agent_id)) is None, (
            "%s deny text offers a command this guard refuses: %r" % (label, alternative)
        )


@pytest.mark.parametrize("label,message,agent_id", _deny_messages(),
                         ids=lambda v: v if isinstance(v, str) and "\n" not in v else "")
def test_offered_alternative_captures_output_without_flipping_the_verdict(
        label, message, agent_id, repo):
    """The same alternative with output captured -- the shape that produced
    the live defect. A redirection is shell plumbing and must not change
    which tier the runner's argv classifies as."""
    for alternative in _alternatives(message):
        piped = "%s 2>&1 | tail -20" % alternative
        assert guard.check(_payload(piped, repo, agent_id)) is None, (
            "%s alternative flips to a deny once output is captured: %r"
            % (label, piped)
        )


@pytest.mark.parametrize("label,message,agent_id", _deny_messages(),
                         ids=lambda v: v if isinstance(v, str) and "\n" not in v else "")
def test_offered_alternative_names_a_provisioned_entrypoint(label, message, agent_id):
    for alternative in _alternatives(message):
        head, _, rest = alternative.partition(" ")
        assert head in _GUARANTEED_INTERPRETERS and rest.startswith("-m "), (
            "%s deny text offers %r, whose entrypoint the install surface does "
            "not provision -- spell it as `python3 -m pytest ...`"
            % (label, alternative)
        )
