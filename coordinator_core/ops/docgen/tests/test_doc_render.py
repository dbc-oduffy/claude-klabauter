"""Tests for coordinator_core.ops.docgen.render (C4).

Covers:
  - every one of the 22 staged template types renders without error given a
    fully-populated resolved-value mapping, with structural fence/no-fence
    shape matching each template's frontmatter style (fenced / whole_document
    / frontmatter: null)
  - each of the 4 conditional idioms exercised on BOTH branches (present and
    absent), including the ``absent_literal``, ``absent_comment``, and
    ``fallback_line`` refinements
  - ``literal``/``raw`` ``{field}`` placeholder substitution, including the
    escaped-double-brace case (no substitution, literal brace survives)
  - quoting matches the oracle's ``_yaml_quote`` escaping rules byte-for-byte
  - defect paths: unknown doc_type, missing required ``value`` field, missing
    placeholder value — all raise ``RenderError``, never a silent KeyError/None

This module does NOT assert byte-identity against the live DoE oracle (C6's
surface) and does NOT assert the no-write/no-subprocess structural property
formally (C7's surface, a standing test) — it stays inside C4's own contract:
the render function is correct against the template format for every idiom.

Spec backlink: pln-strang-12-document-generation--75a7eb § C4 (AC6)
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.docgen import template_format as tf
from coordinator_core.ops.docgen.render import RenderError, render_document, render_template

# A fully-populated resolved-value mapping covering every "field" referenced
# by any of the 22 templates' frontmatter specs, plus every {placeholder} used
# in literal/raw lines. Individual tests override/delete keys to exercise the
# absent branch of each conditional idiom.
FULL_VALUES: dict = {
    "title": "Example Title",
    "created": "2026-07-21",
    "id": "DR-224",
    "branch": "work/example/2026-07-21",
    "author": "example-em",
    "plan_id": "pln-example-abcdef",
    "deliverable_id": "dlv-example-abcdef",
    "initiative": "example-initiative",
    "placeholder_summary": "PLACEHOLDER — example summary",
    "handoff_id": "hnd-example-abcdef",
    "recovers_session": "sid-example",
    "gate_dependency": "example-subsystem gate",
    "goals": ["goal-one", "goal-two"],
    "roadmap_id": "rmp-example-abcdef",
    "stub_id": "stb-example-abcdef",
    "authoring_session_path": "state/handoffs/example.md",
    "from_id": "claude-klabauter-em",
    "to": "claude-central-em",
    "explicit_summary": "Example memo summary",
    "topic": "example-topic",
    "nature": "infra",
    "chain": "example-chain",
    "completion_id": "cmp-example-abcdef",
    "goal_id": "goal-example-abcdef",
    "goal_id_quoted": '"goal-example-abcdef"',
    "repo_name": "project-example",
    "plan_path": "docs/plans/example.md",
    "run_id_placeholder": "2026-07-21-12h00",
    "system": "example-system",
    "chunk_id": "C4",
    "dispatched_at": "2026-07-21T12:00:00Z",
    "dispatched_by": "example-em",
    "agent_type": "general-purpose",
    "slice_id": "slice-1",
    "scope": "coordinator_core/ops/docgen/",
    "spawned_at": "2026-07-21T12:00:00Z",
    "lead_session_id": "sid-example",
}


def _all_types() -> list[str]:
    return tf.available_template_types()


def test_available_types_is_full_22_surface():
    assert len(_all_types()) == 22


@pytest.mark.parametrize("doc_type", _all_types())
def test_every_type_renders_without_error(doc_type):
    output = render_document(doc_type, FULL_VALUES)
    assert isinstance(output, str)
    assert output != ""


@pytest.mark.parametrize("doc_type", _all_types())
def test_fence_shape_matches_frontmatter_style(doc_type):
    template = tf.load_template(tf.templates_dir() / f"{doc_type.replace('-', '_')}.json")
    output = render_document(doc_type, FULL_VALUES)
    fence_count = sum(1 for line in output.split("\n") if line == "---")
    frontmatter = template.get("frontmatter")
    if frontmatter is None:
        assert fence_count == 0, doc_type
    elif frontmatter["style"] == "fenced":
        assert fence_count == 2, doc_type
    else:  # whole_document
        assert fence_count == 0, doc_type


def test_present_as_null_absent_branch_defaults_to_null():
    values = dict(FULL_VALUES)
    del values["deliverable_id"]
    output = render_document("handoff", values)
    assert "deliverable_id: null" in output.split("\n")


def test_present_as_null_present_branch_quotes_value():
    output = render_document("handoff", FULL_VALUES)
    assert 'deliverable_id: "dlv-example-abcdef"' in output.split("\n")


def test_present_as_null_custom_absent_literal():
    # No extracted type declares a custom absent_literal any more — goal-seed's
    # gate_dependency (the one live example) migrated to value_or_literal_fallback
    # at archive/specs/2026-08/2026-08-03-gate-dependency-template-emission-spec.md
    # § C1. Exercise the custom-absent_literal refinement via a synthetic
    # in-memory template instead, same pattern as
    # test_frontmatter_none_type_has_no_fences_or_field_lines above.
    template = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "custom-absent-literal-test",
        "frontmatter": {
            "style": "fenced",
            "fields": [
                {
                    "kind": "present_as_null",
                    "key": "gate_dependency",
                    "field": "gate_dependency",
                    "quote": True,
                    "absent_literal": "PLACEHOLDER",
                },
            ],
        },
        "body": None,
    }
    values = dict(FULL_VALUES)
    del values["gate_dependency"]
    output = render_template(template, values)
    assert "gate_dependency: PLACEHOLDER" in output.split("\n")


def test_value_or_literal_fallback_present_branch_writes_deprecated_field():
    output = render_document("goal-seed", FULL_VALUES)
    lines = output.split("\n")
    assert (
        'gate_dependency: "example-subsystem gate"  # deprecated; superseded by blocked_by/blocking_notes'
        in lines
    )
    assert not any(line.startswith("blocking_notes:") for line in lines)


def test_value_or_literal_fallback_absent_branch_writes_fallback_line():
    values = dict(FULL_VALUES)
    del values["gate_dependency"]
    output = render_document("goal-seed", values)
    lines = output.split("\n")
    assert (
        "blocking_notes: PLACEHOLDER — name the condition gating this baton, "
        "or delete this line once blocked_by names it" in lines
    )
    assert not any(line.startswith("gate_dependency:") for line in lines)


def test_optional_omit_present_branch_emits_line():
    output = render_document("handoff", FULL_VALUES)
    assert 'handoff_id: "hnd-example-abcdef"' in output.split("\n")


def test_optional_omit_absent_branch_omits_key_entirely_when_no_comment():
    values = dict(FULL_VALUES)
    del values["handoff_id"]
    output = render_document("handoff", values)
    assert not any(line.startswith("handoff_id") for line in output.split("\n"))


def test_optional_omit_absent_branch_emits_absent_comment_when_declared():
    values = dict(FULL_VALUES)
    del values["chain"]
    output = render_document("completion", values)
    lines = output.split("\n")
    assert "# chain: null  # fill with chain slug; omit this line entirely for standalone (non-chain) entries" in lines
    assert not any(line.startswith("chain: example-chain") for line in lines)


def test_list_emit_if_present_present_branch():
    output = render_document("goal-seed", FULL_VALUES)
    lines = output.split("\n")
    idx = lines.index("origin_goal_id:")
    assert lines[idx + 1] == '  - "goal-one"'
    assert lines[idx + 2] == '  - "goal-two"'


def test_list_emit_if_present_absent_branch_empty_list():
    values = dict(FULL_VALUES)
    values["goals"] = []
    output = render_document("goal-seed", values)
    assert "origin_goal_id:" not in output.split("\n")


def test_list_emit_if_present_absent_branch_missing_key():
    values = dict(FULL_VALUES)
    del values["goals"]
    output = render_document("goal-seed", values)
    assert "origin_goal_id:" not in output.split("\n")


def test_literal_placeholder_substitution_in_frontmatter():
    output = render_document("goal", FULL_VALUES)
    assert '# goal_id: "goal-example-abcdef"' in output.split("\n")[-1:] or any(
        line.startswith("# goal_id: ") and "goal-example-abcdef" in line
        for line in output.split("\n")
    )


def test_raw_body_placeholder_substitution():
    output = render_document("plan", FULL_VALUES)
    assert "# Example Title" in output.split("\n")


def test_escaped_double_brace_survives_as_literal_brace():
    output = render_document("strategic-self-description", FULL_VALUES)
    assert "version_highlights: []  # optional; add {label, date, bullets, provenance} entries as dated milestones land" in output.split("\n")


def test_yaml_quote_escaping_matches_oracle():
    values = dict(FULL_VALUES)
    values["title"] = 'has "quotes" and a \\backslash\\ and\ta tab'
    output = render_document("handoff", values)
    first_line = next(line for line in output.split("\n") if line.startswith("title:"))
    assert first_line == 'title: "has \\"quotes\\" and a \\\\backslash\\\\ and\\ta tab"'


def test_frontmatter_none_type_has_no_fences_or_field_lines():
    # No extracted type currently declares "frontmatter": null (review-findings
    # was the last one, until the 2026-07-24 schema unification made it
    # frontmatter-bearing — see volatility.py's module docstring) — exercise
    # the idiom via a synthetic in-memory template instead, same pattern as
    # test_render_template_accepts_in_memory_template_dict below.
    template = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "body-only-test",
        "frontmatter": None,
        "body": [{"kind": "raw", "lines": ["# Code Review: Slice {slice_id} -- {scope}"]}],
    }
    output = render_template(template, FULL_VALUES)
    assert "---" not in output
    assert output.startswith("# Code Review: Slice slice-1 -- coordinator_core/ops/docgen/")


def test_whole_document_style_has_no_fences():
    output = render_document("goal", FULL_VALUES)
    assert "---" not in output


def test_unknown_doc_type_raises_render_error():
    with pytest.raises(RenderError):
        render_document("not-a-real-doc-type", FULL_VALUES)


def test_missing_required_value_field_raises_render_error():
    values = dict(FULL_VALUES)
    del values["title"]
    with pytest.raises(RenderError):
        render_document("handoff", values)


def test_missing_literal_placeholder_raises_render_error():
    values = dict(FULL_VALUES)
    del values["goal_id_quoted"]
    with pytest.raises(RenderError):
        render_document("goal", values)


def test_render_template_accepts_in_memory_template_dict():
    template = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "inline-test",
        "frontmatter": {
            "style": "fenced",
            "fields": [
                {"kind": "value", "key": "title", "field": "title", "quote": True},
            ],
        },
        "body": [{"kind": "raw", "lines": ["", "hello {title}"]}],
    }
    output = render_template(template, {"title": "Inline"})
    assert output == '---\ntitle: "Inline"\n---\n\nhello Inline'


def test_render_document_does_not_mutate_input_mapping():
    values = dict(FULL_VALUES)
    snapshot = dict(values)
    render_document("plan", values)
    assert values == snapshot


def test_duplicate_doc_type_raises_render_error(tmp_path):
    # Review: code-reviewer — _template_index used to silently last-wins on a
    # duplicate doc_type across two template files; now fails loud.
    import json

    template = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "dup-test",
        "frontmatter": None,
        "body": [{"kind": "raw", "lines": ["hello"]}],
    }
    (tmp_path / "a_dup_test.json").write_text(json.dumps(template), encoding="utf-8")
    (tmp_path / "b_dup_test.json").write_text(json.dumps(template), encoding="utf-8")
    with pytest.raises(RenderError, match="duplicate doc_type"):
        render_document("dup-test", {}, templates_directory=tmp_path)


# ---------------------------------------------------------------------------
# pickup_ready vs awaiting_gate coherence (cross-repo memo
# 2026-08-06-example-market-data-repo-em-pickup-ready-true-under-unmet-gate.md).
# goal-seed/roadmap-seed are born awaiting_gate with no ready_to_fire arm, so
# they must never scaffold `pickup_ready: true`; the three coherent arms
# (session-handoff, recovery, spinoff) pair it with ready_to_fire and keep it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc_type", ["goal-seed", "roadmap-seed"])
def test_awaiting_gate_seed_types_omit_pickup_ready_true(doc_type):
    output = render_document(doc_type, FULL_VALUES)
    assert "pickup_ready: true" not in output


@pytest.mark.parametrize("doc_type", ["handoff", "recovery", "spinoff"])
def test_ready_to_fire_arms_still_scaffold_pickup_ready_true(doc_type):
    output = render_document(doc_type, FULL_VALUES)
    assert "pickup_ready: true" in output
