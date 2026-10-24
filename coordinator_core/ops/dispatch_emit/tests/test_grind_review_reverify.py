"""Grind route: Review re-verify sits before Review fixes and a red verdict withholds the review-fix commit."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_grind_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

_GUARD = (
    "const _reviewFixWithheld = _reviewReverify ? "
    "(_reviewReverify.status_after ?? _reviewReverify.status_before) === 'indeterminate'"
    " || ((_reviewReverify.status_after ?? _reviewReverify.status_before) === 'fail'"
    " && !(Array.isArray(_reviewReverify.failing) && _reviewReverify.failing.length > 0"
    " && _reviewReverify.failing.every((id) => (_reviewReverify.pre_existing ?? []).includes(id))))"
    " : !_verifyOnly;"
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
    assert "'indeterminate'" in _GUARD
    assert script.index("phase('Review re-verify')") < script.index(_GUARD) < script.index("phase('Review fixes')")


def test_review_fix_call_sits_under_the_negated_guard_and_logs_when_withheld():
    script = _compose()
    guard_at = script.index("if (!_reviewFixWithheld) {")
    fix_at = script.index("label: 'review-fix-commit'")
    else_at = script.index("} else {\n    log('review fixes withheld: targeted-tests-failed');")
    assert script.index(_GUARD) < guard_at < script.index("phase('Review fixes')") < fix_at < else_at


@pytest.mark.spawns_process
@pytest.mark.parametrize(
    ("verdict", "withheld"),
    [
        ({"status_before": "fail", "failing": ["t::a", "t::b"], "pre_existing": ["t::a", "t::b"]}, False),
        ({"status_before": "fail", "failing": ["t::a", "t::b"], "pre_existing": ["t::a"]}, True),
        ({"status_before": "fail", "failing": [], "pre_existing": []}, True),
        ({"status_before": "fail", "failing": ["t::a"]}, True),
        ({"status_before": "fail", "status_after": "pass", "failing": ["t::a"], "pre_existing": []}, False),
        ({"status_before": "indeterminate", "failing": [], "pre_existing": []}, True),
        ({"status_before": "pass", "failing": [], "pre_existing": []}, False),
    ],
)
def test_withhold_guard_spares_only_an_all_pre_existing_fail(verdict, withheld):
    import json

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose()
    start = script.index(_GUARD)
    expr = script[start:start + len(_GUARD)]
    program = (
        f"const _reviewReverify = {json.dumps(verdict)}; const _verifyOnly = false; "
        f"{expr} console.log(JSON.stringify(_reviewFixWithheld));"
    )
    out = subprocess.run([node, "-e", program], capture_output=True, text=True, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) is withheld


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
