"""Reachability tests for Bucket D (C9, state/dispatch-briefs/2026-08-26-the-
destructive-core-learns-the-shell-it-guards/C9.md): the two `guard_chain`
entries that were already dialect-capable in code but never declared it --
`head-tail-plumbing-rewrite` and `block-dev-repo-sentinel-removal-advisory`
(state/audits/2026-08-26-guard-detection-language-dependence-recensus.md
Finding 4).

negative_spec: this module does not test detection CORRECTNESS of either
PowerShell leg beyond what its own unit-test module already covers
(`test_guard_head_tail_rewrite.py` for the `Select-Object` leg,
`test_block_dev_repo_sentinel_removal.py`-adjacent coverage for the sentinel
detector). It tests only that each entry's real dispatch registration makes
its PowerShell branch reachable at all -- the defect this chunk closes was
unreachability via the specific `guard_chain` registration, not a missing
detector.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import guard_head_tail_rewrite as _head_tail_guard


pytestmark = [pytest.mark.cadence]


def test_head_tail_plumbing_rewrite_powershell_leg_is_reached_via_dispatch():
    """`head-tail-plumbing-rewrite`'s own registration in `dispatch.py` used
    to pin `matchers=("Bash",)` while its own docstring (AC16 CALLEE-GRAPH
    AUDIT, C6) named this exact call site as unable to reach the
    `_check_head_tail_plumbing_powershell` leg. Drives the REAL dispatcher
    entrypoint with a PowerShell payload carrying the `ls | Select-Object
    -First N` shape and spies on the internal PowerShell leg to prove it is
    actually invoked -- not merely that `matchers` now contains
    "PowerShell"."""
    calls = []
    original = _head_tail_guard._check_head_tail_plumbing_powershell

    def _spy(cmd, payload=None):
        calls.append((cmd, payload))
        return original(cmd, payload=payload)

    import coordinator_core.bash_guards.guard_head_tail_rewrite as module

    orig_attr = module._check_head_tail_plumbing_powershell
    module._check_head_tail_plumbing_powershell = _spy
    try:
        raw = json.dumps(
            {
                "tool_name": "PowerShell",
                "tool_input": {"command": "ls . | Select-Object -First 5"},
                "session_id": "sess-bucket-d-head-tail-reachability",
                "cwd": ".",
            }
        )
        dispatch.evaluate_payload_json(raw)
    finally:
        module._check_head_tail_plumbing_powershell = orig_attr

    assert calls, (
        "the head-tail-plumbing-rewrite PowerShell leg "
        "(_check_head_tail_plumbing_powershell) was never invoked -- the "
        "dispatcher's per-entry matchers gate skipped this entry, or its "
        "call site never passed dialect=Dialect.POWERSHELL, before its "
        "fn() reached the PowerShell verdict path"
    )


def test_head_tail_plumbing_rewrite_bash_payload_never_reaches_the_powershell_leg():
    """Negative control for the spy above: a Bash-tool_name payload with the
    identical shape must still route to the BASH body, never the
    PowerShell leg -- confirms the spy is a meaningful signal, not a
    tautology that always fires."""
    calls = []

    import coordinator_core.bash_guards.guard_head_tail_rewrite as module

    original = module._check_head_tail_plumbing_powershell

    def _spy(cmd, payload=None):
        calls.append((cmd, payload))
        return original(cmd, payload=payload)

    module._check_head_tail_plumbing_powershell = _spy
    try:
        raw = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "find . -type f | head -n 5"},
                "session_id": "sess-bucket-d-head-tail-negative-control",
                "cwd": ".",
            }
        )
        dispatch.evaluate_payload_json(raw)
    finally:
        module._check_head_tail_plumbing_powershell = original

    assert not calls


def test_block_dev_repo_sentinel_removal_advisory_powershell_reached_via_dispatch():
    """`block-dev-repo-sentinel-removal-advisory`'s registered leg
    (`check_advisory`) already derives its dialect from `payload["tool_name"]`
    internally via `_dialect.dialect_from_tool_name` -- the registration's
    own `matchers` must actually admit a PowerShell payload for that internal
    dialect derivation to ever run. Drives the real dispatcher with a
    PowerShell removal of the sentinel basename and asserts the advisory
    fires (a live signal that the entry was reached and rendered), not a
    silent allow."""
    raw = json.dumps(
        {
            "tool_name": "PowerShell",
            "tool_input": {"command": "Remove-Item .coordinator-dev-repo"},
            "session_id": "sess-bucket-d-sentinel-removal-reachability",
            "cwd": ".",
        }
    )

    out = dispatch.evaluate_payload_json(raw)

    assert out is not None, (
        "block-dev-repo-sentinel-removal-advisory never fired for a "
        "PowerShell Remove-Item of the sentinel basename -- either the "
        "dispatcher's matchers gate skipped the entry, or the internal "
        "dialect_from_tool_name derivation never reached a recognized "
        "verdict"
    )
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "dev-repo guard" in json.dumps(out)


def test_block_dev_repo_sentinel_removal_advisory_matchers_widened_to_command_tool_names():
    """Registration-level pin: `block-dev-repo-sentinel-removal-advisory`'s
    `GuardEntry.matchers` must be (at least) `COMMAND_TOOL_NAMES`, the same
    universe the entry's own registered leg reads -- a regression narrowing
    this back to `("Bash",)` would silently disable the reachability proven
    above without failing any Bash-only test."""
    chain = dispatch._build_guard_chain(
        "echo hi", "sess-bucket-d-registration", ".", {"tool_name": "Bash"}, None, None
    )
    entries = {entry.name: entry for entry in chain}

    assert "block-dev-repo-sentinel-removal-advisory" in entries
    entry = entries["block-dev-repo-sentinel-removal-advisory"]
    assert "PowerShell" in entry.matchers
    assert "Bash" in entry.matchers


def test_head_tail_plumbing_rewrite_matchers_widened_to_command_tool_names():
    """Registration-level pin, sibling of the sentinel-removal one above:
    `head-tail-plumbing-rewrite`'s `GuardEntry.matchers` must be (at least)
    `COMMAND_TOOL_NAMES`, not the pre-C9 `("Bash",)` literal."""
    chain = dispatch._build_guard_chain(
        "echo hi", "sess-bucket-d-registration-head-tail", ".", {"tool_name": "Bash"}, None, None
    )
    entries = {entry.name: entry for entry in chain}

    assert "head-tail-plumbing-rewrite" in entries
    entry = entries["head-tail-plumbing-rewrite"]
    assert "PowerShell" in entry.matchers
    assert "Bash" in entry.matchers
