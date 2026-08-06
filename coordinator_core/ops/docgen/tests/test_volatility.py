"""Tests for coordinator_core.ops.docgen.volatility (C5, AC5).

Covers: the closed volatile-field-set registry is internally consistent AND
consistent with the extracted templates on disk (in both directions), the
injectable/redact-only split is correct per type, the two fully-deterministic
types carry zero volatile fields, and the value/text redaction helpers behave
identically regardless of quoting.

Spec backlink: docs/plans/2026-07-21-strang-12-doc-generation-strangle.md § C5 (AC5)
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.docgen import template_format, volatility as vol

ALL_TYPES = template_format.available_template_types()


def test_all_22_types_extracted():
    assert len(ALL_TYPES) == 22


def test_closed_set_assertion_passes_against_disk():
    # The load-bearing check: registry <-> template field keys agree exactly,
    # in both directions, for every extracted type.
    vol.assert_closed_set()


@pytest.mark.parametrize("doc_type", sorted(ALL_TYPES))
def test_every_policy_field_is_in_volatile_fields(doc_type):
    for policy in vol.volatile_fields_for(doc_type):
        assert policy.field in vol.VOLATILE_FIELDS


@pytest.mark.parametrize("doc_type", sorted(ALL_TYPES))
def test_policy_fields_subset_of_template_fields(doc_type):
    path = template_format.templates_dir() / f"{doc_type.replace('-', '_')}.json"
    data = template_format.load_template(path)
    frontmatter = data.get("frontmatter")
    keys = {f["key"] for f in frontmatter["fields"] if f.get("key")} if frontmatter else set()
    policy_fields = {p.field for p in vol.volatile_fields_for(doc_type)}
    assert policy_fields <= keys


def test_deterministic_types_carry_no_volatile_fields():
    for doc_type in vol.DETERMINISTIC_TYPES:
        assert vol.volatile_fields_for(doc_type) == ()
        assert vol.is_fully_deterministic(doc_type)


def test_deterministic_types_are_exactly_one_known_type():
    # review-findings dropped out of this set 2026-07-24 (schema unification
    # made it frontmatter-bearing with a wall-clock spawned_at) — see
    # volatility.py's module docstring.
    assert vol.DETERMINISTIC_TYPES == {"strategic-self-description"}
    assert vol.DETERMINISTIC_TYPES <= set(ALL_TYPES)


def test_review_findings_has_spawned_at_redact_only():
    redact_only = set(vol.redact_only_fields_for("review-findings"))
    assert redact_only == {"spawned_at"}
    assert vol.injectable_fields_for("review-findings") == {}


@pytest.mark.parametrize(
    "doc_type",
    ["handoff", "recovery", "spinoff", "roadmap-baton", "roadmap-seed", "plan"],
)
def test_deliverable_id_injectable_for_spine_types(doc_type):
    injectable = vol.injectable_fields_for(doc_type)
    assert injectable["deliverable_id"] == "--deliverable-id"


def test_goal_seed_has_no_deliverable_id():
    # Real asymmetry: goal-seed is NOT in the oracle's _spine_types set.
    assert "deliverable_id" not in vol.injectable_fields_for("goal-seed")
    assert "deliverable_id" not in vol.redact_only_fields_for("goal-seed")


def test_roadmap_baton_has_handoff_id():
    # AC13 fix (docs/plans/2026-08-01-baton-spine-information-integrity.md § A5):
    # roadmap-baton USED TO be excluded from the oracle's handoff-id-minting
    # doc_type tuple (a break-class defect — minted roadmap batons carried
    # stub_id/deliverable_id but no handoff_id at all). It is now included
    # like every other handoff-family doc_type.
    all_fields = {p.field for p in vol.volatile_fields_for("roadmap-baton")}
    assert "handoff_id" in all_fields
    assert "handoff_id" in vol.redact_only_fields_for("roadmap-baton")
    assert "handoff_id" not in vol.injectable_fields_for("roadmap-baton")


@pytest.mark.parametrize(
    "doc_type",
    [
        "handoff",
        "recovery",
        "spinoff",
        "roadmap-baton",
        "goal-seed",
        "roadmap-seed",
        "plan",
    ],
)
def test_branch_injectable_for_branch_carrying_types(doc_type):
    injectable = vol.injectable_fields_for(doc_type)
    assert injectable["branch"] == "--branch"


def test_run_report_has_dispatched_at_and_spawned_at_redact_only():
    redact_only = set(vol.redact_only_fields_for("run-report"))
    assert redact_only == {"dispatched_at", "spawned_at"}
    assert vol.injectable_fields_for("run-report") == {}


def test_problem_set_uses_date_not_created():
    redact_only = set(vol.redact_only_fields_for("problem-set"))
    assert redact_only == {"date"}


def test_minted_id_fields_have_no_carry_path():
    # handoff_id / completion_id / plan_id: no CLI flag exists for any of them.
    for doc_type, field in [
        ("handoff", "handoff_id"),
        ("spinoff", "handoff_id"),
        ("recovery", "handoff_id"),
        ("goal-seed", "handoff_id"),
        ("roadmap-seed", "handoff_id"),
        ("roadmap-baton", "handoff_id"),
        ("completion", "completion_id"),
        ("plan", "plan_id"),
    ]:
        assert field in vol.redact_only_fields_for(doc_type)
        assert field not in vol.injectable_fields_for(doc_type)


def test_redact_values_only_touches_redact_only_fields():
    values = {
        "title": "My Plan",
        "created": "2026-07-21",
        "branch": "work/foo/2026-07-21",
        "deliverable_id": "dlv-my-plan-abc123",
        "plan_id": "pln-my-plan-def456",
    }
    redacted = vol.redact_values(values, "plan")
    assert redacted["title"] == "My Plan"
    assert redacted["branch"] == "work/foo/2026-07-21"  # injectable, untouched
    assert redacted["deliverable_id"] == "dlv-my-plan-abc123"  # injectable, untouched
    assert redacted["created"] == vol.REDACTED_TOKEN
    assert redacted["plan_id"] == vol.REDACTED_TOKEN


def test_redact_values_is_a_copy_not_a_mutation():
    values = {"created": "2026-07-21"}
    redacted = vol.redact_values(values, "decision")
    assert values["created"] == "2026-07-21"
    assert redacted["created"] == vol.REDACTED_TOKEN
    assert redacted is not values


def test_audit_record_has_run_id_redact_only():
    # Review: code-reviewer — run_id (audit-record's run_id_placeholder field)
    # was a real registry gap, previously patched only in the C6 test's own
    # private _LOCAL_EXTRA_REDACT, not in this frozen registry.
    redact_only = set(vol.redact_only_fields_for("audit-record"))
    assert redact_only == {"created", "run_id"}
    assert "run_id" in vol.VOLATILE_FIELDS


def test_redact_values_uses_mapping_field_not_emitted_key():
    # key != field divergence: "problem-set" emits a "date:" line but the
    # resolved-value mapping render_document consumes is keyed "created".
    values = {"title": "P1", "created": "2026-07-21"}
    redacted = vol.redact_values(values, "problem-set")
    assert redacted["created"] == vol.REDACTED_TOKEN
    assert redacted["title"] == "P1"


def test_redact_values_uses_mapping_field_not_emitted_key_audit_record():
    # Same divergence, other direction: "audit-record" emits a "run_id:" line
    # but the resolved-value mapping is keyed "run_id_placeholder".
    values = {"created": "2026-07-21", "run_id_placeholder": "2026-07-21-14h30"}
    redacted = vol.redact_values(values, "audit-record")
    assert redacted["created"] == vol.REDACTED_TOKEN
    assert redacted["run_id_placeholder"] == vol.REDACTED_TOKEN


def test_redact_text_normalizes_across_quoting_styles():
    left = 'title: "My Plan"\ncreated: 2026-07-21\nbranch: "work/foo"\n'
    right = 'title: "My Plan"\ncreated: "2026-07-21"\nbranch: "work/foo"\n'
    left_redacted = vol.redact_text(left, "plan")
    right_redacted = vol.redact_text(right, "plan")
    assert left_redacted == right_redacted
    assert "2026-07-21" not in left_redacted


def test_redact_text_leaves_injectable_and_untracked_lines_alone():
    text = 'title: "My Plan"\ncreated: 2026-07-21\nbranch: "work/foo"\n'
    redacted = vol.redact_text(text, "plan")
    assert 'title: "My Plan"' in redacted
    assert 'branch: "work/foo"' in redacted
    assert "created: <REDACTED>" in redacted


def test_redact_text_handles_indented_field_lines():
    text = "some:\n  created: 2026-07-21\n"
    redacted = vol.redact_text(text, "decision")
    assert redacted == "some:\n  created: <REDACTED>\n"


def test_redact_text_is_idempotent():
    text = "created: 2026-07-21\n"
    once = vol.redact_text(text, "decision")
    twice = vol.redact_text(once, "decision")
    assert once == twice


# ---------------------------------------------------------------------------
# Regression: a present-but-empty redacted field must not consume its NEIGHBOUR
# (break-class, 2026-07-28). The captured prefix padded with `\s*`, and `\s`
# matches a newline, so on a bare `created:` the pad crossed the line break and
# the trailing `.*$` matched the FOLLOWING line — `sub` then replaced that line
# with the redaction token, deleting an unrelated field from one side of a
# byte-identity comparison. Parametrized over BOTH line endings: the LF case
# alone would leave the Windows-authored half unproven.
# ---------------------------------------------------------------------------

_EOLS = pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])


@_EOLS
def test_redact_text_empty_field_does_not_consume_next_line(eol):
    text = eol.join(["created:", "status: open", "title: T", ""])

    redacted = vol.redact_text(text, "decision")

    assert f"status: open{eol}" in redacted
    assert redacted.count(vol.REDACTED_TOKEN) == 1
    assert redacted == eol.join([f"created:{vol.REDACTED_TOKEN}", "status: open", "title: T", ""])


@_EOLS
def test_redact_text_preserves_line_endings(eol):
    """The redacted line keeps its own terminator, so redacting a CRLF document
    cannot silently downgrade one line to LF and leave the text mixed-ending —
    which would itself defeat the byte-identity comparison redaction exists to
    make possible."""
    text = eol.join(["created: 2026-07-21", "status: open", ""])

    redacted = vol.redact_text(text, "decision")

    assert redacted == eol.join([f"created: {vol.REDACTED_TOKEN}", "status: open", ""])


@_EOLS
def test_redact_text_indented_empty_field_does_not_consume_next_line(eol):
    """The LEADING `\\s*` is deliberate indent tolerance and is unchanged; it is
    safe because it is captured into group 1 and re-emitted verbatim."""
    text = eol.join(["some:", "  created:", "  status: open", ""])

    redacted = vol.redact_text(text, "decision")

    assert redacted == eol.join(["some:", f"  created:{vol.REDACTED_TOKEN}", "  status: open", ""])


def test_assert_closed_set_detects_untracked_volatile_field(tmp_path):
    # Fabricate a template that declares a volatile-named field with no
    # policy entry (a "missing case in the plan text" analogue), and confirm
    # assert_closed_set catches it rather than silently passing.
    import json

    bogus = {
        "format_version": template_format.FORMAT_VERSION,
        "doc_type": "handoff",  # real type — policy already lacks "run_id" for it
        "frontmatter": {
            "style": "fenced",
            "fields": [
                {"kind": "value", "key": "run_id", "field": "run_id", "quote": True},
                {"kind": "value", "key": "created", "field": "created", "quote": False},
            ],
        },
        "body": None,
    }
    # run_id is not in VOLATILE_FIELDS, so this fabricated template alone should
    # NOT trip the untracked-volatile-field branch; instead assert the
    # missing-from-template branch fires because "handoff"'s real policy names
    # branch/deliverable_id/handoff_id, none of which this fabricated template
    # declares.
    (tmp_path / "handoff.json").write_text(json.dumps(bogus), encoding="utf-8")
    with pytest.raises(vol.VolatilitySetError):
        vol.assert_closed_set(templates_directory=tmp_path)


def test_assert_closed_set_detects_untracked_volatile_field_by_name(tmp_path):
    import json

    bogus = {
        "format_version": template_format.FORMAT_VERSION,
        "doc_type": "decision",  # policy for decision only names "created"
        "frontmatter": {
            "style": "fenced",
            "fields": [
                {"kind": "value", "key": "created", "field": "created", "quote": False},
                # "branch" is volatile-named but decision's policy doesn't track it.
                {"kind": "value", "key": "branch", "field": "branch", "quote": True},
            ],
        },
        "body": None,
    }
    (tmp_path / "decision.json").write_text(json.dumps(bogus), encoding="utf-8")
    with pytest.raises(vol.VolatilitySetError) as exc_info:
        vol.assert_closed_set(templates_directory=tmp_path)
    assert "volatile-named" in str(exc_info.value)
