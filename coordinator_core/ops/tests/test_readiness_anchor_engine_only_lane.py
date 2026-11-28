"""plan.readiness_verdict lanes: an unevaluable anchor check is a non-blocking note naming its id,
with or without premise-checker output, while a blocking checker row or an evaluable failing
check still blocks."""

import json
import subprocess
from pathlib import Path

from coordinator_core.ops import plan_readiness_verdict as op

from coordinator_core.ops.tests import test_plan_readiness_verdict as base

_PIPE = (
    "anchor_premises:\n  - id: AP1\n    claim: bin has one entry\n    check:\n"
    "      command: ls -d bin | wc -l\n      expect: '1'\n"
)
_GREP_MISMATCH = (
    "anchor_premises:\n  - id: AP1\n    claim: file has three lines\n    check:\n"
    "      command: grep -c x data.txt\n      expect: '3'\n"
)


def _committed_data(root: Path) -> None:
    """Anchor checks read HEAD, so the checked file must be committed there."""
    (root / "data.txt").write_text("x\nx\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "data.txt"), ("commit", "-q", "-m", "base")):
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _assert_unevaluable_note_not_blocking(reply):
    assert reply["verdict"] == "ready"
    assert reply["blocking"] == [] and reply["mechanical_repairs"] == []
    assert not any(b.startswith("anchor-premise-no-verdict") for b in reply["blocking"])
    assert "anchor check not evaluable in-process, unchecked, not blocking" in reply["reason"]
    assert "AP1" in reply["reason"]


def test_no_premise_input_notes_an_unevaluable_check_instead_of_pulling(tmp_path):
    common = base._anchor_plan(tmp_path, _PIPE)
    reply = op._handler({"plan": base.REL}, common)
    base._validate(reply)
    _assert_unevaluable_note_not_blocking(reply)
    assert reply["pull_class"] == "none" and reply["blocking_findings"] == 0
    assert "unsupported shell operator '|'" in reply["reason"]


def test_premise_param_with_a_clean_checker_does_not_block_an_unevaluable_check(tmp_path):
    common = base._anchor_plan(tmp_path, _PIPE)
    reply = op._handler({"plan": base.REL, "premise": {"rows": []}}, common)
    base._validate(reply)
    _assert_unevaluable_note_not_blocking(reply)


def test_premise_check_review_does_not_block_an_unevaluable_check(tmp_path):
    common = base._anchor_plan(tmp_path, _PIPE)
    reply = op._handler(
        {"plan": base.REL, "reviews": [{"reviewer": "premise-check", "verdict": "pass"}]}, common
    )
    base._validate(reply)
    _assert_unevaluable_note_not_blocking(reply)


def test_checker_that_returned_a_blocking_row_still_blocks_an_unevaluable_check(tmp_path):
    common = base._anchor_plan(tmp_path, _PIPE)
    rows = [{"verdict": "UNRESOLVED", "citation": "some premise"}]
    reply = op._handler({"plan": base.REL, "premise": {"rows": rows}}, common)
    base._validate(reply)
    assert reply["verdict"] == "judgment"
    assert any(b.startswith("premise-check: UNRESOLVED") for b in reply["blocking"])


def test_evaluable_failing_check_still_blocks_when_the_checker_ran(tmp_path):
    common = base._anchor_plan(tmp_path, _GREP_MISMATCH)
    _committed_data(tmp_path)
    reply = op._handler({"plan": base.REL, "premise": {"rows": []}}, common)
    base._validate(reply)
    assert reply["verdict"] == "judgment"
    assert any(b.startswith("anchor-premise-CONTRADICTED: AP1") for b in reply["blocking"])


def test_mismatching_grep_check_still_blocks_contradicted_without_a_checker(tmp_path):
    common = base._anchor_plan(tmp_path, _GREP_MISMATCH)
    _committed_data(tmp_path)
    reply = op._handler({"plan": base.REL}, common)
    base._validate(reply)
    assert reply["verdict"] == "judgment"
    assert any(b.startswith("anchor-premise-CONTRADICTED: AP1") for b in reply["blocking"])
    assert reply["mechanical_repairs"] == []
