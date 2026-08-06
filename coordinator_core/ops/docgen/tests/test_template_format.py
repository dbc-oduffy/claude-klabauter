"""Round-trip + idiom-coverage tests for coordinator_core.ops.docgen.template_format (C2).

Covers:
  - every staged template file parses + validates against FORMAT_VERSION
  - format round-trip: load -> re-serialize -> re-parse -> re-validate is lossless
  - all 22 extracted types are present (the plan's "extract ALL 22" requirement)
  - all 4 conditional idioms (present_as_null, optional_omit, list_emit_if_present,
    value_or_literal_fallback) are exercised across the corpus, and no unsanctioned
    field kind sneaks in (AC3)

Spec backlink: docs/plans/2026-07-21-strang-12-doc-generation-strangle.md § C2 (AC3)
"""

from __future__ import annotations

import json
import re

import pytest

from coordinator_core.ops.docgen import template_format as tf

# The full 22-type surface this chunk extracts (Problem section: all 22
# `_scaffold_*` bodies in example-doctrine-repo's coordinator-doc-new, with _scaffold_sidecar's
# 4-way if/elif split into 4 keyed template entries per the plan body).
EXPECTED_DOC_TYPES = frozenset({
    "handoff",
    "recovery",
    "spinoff",
    "roadmap-baton",
    "goal-seed",
    "roadmap-seed",
    "memo",
    "plan",
    "decision",
    "audit-record",
    "problem-set",
    "completion",
    "health-status",
    "goal",
    "strategic-self-description",
    "research-synthesis",
    "run-report",
    "review-findings",
    "review",
    "prior-art-check",
    "plan-coverage-check",
    "docs-check",
})


def _all_template_paths():
    return sorted(tf.templates_dir().glob("*.json"))


def test_exactly_22_templates_staged():
    paths = _all_template_paths()
    assert len(paths) == 22, f"expected 22 templates, found {len(paths)}: {[p.name for p in paths]}"


def test_every_template_parses_and_validates():
    for path in _all_template_paths():
        data = tf.load_template(path)  # raises TemplateFormatError on defect
        errors = tf.validate_template(data)
        assert errors == [], f"{path.name}: {errors}"


def test_doc_type_set_is_the_full_22():
    assert tf.available_template_types() == sorted(EXPECTED_DOC_TYPES)


def test_doc_type_matches_filename_stem_family():
    # Not a strict 1:1 (hyphen vs underscore), but every template's doc_type
    # must be one of the expected 22 and every file must be distinct.
    seen = set()
    for path in _all_template_paths():
        data = tf.load_template(path)
        doc_type = data["doc_type"]
        assert doc_type in EXPECTED_DOC_TYPES, f"{path.name}: unexpected doc_type {doc_type!r}"
        assert doc_type not in seen, f"duplicate doc_type {doc_type!r} across templates"
        seen.add(doc_type)


@pytest.mark.parametrize("path", _all_template_paths(), ids=lambda p: p.name)
def test_format_round_trip_is_lossless(path):
    """Load -> serialize -> reparse -> revalidate reproduces the identical structure.

    This is the format's round-trip contract (plan C2 body: "Test: format
    round-trip over every extracted type") — not a byte-identity check against
    the live example-doctrine-repo oracle (that harness is C6's, against rendered *output*, not
    this data-format's serialization).
    """
    original = tf.load_template(path)
    dumped = json.dumps(original, sort_keys=True)
    reloaded = json.loads(dumped)
    assert reloaded == original
    assert tf.validate_template(reloaded) == []


def test_all_conditional_idioms_are_exercised():
    # Count-agnostic name and body (Review: code-reviewer — CONDITIONAL_FIELD_KINDS
    # is the live source of truth for how many idioms exist; naming/asserting a
    # fixed count here would go stale again the next time the set grows).
    seen_kinds: set[str] = set()
    for path in _all_template_paths():
        data = tf.load_template(path)
        frontmatter = data.get("frontmatter")
        if not frontmatter:
            continue
        for field in frontmatter["fields"]:
            seen_kinds.add(field["kind"])
    missing = tf.CONDITIONAL_FIELD_KINDS - seen_kinds
    assert not missing, f"conditional idiom(s) never exercised across the 22 templates: {missing}"


def test_no_field_kind_outside_the_sanctioned_set():
    """AC3: 'no idiom requires an escape hatch to Python' — structurally enforced.

    Every field kind used by every template must be in FIELD_KINDS; this is also
    covered by validate_template (called in test_every_template_parses_and_validates)
    but asserted directly here so a future kind addition is caught even if a
    validator bug ever let it slip through silently.
    """
    for path in _all_template_paths():
        raw = json.loads(path.read_text(encoding="utf-8"))
        frontmatter = raw.get("frontmatter")
        if not frontmatter:
            continue
        for field in frontmatter["fields"]:
            assert field["kind"] in tf.FIELD_KINDS, f"{path.name}: unsanctioned kind {field['kind']!r}"


def test_whole_document_style_forbids_body():
    for path in _all_template_paths():
        data = tf.load_template(path)
        frontmatter = data.get("frontmatter")
        if frontmatter and frontmatter["style"] == "whole_document":
            assert data["body"] is None, f"{path.name}: whole_document style must carry body: null"


def test_unknown_field_kind_is_rejected():
    bad = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "bogus",
        "frontmatter": {
            "style": "fenced",
            "fields": [{"kind": "eval_python", "code": "os.system('rm -rf /')"}],
        },
        "body": None,
    }
    errors = tf.validate_template(bad)
    assert any("unknown field kind" in e for e in errors)


def test_wrong_format_version_is_rejected():
    bad = {"format_version": "docgen-template/v0", "doc_type": "x", "frontmatter": None, "body": None}
    errors = tf.validate_template(bad)
    assert any("format_version" in e for e in errors)


def test_missing_required_key_is_rejected():
    bad = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "x",
        "frontmatter": {
            "style": "fenced",
            "fields": [{"kind": "present_as_null", "key": "foo"}],  # missing 'field'/'quote'
        },
        "body": None,
    }
    errors = tf.validate_template(bad)
    assert any("missing required key" in e for e in errors)


def test_fallback_line_must_be_a_string():
    bad = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "x",
        "frontmatter": {
            "style": "fenced",
            "fields": [
                {
                    "kind": "value_or_literal_fallback",
                    "key": "foo",
                    "field": "foo",
                    "quote": False,
                    "fallback_line": 123,
                }
            ],
        },
        "body": None,
    }
    errors = tf.validate_template(bad)
    assert any("'fallback_line' must be a string" in e for e in errors)


def test_absent_literal_must_be_a_string():
    # Review: code-reviewer (Finding 3) — backfilled alongside fallback_line since
    # this is the direct precedent the finding named for the same untested gap.
    bad = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "x",
        "frontmatter": {
            "style": "fenced",
            "fields": [
                {
                    "kind": "present_as_null",
                    "key": "foo",
                    "field": "foo",
                    "quote": False,
                    "absent_literal": 123,
                }
            ],
        },
        "body": None,
    }
    errors = tf.validate_template(bad)
    assert any("'absent_literal' must be a string" in e for e in errors)


def test_absent_comment_must_be_a_string():
    # Review: code-reviewer (Finding 3) — backfilled alongside fallback_line since
    # this is the direct precedent the finding named for the same untested gap.
    bad = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "x",
        "frontmatter": {
            "style": "fenced",
            "fields": [
                {
                    "kind": "optional_omit",
                    "key": "foo",
                    "field": "foo",
                    "quote": False,
                    "absent_comment": 123,
                }
            ],
        },
        "body": None,
    }
    errors = tf.validate_template(bad)
    assert any("'absent_comment' must be a string" in e for e in errors)


def test_load_template_raises_on_invalid_file(tmp_path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps({"format_version": "wrong", "doc_type": "x"}), encoding="utf-8")
    with pytest.raises(tf.TemplateFormatError):
        tf.load_template(bad_path)


# Review: code-reviewer (Finding 2) — the docstring's numbered idiom list,
# FIELD_KINDS's size, and "N conditional idioms" prose were hand-maintained
# integers with nothing asserting they agree; this drifted stale 3 separate
# times in one session. Parse the prose rather than hardcoding a literal, so
# a 5th idiom that skips updating the docstring fails here instead of a 5th
# idiom that DOES update the docstring tripping a re-asserted magic number.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def test_docstring_conditional_idiom_count_matches_conditional_field_kinds():
    doc = tf.__doc__ or ""
    numbered_items = re.findall(r"^\s+(\d+)\.\s+\*\*", doc, flags=re.MULTILINE)
    assert numbered_items, "expected the docstring's numbered idiom list to be findable"
    docstring_count = len(numbered_items)
    assert docstring_count == len(tf.CONDITIONAL_FIELD_KINDS), (
        f"docstring enumerates {docstring_count} conditional idiom(s) but "
        f"CONDITIONAL_FIELD_KINDS has {len(tf.CONDITIONAL_FIELD_KINDS)}: "
        f"{sorted(tf.CONDITIONAL_FIELD_KINDS)} — update whichever fell behind"
    )


def test_docstring_field_kind_total_matches_field_kinds():
    doc = tf.__doc__ or ""
    match = re.search(r"beyond these (\w+) exists", doc)
    assert match, "expected the docstring's 'beyond these N exists' claim to be findable"
    word = match.group(1).lower()
    assert word in _NUMBER_WORDS, f"unrecognized number word {word!r} in docstring claim"
    docstring_total = _NUMBER_WORDS[word]
    assert docstring_total == len(tf.FIELD_KINDS), (
        f"docstring claims {docstring_total} total FieldSpec kind(s) but FIELD_KINDS "
        f"has {len(tf.FIELD_KINDS)}: {sorted(tf.FIELD_KINDS)} — update whichever fell behind"
    )
