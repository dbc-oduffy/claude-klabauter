from coordinator_core.attribution import (
    is_exempt_path,
    scan_text,
    strip_review_annotations,
)


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


def test_is_exempt_path_no_longer_over_exempts_substring_match():
    # Anchored to repo-root prefixes only -- "sidecar" as a substring
    # anywhere in the path must no longer exempt it (the old check wrongly
    # exempted 54 tracked non-md source files this way).
    assert not is_exempt_path("coordinator/bin/provision-sidecar.py")


def test_is_exempt_path_state_prefix():
    assert is_exempt_path("state/x.py")


def test_is_exempt_path_docs_reference_md_still_exempt_via_extension():
    assert is_exempt_path("docs/reference/x.md")


def test_is_exempt_path_attribution_package_itself():
    assert is_exempt_path("coordinator_core/attribution/patterns.py")


def test_is_exempt_path_windows_separator_state_prefix():
    assert is_exempt_path("state\\handoffs\\2026-09-25-note.md")


def test_is_exempt_path_windows_separator_non_exempt_source():
    assert not is_exempt_path("coordinator_core\\bin\\provision-sidecar.py")


def test_review_colon_namespaced_form_detected():
    text = "Review: coordinator:code-reviewer — x"
    assert scan_text(text, "a.py")


def test_review_colon_paren_finding_trailer_detected():
    text = "Review: code-reviewer (Finding 3)"
    assert scan_text(text, "a.py")


def test_review_colon_paren_f_trailer_detected():
    text = "Review: code-reviewer (F1)"
    assert scan_text(text, "a.py")


def test_review_colon_paren_multi_f_trailer_detected():
    text = "Review: code-reviewer (F1, F2)"
    assert scan_text(text, "a.py")


def test_review_colon_paren_dated_f_trailer_detected():
    text = "Review: code-reviewer (2026-09-25 F3)"
    assert scan_text(text, "a.py")


def test_review_colon_bare_doctrine_break_class_not_flagged():
    text = "# Break-class defects are fixed by default."
    assert scan_text(text, "a.py") == []


def test_strip_review_annotations_drops_html_comment_line():
    text = "keep this\n<!-- Review: coordinator:eng-director (finding 7) -->\nkeep that\n"
    assert strip_review_annotations(text) == "keep this\nkeep that\n"


def test_strip_review_annotations_drops_hash_comment_line():
    text = "before\n# Review: code-reviewer (Finding 3)\nafter\n"
    assert strip_review_annotations(text) == "before\nafter\n"


def test_strip_review_annotations_leaves_other_lines_byte_identical():
    text = "line one\nline two — nothing to strip\n"
    assert strip_review_annotations(text) == text


def test_strip_review_annotations_leaves_mixed_content_line_alone():
    # A line carrying other content alongside a "# Review:" fragment is not
    # itself the whole-line annotation shape and must be left alone.
    text = "value = 1  # not a Review: annotation on its own line\n"
    assert strip_review_annotations(text) == text
