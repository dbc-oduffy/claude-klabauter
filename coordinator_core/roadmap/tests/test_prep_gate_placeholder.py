"""The bar refuses a scaffold placeholder, which is what lets the producer emit live.

WHY THIS PINS SOMETHING LOAD-BEARING. `coordinator/bin/coordinator-doc-new.py`
previously emitted `prime_exit_criterion` COMMENTED OUT, on the reasoning that a
live stub would be "a placeholder that CLEARS the gate without meaning anything".
That reasoning was correct about the gate as it then stood, and its consequence
was that every plan was born failing PRIME_EXIT — measured at 635 of 741 across
two repos, with plan-blitz's own 34 plans failing it 34/34.

The fix inverts the dependency: the gate refuses the marker, so the producer can
emit the key live and a plan is born with the field VISIBLE and unanswered rather
than absent and invisible. If this test goes green with `is_placeholder` gutted,
the scaffolder is free to emit a stub that certifies itself — the exact
form-filling failure the bar exists to prevent.

Spec backlink: DoE-claude coordinator/docs/wiki/mise-prepped-authoring-bar.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap.prep_gate import (
    NOT_PREPPED,
    evaluate_plan,
    is_placeholder,
    refusal_message,
)

_ANSWERED = {
    "statement": "The gate refuses a placeholder criterion.",
    "derived_from": "state/sizings/2026-09-08-example.yaml",
}


@pytest.mark.parametrize(
    "value",
    [
        "<REPLACE: one falsifiable sentence>",
        "  <REPLACE: with leading space, as block-scalar folding leaves it>",
        "TODO: write this",
        "TBD",
    ],
)
def test_markers_are_recognised(value: str) -> None:
    assert is_placeholder(value) is True


@pytest.mark.parametrize(
    "value",
    ["A real falsifiable sentence.", "state/sizings/x.yaml", "", None, 7, ["a"]],
)
def test_answers_and_non_strings_are_not_placeholders(value) -> None:
    assert is_placeholder(value) is False


def _plan(statement: str | None, derived_from: str | None) -> str:
    """A minimal plan document carrying only what PRIME_EXIT reads."""
    lines = ["---", "title: fixture", "status: draft", "kind: plan", "census: []"]
    if statement is not None or derived_from is not None:
        lines.append("prime_exit_criterion:")
        if statement is not None:
            lines.append(f"  statement: {statement!r}")
        if derived_from is not None:
            lines.append(f"  derived_from: {derived_from!r}")
    lines += ["---", "", "# fixture", ""]
    return "\n".join(lines)


def _prime_exit_of(text: str) -> dict:
    return evaluate_plan(
        Path("docs/plans/x.md"),
        text=text,
        root_names=frozenset(),
        siblings=(),
    )["classes"]["PRIME_EXIT"]


def test_placeholder_statement_does_not_certify() -> None:
    result = _prime_exit_of(_plan("<REPLACE: a sentence>", "state/sizings/x.yaml"))
    assert result["status"] != "PASS"
    assert result["kind"] == "prime-exit-placeholder"


def test_placeholder_derived_from_does_not_certify() -> None:
    result = _prime_exit_of(_plan("A real sentence.", "<REPLACE: a link>"))
    assert result["kind"] == "prime-exit-placeholder"


def test_an_answered_criterion_still_passes() -> None:
    """The refusal must not widen into the answered case it exists to admit."""
    result = _prime_exit_of(_plan(_ANSWERED["statement"], _ANSWERED["derived_from"]))
    assert result["status"] == "PASS"


def test_absent_and_placeholder_are_different_defects() -> None:
    """The repairs differ — one adds a key, the other replaces a marker — so a
    reader told the wrong noun fixes the wrong thing."""
    absent = _prime_exit_of(_plan(None, None))
    placeholder = _prime_exit_of(_plan("<REPLACE: x>", "state/sizings/x.yaml"))
    assert absent["kind"] == "prime-exit-absent"
    assert placeholder["kind"] == "prime-exit-placeholder"
    assert absent["detail"] != placeholder["detail"]


def test_refusal_names_the_converter() -> None:
    """A NOT-PREPPED plan authored before the bar names the runnable remedy.

    Cold-path remediation names a runnable script, never a slash command
    (claude-klabauter CLAUDE.md § Runtime conventions).
    """
    passing = {"status": "PASS", "detail": "", "kind": None, "withheld": []}
    message = refusal_message(
        Path("docs/plans/x.md"),
        NOT_PREPPED,
        {
            "SPINE": passing,
            "CENSUS": {
                "status": "DEFECT",
                "detail": "no census: key",
                "kind": "census-absent",
                "withheld": [],
            },
            "EXTERNAL_DEPS": passing,
            "PRIME_EXIT": passing,
        },
        [],
    )
    assert "mise-prep-upgrade.py" in message
    # A runnable script, not the surface that just failed.
    assert "/mise-en-place" not in message
    assert "/warp-speed-execute" not in message


def test_converter_named_by_the_gate_exists_and_is_runnable() -> None:
    """The remedy the message names must be a file that is actually there — a
    guard that names a fix nobody can run is the cold-path defect one level up.
    """
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "coordinator" / "bin" / "mise-prep-upgrade.py"
    assert script.is_file(), f"gate names {script}, which does not exist"
    assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")
