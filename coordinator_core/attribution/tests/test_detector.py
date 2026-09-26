from coordinator_core.attribution import is_exempt_path, scan_text


def test_review_colon_shape_detected():
    text = "// Review: code-reviewer (Finding 3) — regression guard for the 403 path"
    assert scan_text(text, "src/handler.ts")


def test_persona_fnum_shape_detected():
    text = "# the Staff Engineer F1: guard against X"
    assert scan_text(text, "engine/foo.py")


def test_per_finding_paren_detected():
    text = "clamp the retry budget (per staff-eng finding 4)"
    assert scan_text(text, "engine/foo.py")


def test_review_corrected_detected():
    text = "# REVIEW-CORRECTED: widen the timeout"
    assert scan_text(text, "engine/foo.py")


def test_break_class_bare_doctrine_not_flagged():
    text = "# Break-class defects are fixed by default, not deferred."
    assert scan_text(text, "engine/foo.py") == []


def test_break_class_with_attribution_flagged():
    text = "# BREAK-CLASS per the Staff Engineer — fix before merge"
    assert scan_text(text, "engine/foo.py")


def test_bare_common_word_persona_not_flagged():
    text = "# the Game Dev Reviewer the cat walked across the keyboard during the demo"
    assert scan_text(text, "engine/foo.py") == []


def test_bare_yk_acronym_not_flagged():
    text = "# the VP-Product Reviewer is a units abbreviation used in the legacy import format"
    assert scan_text(text, "engine/foo.py") == []


def test_bare_review_word_not_flagged():
    text = "# This function performs a final review of the input before dispatch."
    assert scan_text(text, "engine/foo.py") == []


def test_bare_finding_word_not_flagged():
    text = "# The scanner reports its finding to the caller."
    assert scan_text(text, "engine/foo.py") == []


def test_exempt_path_state():
    text = "# Review: the Staff Engineer (Finding 3) — kept intentionally"
    assert scan_text(text, "state/handoffs/2026-09-25-note.md") == []


def test_exempt_path_docs_plans():
    text = "# Review: the Staff Engineer (Finding 3)"
    assert scan_text(text, "docs/plans/2026-09-25-plan.md") == []


def test_is_exempt_path_wiki():
    assert is_exempt_path("docs/wiki/some-topic.md")


def test_is_exempt_path_source_not_exempt():
    assert not is_exempt_path("src/handler.ts")
