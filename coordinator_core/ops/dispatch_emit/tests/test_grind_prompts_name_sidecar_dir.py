"""Every grind-stage and adjudicate agent() call ends its prompt with exactly one sidecar clause."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import grind_compose as gc, pm_adjudication
from coordinator_core.ops.dispatch_emit.agent_sidecar import SIDECAR_DIR_MARKER_PREFIX, SIDECAR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet import _grind_scripts

_RUN_KEY = "run-1"
_CALL_SITE = re.compile(r"\bagent\(")
_OPTS_RE = re.compile(r"\{ label: '([^']+)', phase: '([^']+)'")
_STAGE_PHASES = {"Grind", pm_adjudication.ADJUDICATE_PHASE_TITLE, gc._REVIEW_FIX_PHASE}


def _stage_prompts(script: str) -> dict[str, str]:
    """label -> prompt text (the span from ``agent(`` to its option object) for every grind-stage call."""
    starts = [m.end() for m in _CALL_SITE.finditer(script)]
    found: dict[str, str] = {}
    for start, end in zip(starts, starts[1:] + [len(script)]):
        opts = _OPTS_RE.search(script, start, end)
        if opts and opts.group(2) in _STAGE_PHASES:
            found[opts.group(1)] = script[start : opts.start()]
    return found


def test_every_grind_stage_call_carries_one_marker_naming_its_label():
    for script in _grind_scripts():
        prompts = _stage_prompts(script)
        assert {"triage", "size", "fix", "verify-agent", "verify-op", "commit", "ledger-sweep:drain",
                "adjudicate", "review-fix-commit"} <= set(prompts)
        for label, prompt in prompts.items():
            assert prompt.count(SIDECAR_DIR_MARKER_PREFIX) == 1, label
            slug = re.sub(r"[^A-Za-z0-9._-]", "-", label)
            assert f"{SIDECAR_DIR_MARKER_PREFIX}{SIDECAR_ROOT}/grind/{_RUN_KEY}/{slug}/" in prompt, label
            assert "never skip" in prompt, label
