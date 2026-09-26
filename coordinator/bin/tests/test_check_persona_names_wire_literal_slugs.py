"""Regression: the mirror's identity gate permits a persona slug inside a WIRE LITERAL.

THE DEADLOCK THIS CLOSES
------------------------
state/bug-backlog/2026-08-23-publish-persona-scrub-rewrites-wire-lite-042dfc9d016e.yaml
(P1): the publish transform was rewriting a lowercase persona slug glued into a compound
value -- a schema enum member (`"patrik-review"`), a sidecar-filename constant
(`".patrik-review.md"`), a reviewer-priority tuple -- into a placeholder carrying SPACES.
The mirror then shipped `"the Staff Engineer-review"` where the upstream tree emits
`the Staff Engineer-review`, so an install running the mirror rejected the records upstream produces.

The transform-side fix holds those literals unrewritten
(coordinator_core/percolate/substitute.py::_FUNCTIONAL_HOLD_LINE_PATTERNS). That alone
would relocate the breakage rather than close it: this gate bans the persona names
case-INSENSITIVELY, so every held literal becomes an identity finding and no publish
completes. Hold upstream and permit here are one change, and this pins the second half.

NEGATIVE SPEC, and it is the load-bearing half: the permit is lowercase-only and
slug-shouldered. A capitalised DISPLAY name stays banned wherever it appears, quoted or
not, and a quoted PROSE sentence that merely mentions a name -- space on the shoulder --
stays a finding. Widening this to `[^"]*` shoulders or to the capitalised forms would turn
the gate into a blanket allow for any name inside any string literal.

Fixtures assemble their capitalised tokens from fragments for the same reason the checker
does: a contiguous literal in these bytes would itself be the residual.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "dist"
    / "mirror-native"
    / "claude-klabauter"
    / ".github"
    / "scripts"
    / "check-persona-names.py"
)

_DISPLAY = "Pat" + "rik"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_persona_names", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_module()


WIRE_LITERALS = (
    '    "patrik-review",',
    '    ".patrik-review.md",',
    '    ".zoli-review.md",',
    '    ".review-zoli",',
    '        "docs/plans/2026-07-14-real.patrik-review.md",',
    '    sidecar = plans_dir / "foo.review-zoli.md"',
    '_REVIEWER_SIDECAR_PRIORITY = ("patrik", "eng-director", "zoli")',
    '    assert by_slug["patrik"]["agent_file"] == "coordinator/agents/staff-eng.md"',
    '    _SYNTHESIZER_SLUG = "zoli"',
)

STILL_A_FINDING = (
    f"# Review: {_DISPLAY} F12 (auto-push tightening).",
    f'    "{_DISPLAY}-review",',
    '    msg = "decision, Review: the Director of Engineering cutover review F1)"',
    "# see 2026-07-15-the Data Science Reviewer-coverage-graph-walk.md",
)


@pytest.mark.parametrize("line", WIRE_LITERALS)
def test_wire_literal_slug_is_not_a_finding(line, gate):
    assert gate.findings_in(line, "coordinator_core/x.py") == []


@pytest.mark.parametrize("line", STILL_A_FINDING)
def test_display_name_and_prose_mention_remain_findings(line, gate):
    assert gate.findings_in(line, "coordinator_core/x.py") != []


def test_permit_does_not_leak_across_a_string_boundary(gate):
    line = f'    KINDS = ["patrik-review"]  # authored by {_DISPLAY}'
    findings = gate.findings_in(line, "coordinator_core/x.py")
    assert [matched for _, matched in findings] == [_DISPLAY]
