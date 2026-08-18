"""
Tests for coordinator_core.workstream_complete.completion_verdict — C1a's
two readers plus the module-level properties (AC2/AC3/AC4/AC7) that govern
every reader in this module, including C1b's three appended later.

Spec backlink: pln-one-completion-verdict-for-wor-ea96e2, chunk C1a.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest

from coordinator_core.workstream_complete import completion_verdict as cv
from coordinator_core.workstream_complete.directives_spine_worklist import SpineRowItem

MODULE_PATH = Path(cv.__file__)
MODULE_SOURCE = MODULE_PATH.read_text(encoding="utf-8")


def _source_with_docstrings_and_comments_blanked(source: str) -> str:
    """Returns `source` with every docstring's text and every `#` comment
    replaced by blank lines/segments — leaving only executable code and
    non-docstring string literals for a vocabulary scan. Docstrings are
    where AC4's "comments explaining absence" legitimately name the
    banned words; executable code and residue-carrying strings must not.
    """
    lines = source.splitlines()
    tree = ast.parse(source)
    docstring_line_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) \
                    and isinstance(node.body[0].value.value, str):
                doc_node = node.body[0]
                docstring_line_ranges.append((doc_node.lineno, doc_node.end_lineno))
    for start, end in docstring_line_ranges:
        for lineno in range(start, end + 1):
            lines[lineno - 1] = ""
    blanked = "\n".join(line.split("#", 1)[0] for line in lines)
    return blanked


# --- open_spine_row_worklist ------------------------------------------

@pytest.mark.parametrize(
    "verdict, expected_status",
    [
        ("applicable", "open"),
        ("not-applicable", "not-applicable"),
        ("indeterminate", "indeterminate"),
    ],
)
def test_open_spine_row_worklist_maps_every_declared_verdict(verdict, expected_status):
    payload = {
        "applies": verdict == "applicable",
        "rows": (),
        "open_count": 0,
        "warn_text": None,
        "summary_line": "summary",
        "verdict": verdict,
    }
    reading = cv.open_spine_row_worklist(payload)
    assert reading.status == expected_status
    assert reading.reason is None


def test_open_spine_row_worklist_open_emits_residue_per_row():
    rows = (SpineRowItem(id="row-1", title="t1", waived=False), SpineRowItem(id="row-2", title="t2", waived=False))
    payload = {
        "applies": True,
        "rows": rows,
        "open_count": 2,
        "warn_text": "two open rows",
        "summary_line": "two open rows remain",
        "verdict": "applicable",
    }
    reading = cv.open_spine_row_worklist(payload)
    assert reading.status == "open"
    assert len(reading.residue_items) == 2
    ids = {item["reference"] for item in reading.residue_items}
    assert ids == {"row-1", "row-2"}
    for item in reading.residue_items:
        assert item["gate"] == "open_spine_row_worklist"
        assert item["summary"] == "two open rows remain"


def test_open_spine_row_worklist_unrecognised_value_is_indeterminate():
    payload = {
        "applies": True,
        "rows": (),
        "open_count": 0,
        "warn_text": None,
        "summary_line": "summary",
        "verdict": "some-fourth-string",
    }
    reading = cv.open_spine_row_worklist(payload)
    assert reading.status == "indeterminate"
    assert "some-fourth-string" in reading.reason


def test_open_spine_row_worklist_unrecognised_shape_is_indeterminate():
    payload = {"applies": False, "summary_line": "no verdict key at all"}
    reading = cv.open_spine_row_worklist(payload)
    assert reading.status == "indeterminate"
    assert "verdict" in reading.reason


def test_open_spine_row_worklist_applies_false_never_read_uniformly():
    # AC3 regression, restated for this gate alone: applies=False with
    # verdict=indeterminate must read indeterminate, never clean.
    payload = {
        "applies": False,
        "rows": (),
        "open_count": 0,
        "warn_text": None,
        "summary_line": "no governing plan resolved",
        "verdict": "indeterminate",
    }
    reading = cv.open_spine_row_worklist(payload)
    assert reading.status == "indeterminate"
    assert reading.status != "clean"


# --- landed_reconciliation ----------------------------------------------

@pytest.mark.parametrize(
    "verdict, expected_status",
    [
        ("applicable", "open"),
        ("not-applicable", "not-applicable"),
        ("indeterminate", "indeterminate"),
    ],
)
def test_landed_reconciliation_maps_every_declared_verdict(verdict, expected_status):
    payload = {
        "applies": verdict == "applicable",
        "open_count": 1 if verdict == "applicable" else 0,
        "total_count": 3,
        "warn_text": None,
        "summary_line": "summary",
        "verdict": verdict,
    }
    reading = cv.landed_reconciliation(payload)
    assert reading.status == expected_status
    assert reading.reason is None


def test_landed_reconciliation_open_emits_residue():
    payload = {
        "applies": True,
        "open_count": 2,
        "total_count": 5,
        "warn_text": "2 unreconciled",
        "summary_line": "2 of 5 ACs unreconciled",
        "verdict": "applicable",
    }
    reading = cv.landed_reconciliation(payload)
    assert reading.status == "open"
    assert len(reading.residue_items) == 1
    item = reading.residue_items[0]
    assert item["gate"] == "landed_reconciliation"
    assert item["reference"] == "2/5 unreconciled"
    assert item["summary"] == "2 of 5 ACs unreconciled"


def test_landed_reconciliation_unrecognised_value_is_indeterminate():
    payload = {
        "applies": True,
        "open_count": 0,
        "total_count": 0,
        "warn_text": None,
        "summary_line": "summary",
        "verdict": "a-new-fourth-value",
    }
    reading = cv.landed_reconciliation(payload)
    assert reading.status == "indeterminate"
    assert "a-new-fourth-value" in reading.reason


def test_landed_reconciliation_unrecognised_shape_is_indeterminate():
    payload = {"applies": False, "summary_line": "no verdict key"}
    reading = cv.landed_reconciliation(payload)
    assert reading.status == "indeterminate"
    assert "verdict" in reading.reason


def test_landed_reconciliation_applies_false_never_read_uniformly():
    payload = {
        "applies": False,
        "open_count": 0,
        "total_count": 0,
        "warn_text": None,
        "summary_line": "no landed plan reconciled",
        "verdict": "indeterminate",
    }
    reading = cv.landed_reconciliation(payload)
    assert reading.status == "indeterminate"
    assert reading.status != "clean"


# --- module-level properties (govern C1a and C1b alike) -----------------

_BANNED_VOCAB = ("trampoline", "wont_do", "won't-do", "backlog", "spinoff")


def test_ac4_vocabulary_ban_outside_docstrings_and_comments():
    """AC4: grep-assert over completion_verdict.py's own source for the
    banned next-step vocabulary, outside comments/docstrings explaining
    its deliberate absence (the module docstring's own vocabulary-ban
    paragraph is the sanctioned exception; this scan blanks it out before
    checking).
    """
    scanned = _source_with_docstrings_and_comments_blanked(MODULE_SOURCE)
    for word in _BANNED_VOCAB:
        assert not re.search(re.escape(word), scanned, re.IGNORECASE), (
            f"banned vocabulary {word!r} found outside a docstring/comment "
            f"in completion_verdict.py"
        )


def test_ac7_purity_no_gate_computation_import():
    """AC7: this module imports and calls no gate-computation function."""
    banned_symbols = (
        "compute_open_spine_row_gate",
        "compute_landed_reconciliation_gate",
        "compute_completeness_checklist_gate",
        "build_envelope",
    )
    for symbol in banned_symbols:
        assert symbol not in MODULE_SOURCE, f"{symbol!r} referenced in completion_verdict.py"


def test_ac7_purity_no_disk_or_subprocess_calls():
    scanned = _source_with_docstrings_and_comments_blanked(MODULE_SOURCE)
    disallowed = ("subprocess", "open(", "Path(", "os.system", "requests.")
    for token in disallowed:
        assert token not in scanned, f"{token!r} referenced in completion_verdict.py's code"


def test_gate_reading_is_named_tuple_with_expected_fields():
    reading = cv.GateReading(status="clean", residue_items=(), reason=None)
    assert reading.status == "clean"
    assert reading.residue_items == ()
    assert reading.reason is None
    assert isinstance(reading, tuple)


def test_readers_are_pure_functions_of_their_argument():
    sig_spine = inspect.signature(cv.open_spine_row_worklist)
    sig_landed = inspect.signature(cv.landed_reconciliation)
    assert list(sig_spine.parameters) == ["payload"]
    assert list(sig_landed.parameters) == ["payload"]


# =========================================================================
# C1b — three remaining readers
# Spec backlink: pln-one-completion-verdict-for-wor-ea96e2, chunk C1b.
# =========================================================================

# --- completeness_checklist -----------------------------------------------

@pytest.mark.parametrize(
    "verdict, expected_status",
    [
        ("not-applicable", "not-applicable"),
        ("indeterminate", "indeterminate"),
        ("clean", "clean"),
        ("open", "open"),
    ],
)
def test_completeness_checklist_maps_every_declared_verdict(verdict, expected_status):
    payload = {
        "applies": verdict in ("clean", "open"),
        "items": (),
        "unverified_count": 1 if verdict == "open" else 0,
        "warn_text": None,
        "summary_line": "summary",
        "verdict": verdict,
    }
    reading = cv.completeness_checklist(payload)
    assert reading.status == expected_status
    assert reading.reason is None


def test_completeness_checklist_open_emits_residue_per_unverified_item():
    payload = {
        "applies": True,
        "items": (
            {"item_class": "live", "assertion": "app boots", "probe": "", "verified": False},
            {"item_class": "restart-gated", "assertion": "cache warm", "probe": "", "verified": True},
        ),
        "unverified_count": 1,
        "warn_text": "WARN [completeness-checklist]: 1 completeness item(s) unverified",
        "summary_line": "Completeness checklist: 1 items unverified — WARN emitted",
        "verdict": "open",
    }
    reading = cv.completeness_checklist(payload)
    assert reading.status == "open"
    assert len(reading.residue_items) == 1
    item = reading.residue_items[0]
    assert item["gate"] == "completeness_checklist"
    assert item["reference"] == "live: app boots"
    assert item["summary"] == payload["summary_line"]


def test_completeness_checklist_unrecognised_value_is_indeterminate():
    payload = {"applies": False, "items": (), "unverified_count": 0, "warn_text": None,
               "summary_line": "summary", "verdict": "a-new-fifth-value"}
    reading = cv.completeness_checklist(payload)
    assert reading.status == "indeterminate"
    assert "a-new-fifth-value" in reading.reason


def test_completeness_checklist_unrecognised_shape_is_indeterminate():
    payload = {"applies": False, "summary_line": "no verdict key"}
    reading = cv.completeness_checklist(payload)
    assert reading.status == "indeterminate"
    assert "verdict" in reading.reason


# --- consumed_handoff_completeness ----------------------------------------

def _element(handoff="a.md", leg_a_verdict="clean", leg_b_verdict="no-children",
             leg_a_detail="all ticked", leg_b_detail="no live successor"):
    return {
        "handoff": handoff,
        "blocks": leg_a_verdict == "open" or leg_b_verdict == "live-child",
        "leg_a": {"verdict": leg_a_verdict, "detail": leg_a_detail, "open": None, "total": None},
        "leg_b": {"verdict": leg_b_verdict, "detail": leg_b_detail, "exit_code": 1, "error": None},
    }


@pytest.mark.parametrize(
    "leg_a_verdict, leg_b_verdict, expected_status",
    [
        ("open", "no-children", "open"),
        ("open", "live-child", "open"),
        ("open", "indeterminate", "open"),
        ("clean", "live-child", "open"),
        ("clean", "no-children", "clean"),
        ("clean", "indeterminate", "indeterminate"),
        ("not-applicable", "live-child", "open"),
        ("not-applicable", "no-children", "not-applicable"),
        ("not-applicable", "indeterminate", "indeterminate"),
        ("indeterminate", "live-child", "open"),
        ("indeterminate", "no-children", "indeterminate"),
        ("indeterminate", "indeterminate", "indeterminate"),
    ],
)
def test_consumed_handoff_completeness_element_reduction_is_total(leg_a_verdict, leg_b_verdict, expected_status):
    payload = {"applies": True, "blocks": False, "elements": (_element(leg_a_verdict=leg_a_verdict, leg_b_verdict=leg_b_verdict),)}
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == expected_status


def test_consumed_handoff_completeness_multi_element_clean_is_gate_level_clean():
    """Distinguishes a real multi-element `clean` reduction from the
    single-element case covered by the parametrized table above: two
    elements, both reducing to `clean`, with no `open`/`indeterminate`/
    `not-applicable` element in the mix, must still reduce to gate-level
    `clean` — not by coincidence of being a single-element case."""
    payload = {
        "applies": True,
        "blocks": False,
        "elements": (
            _element(handoff="a.md", leg_a_verdict="clean", leg_b_verdict="no-children"),
            _element(handoff="b.md", leg_a_verdict="clean", leg_b_verdict="no-children"),
        ),
    }
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == "clean"
    assert reading.residue_items == ()


def test_consumed_handoff_completeness_open_wins_across_elements():
    payload = {
        "applies": True,
        "blocks": True,
        "elements": (
            _element(handoff="clean.md", leg_a_verdict="clean", leg_b_verdict="no-children"),
            _element(handoff="open.md", leg_a_verdict="open", leg_b_verdict="no-children"),
        ),
    }
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == "open"
    assert len(reading.residue_items) == 1
    item = reading.residue_items[0]
    assert item["gate"] == "consumed_handoff_completeness"
    assert item["reference"] == "open.md"


def test_consumed_handoff_completeness_indeterminate_wins_over_not_applicable():
    payload = {
        "applies": True,
        "blocks": False,
        "elements": (
            _element(handoff="na.md", leg_a_verdict="not-applicable", leg_b_verdict="no-children"),
            _element(handoff="ind.md", leg_a_verdict="indeterminate", leg_b_verdict="no-children"),
        ),
    }
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == "indeterminate"


def test_consumed_handoff_completeness_no_elements_is_not_applicable():
    payload = {"applies": False, "blocks": False, "elements": ()}
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == "not-applicable"
    assert reading.residue_items == ()


def test_consumed_handoff_completeness_unrecognised_leg_value_is_indeterminate():
    payload = {"applies": True, "blocks": False, "elements": (_element(leg_a_verdict="a-new-value"),)}
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == "indeterminate"
    assert "a-new-value" in reading.reason


def test_consumed_handoff_completeness_element_missing_leg_is_indeterminate():
    payload = {"applies": True, "blocks": False, "elements": ({"handoff": "x.md", "blocks": False, "leg_a": {"verdict": "clean"}},)}
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == "indeterminate"
    assert "leg_b" in reading.reason


def test_consumed_handoff_completeness_unrecognised_shape_is_indeterminate():
    payload = {"applies": False, "blocks": False}
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == "indeterminate"
    assert "elements" in reading.reason


def test_consumed_handoff_completeness_never_reads_applies_or_blocks():
    """This reader's TOTAL mapping is over per-element leg verdicts only —
    a payload whose `applies`/`blocks` bits disagree with what the leg
    verdicts actually say must still be read off the legs, never the
    summary bits."""
    payload = {
        "applies": False,  # lies: elements are non-empty and one is open
        "blocks": False,  # lies: one element's leg_a is "open"
        "elements": (_element(leg_a_verdict="open", leg_b_verdict="no-children"),),
    }
    reading = cv.consumed_handoff_completeness(payload)
    assert reading.status == "open"


# --- review_scale ----------------------------------------------------------

def test_review_scale_resolved_true_is_not_applicable_narration_only():
    payload = {"resolved": True, "row": 2, "scale": "code-reviewer", "partition_mandatory": False,
               "commit_message_names_change": True, "reason": "small fix"}
    reading = cv.review_scale(payload)
    assert reading.status == "not-applicable"
    assert reading.residue_items == ()
    assert reading.reason is None


def test_review_scale_resolved_false_is_indeterminate():
    payload = {"resolved": False, "row": None, "scale": "unresolved", "partition_mandatory": False,
               "commit_message_names_change": False, "reason": "row 4 not yet resolved"}
    reading = cv.review_scale(payload)
    assert reading.status == "indeterminate"
    assert reading.residue_items == ()


def test_review_scale_unrecognised_value_is_indeterminate():
    payload = {"resolved": "yes", "row": 2, "scale": "code-reviewer"}
    reading = cv.review_scale(payload)
    assert reading.status == "indeterminate"
    assert "yes" in reading.reason


def test_review_scale_unrecognised_shape_is_indeterminate():
    payload = {"row": 2, "scale": "code-reviewer"}
    reading = cv.review_scale(payload)
    assert reading.status == "indeterminate"
    assert "resolved" in reading.reason


# --- AC3 regression: the same `applies: false` boolean maps to two ---------
# --- different readings, because each reader consults its own gate's ------
# --- verdict field and never the boolean. ----------------------------------

def test_applies_false_reads_differently_across_gates_because_each_reader_reads_its_own_verdict_never_the_boolean():
    """AC3 regression, restated against C0's post-fix `completeness_checklist`
    payload shape. Both gates below report `applies: false` in the SAME
    envelope; `completeness_checklist` means "not chain-terminal / no
    checklist field" (not-applicable) while `open_spine_row_worklist` means
    "no governing plan resolved / unreadable" (indeterminate). A rollup
    reading `applies: false` uniformly as one status would collapse these
    two different meanings — this module's readers never do, because
    neither one looks at `applies` at all."""
    completeness_checklist_payload = {
        "applies": False,
        "items": (),
        "unverified_count": 0,
        "warn_text": None,
        "summary_line": "Completeness checklist: all verified / not applicable",
        "verdict": "not-applicable",
    }
    open_spine_row_worklist_payload = {
        "applies": False,
        "rows": (),
        "open_count": 0,
        "warn_text": None,
        "summary_line": "no governing plan resolved",
        "verdict": "indeterminate",
    }

    checklist_reading = cv.completeness_checklist(completeness_checklist_payload)
    spine_reading = cv.open_spine_row_worklist(open_spine_row_worklist_payload)

    assert checklist_reading.status == "not-applicable"
    assert spine_reading.status == "indeterminate"
    assert checklist_reading.status != spine_reading.status
