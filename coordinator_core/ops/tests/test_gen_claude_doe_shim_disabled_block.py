"""
coordinator_core/ops/tests/test_gen_claude_doe_shim_disabled_block.py

Regression guard: a DISABLED coordinator shim block must be distinguishable
from a never-installed one.

Why (2026-08-14 launch-chain incident): the workaround for a console-corruption
bug was to comment the generated block out of the operator's PowerShell
`$PROFILE`. Bare `claude` then works — without the coordinator plugin. Every
session on the box silently became vanilla, and nothing detected it for days.

Coordinator's own SessionStart hooks cannot catch this: they do not run when
coordinator fails to load. The detector has to live in the installer, which is
what this guards.

Before the fix, both shapes reported `sentinel absent — would add source block`
and exited 0, because neither carries an exact `SENTINEL_BEGIN` line.

Spec backlink: docs/reference/interactive-launch-chain.md § 4.
"""
from __future__ import annotations

import pytest

from coordinator_core.ops.gen_claude_doe_shim import (
    EXPECTED_SOURCE_LINE,
    EXPECTED_SOURCE_LINE_POWERSHELL,
    _commented_out_source_lines,
    _disabled_block_report,
)

pytestmark = [pytest.mark.cadence]

_SOURCE_LINES = {
    "powershell": EXPECTED_SOURCE_LINE_POWERSHELL,
    "bash": EXPECTED_SOURCE_LINE,
}


@pytest.mark.parametrize("family", sorted(_SOURCE_LINES))
def test_commented_out_block_is_detected(family):
    """The exact shape the operator produced: generated lines, each commented."""
    expected = _SOURCE_LINES[family]
    rc = "\n".join(f"# {line}" for line in expected.split("\n") if line.strip())

    found = _commented_out_source_lines(rc, expected)

    assert len(found) == len([l for l in expected.split("\n") if l.strip()])


@pytest.mark.parametrize("family", sorted(_SOURCE_LINES))
def test_live_block_is_not_reported_as_disabled(family):
    """An enabled block must never be flagged — it is uncommented."""
    expected = _SOURCE_LINES[family]
    rc = f"# --- coordinator claude-doe shim [generated] ---\n{expected}\n"

    assert _commented_out_source_lines(rc, expected) == []


@pytest.mark.parametrize("family", sorted(_SOURCE_LINES))
def test_clean_rc_is_not_reported_as_disabled(family):
    """A never-installed rc has nothing to report — it is not a misconfiguration."""
    rc = "# my profile\nSet-Alias ll Get-ChildItem\n"

    assert _commented_out_source_lines(rc, _SOURCE_LINES[family]) == []


def test_prose_mentioning_the_shim_is_not_a_false_positive():
    """Detection is exact-match on our own generated lines, never keyword prose.

    The operator's profile legitimately carries hand-written notes naming the
    shim — including remediation notes this generator's own diagnostics
    suggest. Flagging those would train the operator to ignore the warning.
    """
    rc = (
        "# The shim invokes claude-doe.ps1 in-process so claude.exe is a direct child.\n"
        "# If corruption returns, run claude.exe --plugin-dir X:\\DoE-claude\\coordinator\n"
        "# See .claude\\shell\\claude-doe-shim.ps1 for the generated function.\n"
    )

    assert _commented_out_source_lines(rc, EXPECTED_SOURCE_LINE_POWERSHELL) == []


def test_extra_comment_markers_and_indentation_still_detected():
    """`# #` and leading whitespace are both real shapes an operator produces."""
    expected = EXPECTED_SOURCE_LINE_POWERSHELL
    first = [l for l in expected.split("\n") if l.strip()][0]
    rc = f"   ## {first}\n"

    assert _commented_out_source_lines(rc, expected) == [f"   ## {first}"]


def test_report_names_the_blast_radius_and_a_manual_action():
    """Message register: state the consequence and give the operator the edit.

    No slash command — this fires with no coordinator session available, so an
    agentic remedy would name one that cannot run.
    """
    report = _disabled_block_report("C:\\rc.ps1", ["# $__claude_doe_shim_base = ..."])
    text = "\n".join(report)

    assert "without the coordinator plugin" in text
    assert "C:\\rc.ps1" in text
    assert "coordinator:" not in text
