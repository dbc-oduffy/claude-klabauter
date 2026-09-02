"""The exit-code contract for the agent-helper forwarder write loop.

Pins the closing action of `state/bug-backlog/2026-08-30-install-substrate-
exits-0-after-failing-45f4d5390b68.yaml`: an install run in which one name's
forwarder cannot be written must NOT exit 0 while reporting names it did not
write. The measured incident was a run whose door-image install died with an
uncaught PermissionError, printed the traceback, replaced no image, and still
reported success -- which is why the underlying defect went unnoticed for as
long as it did. Every install anyone ran said it worked.

NEGATIVE SPEC -- what these tests deliberately do NOT assert:

  - They do not assert that a per-name failure ABORTS the run. Per-name
    tolerance is the point of C2's degrade (a failed door build leaves ONE
    name on its Python forwarder rather than killing the install for every
    later name). Tolerance and honesty are separate properties; only the
    second is under test here.
  - They do not assert exact message wording. The register may be reworded;
    what must not change is that the failed count reaches the operator and
    that the run's exit is non-zero.

The two halves are tested separately because they fail independently: a run
can raise without ever printing what was lost, and can print a summary that
nothing acts on.
"""

import pytest

from coordinator_core.install import substrate


def _failed(*names):
    return [(name, OSError(f"synthetic failure for {name}")) for name in names]


def test_a_failed_name_makes_the_run_raise_rather_than_return():
    """Non-zero exit, via the fatal this module's `main` turns into one."""
    with pytest.raises(substrate.SubstrateFatalError) as excinfo:
        substrate._raise_if_agent_helper_forwarders_failed(
            _failed("coordinator-doc-new"),
            {"coordinator-doc-new": "x", "coordinator-queue-append": "y"},
            check_only=False,
            agent_helper_resolved=[],
        )
    message = str(excinfo.value)
    assert "coordinator-doc-new" in message, (
        "the failing name must reach the operator -- a count alone does not say "
        "which forwarder is missing"
    )


def test_check_only_failure_also_raises():
    """The check branch carries the same contract as the write branch; the
    incident's own second run was a repeat, so a branch that reports success
    is as harmful here as in the write path."""
    with pytest.raises(substrate.SubstrateFatalError):
        substrate._raise_if_agent_helper_forwarders_failed(
            _failed("coordinator-doc-new"),
            {"coordinator-doc-new": "x"},
            check_only=True,
            agent_helper_resolved=[],
        )


def test_a_clean_run_does_not_raise():
    """Discriminates the assertion above: it must fail on failure, not always."""
    substrate._raise_if_agent_helper_forwarders_failed(
        [], {"coordinator-doc-new": "x"}, check_only=False, agent_helper_resolved=[],
    )


def test_summary_names_the_failed_count_on_stderr(capsys):
    substrate._report_agent_helper_forwarder_summary(
        {"a": "x", "b": "y", "c": "z"}, _failed("b"),
    )
    err = capsys.readouterr().err
    assert "2 written" in err and "1 FAILED of 3" in err
    assert "FAILED b" in err, "the per-name line must survive, not just the tally"


def test_summary_prints_on_a_clean_run_too(capsys):
    """The all-clear case prints, so a MISSING summary line is itself evidence
    the run died early -- never silently indistinguishable from a clean one.
    Without this, absence of bad news reads as good news, which is the exact
    shape of the original incident."""
    substrate._report_agent_helper_forwarder_summary({"a": "x", "b": "y"}, [])
    captured = capsys.readouterr()
    assert "2 written, 0 failed of 2" in (captured.out + captured.err)
