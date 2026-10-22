"""Grind route: Review re-verify sits before Review fixes and a red verdict withholds the review-fix commit."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_grind_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

_GUARD = (
    "const _reviewFixWithheld = _reviewReverify ? "
    "(_reviewReverify.status_after ?? _reviewReverify.status_before) === 'fail' : !_verifyOnly;"
)


def test_reverify_phase_follows_the_review_blocks_and_precedes_review_fixes():
    script = _compose()
    assert (
        script.index("phase('Review wave')")
        < script.index("phase('Review integration')")
        < script.index("phase('Review re-verify')")
        < script.index("phase('Review fixes')")
    )
    assert "'Review integration', 'Review re-verify', 'Review fixes'" in script


def test_script_carries_the_verbatim_withhold_guard():
    script = _compose()
    assert _GUARD in script
    assert script.index("phase('Review re-verify')") < script.index(_GUARD) < script.index("phase('Review fixes')")


def test_review_fix_call_sits_under_the_negated_guard_and_logs_when_withheld():
    script = _compose()
    guard_at = script.index("if (!_reviewFixWithheld) {")
    fix_at = script.index("label: 'review-fix-commit'")
    else_at = script.index("} else {\n    log('review fixes withheld: targeted-tests-failed');")
    assert script.index(_GUARD) < guard_at < script.index("phase('Review fixes')") < fix_at < else_at


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_composed_script_parses_as_an_async_function_body(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "g.js"
    path.write_text(_compose(), encoding="utf-8")
    program = (
        "const src = require('fs').readFileSync(process.argv[2], 'utf8')"
        ".replace(/^export const meta/m, 'const meta');"
        "new (Object.getPrototypeOf(async function () {}).constructor)("
        "'args', 'agent', 'parallel', 'pipeline', 'phase', src);"
    )
    out = subprocess.run([node, "-", str(path)], input=program, capture_output=True, text=True, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
