"""The grind review gate skips the review wave and review-fix-commit when committed rows touched only record paths.

Node is barred here, so the emitted gate is checked over the rendered script and its regex literal is
applied with Python `re`.
"""
from __future__ import annotations

import ast
import re

import pytest

from coordinator_core.ops.dispatch_emit import record_paths

from .test_grind_compose import _compose

pytestmark = pytest.mark.complete

_SKIP_NO_ROW = "execute_review skipped: no row committed, nothing landed to review"
_SKIP_NO_PRODUCT = "execute_review skipped: no product file changed, nothing to review"


def _gate_region(script: str) -> str:
    start = script.index("const _reviewPaths = ")
    return script[start : script.index("finally { await _runClose(); }", start)]


def _emitted_regex(script: str) -> re.Pattern[str]:
    literal = re.search(r"const _reviewRecordRe = new RegExp\(('.*?')\);", script).group(1)
    return re.compile(ast.literal_eval(literal))


def test_gate_filters_product_paths_with_the_shared_record_regex() -> None:
    script = _compose()
    assert _emitted_regex(script).pattern == record_paths.record_prefix_js_regex()
    assert "const _reviewProductPaths = _reviewPaths.filter(" in script


def test_record_only_touched_files_are_not_product_paths() -> None:
    regex = _emitted_regex(_compose())
    for path in ("state/queue-grind/x.yaml", "docs/lessons/a.yaml", ".coordinator-local/x/y"):
        assert regex.search(path), path
    assert not regex.search("coordinator_core/x.py")


def test_skip_arm_precedes_the_review_wave_and_review_fix_commit() -> None:
    region = _gate_region(_compose())
    no_row = region.index("if (_reviewPaths.length === 0) {")
    no_product = region.index("} else if (_reviewProductPaths.length === 0) {")
    run_arm = region.index("} else {", no_product)
    assert no_row < region.index(_SKIP_NO_ROW) < no_product
    assert no_product < region.index(_SKIP_NO_PRODUCT) < run_arm
    wave = region.index("_reviewWave")
    fix = region.index("review-fix-commit")
    assert run_arm < wave and run_arm < fix
    assert _SKIP_NO_PRODUCT not in region[run_arm:]


def test_skip_arm_leaves_review_state_and_verdicts_at_their_initial_values() -> None:
    script = _compose()
    region = _gate_region(script)
    skip_arm = region[region.index("} else if (_reviewProductPaths.length === 0) {") : region.index("} else {", region.index(_SKIP_NO_PRODUCT))]
    assert "_reviewState" not in skip_arm and "_reviewVerdictsRecord" not in skip_arm
    assert "let _reviewState = 'not-run';\nlet _reviewVerdictsRecord = null;" in script


def test_review_wave_still_declares_every_committed_path() -> None:
    script = _compose()
    assert script.count("const _reviewPaths = ") == 1
    run_arm = _gate_region(script).split("} else {", 1)[1]
    assert "_reviewPaths" in run_arm
