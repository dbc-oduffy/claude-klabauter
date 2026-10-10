"""A review-only run's terminal test phase is scoped by the rows' own writes, never by the raw
``run_base..HEAD`` range: on a shared branch that range holds peers' commits and their tests."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_review_fix_reverification_and_cli_line import _wave_row

_BASE = "a" * 40
_WRITES = ["coordinator_core/ops/dispatch_emit/spine_read.py", "coordinator_core/ops/dispatch_emit/cli.py"]


def _terminal_prompt() -> str:
    script = compose_script(
        [[_wave_row("C1", _WRITES[:1]), _wave_row("C2", _WRITES[1:])]],
        name="wf",
        description="d",
        run_base_sha=_BASE,
        review_only=True,
        plan_path="docs/plans/p.md",
        **REVIEW_KW,
    )
    start = script.index("phase('Scoped test run')")
    return script[start : script.index("test:terminal", start)]


def test_review_only_diffs_are_limited_to_the_rows_writes():
    prompt = _terminal_prompt()
    paths = _WRITES

    for diff in re.findall(r"git diff --name-only \S+[^`]*", prompt):
        assert " -- " in diff and all(p in diff for p in paths), diff
    assert "git ls-files --others --exclude-standard -- " in prompt
    assert f"git diff --name-only {_BASE}`" not in prompt


def test_out_of_scope_failures_are_not_attributed_to_the_plan():
    prompt = _terminal_prompt()

    assert "outside it is a peer" in prompt
    assert "never count it in tests_failed" in prompt
