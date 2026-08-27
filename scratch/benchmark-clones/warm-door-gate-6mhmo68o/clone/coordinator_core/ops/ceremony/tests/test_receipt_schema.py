"""
coordinator_core.ops.ceremony.tests.test_receipt_schema

Tests for the ceremony evidence receipt schema, validation, and factory helpers.

Coverage:
  (a) schema_version   — SCHEMA_VERSION is integer, not a string/semver
  (b) make_receipt     — all required fields present; empty arrays never key-absent
  (c) graceful_absent  — absent file is not an error (validate rejects None/missing, not path)
  (d) node_factories   — each node factory produces a schema-valid dict
  (e) validate_valid   — a fully-formed receipt validates clean (no errors)
  (f) validate_missing — missing required field returns an error
  (g) validate_types   — wrong type on schema_version (str not int) returns error
  (h) validate_op_tail — op_tail key-absent arrays caught; wrong type caught
  (i) validate_nodes   — invalid node type, missing node fields caught
  (j) compute_op_tail  — derived view aggregates tail D-nodes only, ignores non-tail
  (k) compute_empty    — no tail nodes → empty acted/skipped/failed, phase preserved
  (l) b_node_generic   — B-node uses pre_resolved_evidence/em_adjudication (not wsc-specific names)
  (m) pickup_phase     — op_tail phase="consume" validates without schema change (naming generality)
  (n) b_node_noreview  — non-review B bracket fits generic slots without rename (naming generality)
  (o) engine_sha       — optional/additive top-level field (C2); absence validates clean
  (p) scoping_method   — optional/additive top-level field (C2, wsc concurrent-tree race fix);
                          enum-checked when present, absence still validates
  (q) foreign_commit_count — optional/additive top-level field (C2, wsc concurrent-tree race
                          fix); int-checked when present, absence still validates

Spec backlink:
  coordinator_core/ops/ceremony/receipt_schema.py
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § A8
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony.receipt_schema import (
    SCHEMA_VERSION,
    VALID_NODE_TYPES,
    VALID_SCOPING_METHODS,
    compute_op_tail,
    make_b_node,
    make_d_node,
    make_empty_op_tail,
    make_f_node,
    make_j_node,
    make_receipt,
    make_x_node,
    validate,
)

# ---------------------------------------------------------------------------
# (a) schema_version is an integer
# ---------------------------------------------------------------------------


def test_schema_version_is_integer() -> None:
    """SCHEMA_VERSION must be an integer, not a semver string."""
    assert isinstance(SCHEMA_VERSION, int)
    assert not isinstance(SCHEMA_VERSION, bool)
    assert SCHEMA_VERSION >= 1


def test_schema_version_not_semver() -> None:
    """SCHEMA_VERSION must not be a dot-separated version string."""
    assert "." not in str(SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# (b) make_receipt — all required fields present; empty arrays never key-absent
# ---------------------------------------------------------------------------


def _minimal_receipt(**kwargs) -> dict:
    """Return a receipt dict with only required fields supplied."""
    defaults = dict(
        ceremony="wsc",
        phase="phase-1",
        emitted_at="2026-07-06T12:00:00Z",
        scope_mode="architecture",
    )
    defaults.update(kwargs)
    return make_receipt(**defaults)


def test_make_receipt_all_required_fields_present() -> None:
    """make_receipt returns a dict containing every required top-level field."""
    r = _minimal_receipt()
    required = [
        "schema_version", "ceremony", "phase", "emitted_at",
        "scope_mode", "nodes", "op_tail",
    ]
    for field in required:
        assert field in r, f"required field {field!r} missing from make_receipt() result"


def test_make_receipt_nodes_empty_list_present() -> None:
    """nodes defaults to [] (not absent) when not supplied."""
    r = _minimal_receipt()
    assert r["nodes"] == []


def test_make_receipt_op_tail_present_when_omitted() -> None:
    """op_tail is always present even when not supplied explicitly."""
    r = _minimal_receipt()
    assert "op_tail" in r
    assert isinstance(r["op_tail"], dict)


def test_make_receipt_op_tail_arrays_never_absent() -> None:
    """op_tail.acted / skipped / failed are never key-absent (graceful-absent rule)."""
    r = _minimal_receipt()
    tail = r["op_tail"]
    for key in ("acted", "skipped", "failed"):
        assert key in tail, f"op_tail.{key!r} must always be present; was absent"
        assert isinstance(tail[key], list)


def test_make_receipt_omits_coverage_pointer() -> None:
    """The K-001-killed coverage_pointer field is not emitted."""
    r = _minimal_receipt()
    assert "coverage_pointer" not in r


def test_validate_accepts_legacy_receipt_with_coverage_pointer() -> None:
    """Receipts written before the K-001 field removal still validate."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    r["coverage_pointer"] = "state/coverage/gate-result.json"
    assert validate(r) == []


def test_make_receipt_schema_version_is_integer() -> None:
    """make_receipt sets schema_version to the integer constant, not a string."""
    r = _minimal_receipt()
    assert r["schema_version"] == SCHEMA_VERSION
    assert isinstance(r["schema_version"], int)
    assert not isinstance(r["schema_version"], bool)


# ---------------------------------------------------------------------------
# (c) graceful_absent — absence of file = "not yet run"; validate rejects bad input
# ---------------------------------------------------------------------------


def test_validate_rejects_none() -> None:
    """validate(None) returns errors — not an exception."""
    errors = validate(None)  # type: ignore[arg-type]
    assert len(errors) > 0


def test_validate_rejects_non_dict() -> None:
    """validate(<non-dict>) returns errors."""
    errors = validate("not a dict")  # type: ignore[arg-type]
    assert len(errors) > 0


# ---------------------------------------------------------------------------
# (d) node factories produce schema-valid dicts
# ---------------------------------------------------------------------------


def test_make_d_node_shape() -> None:
    """make_d_node returns a dict with all required D-node fields."""
    node = make_d_node("step-0", resolving_op="my_op", evidence={"result": "ok"})
    assert node["type"] == "D"
    assert node["id"] == "step-0"
    assert node["resolving_op"] == "my_op"
    assert isinstance(node["evidence"], dict)
    assert node["tail_step"] is False  # default


def test_make_d_node_tail_step_flag() -> None:
    """make_d_node with tail_step=True marks the node as a tail op."""
    node = make_d_node("step-3.5a", tail_step=True)
    assert node["tail_step"] is True


def test_make_j_node_shape() -> None:
    """make_j_node returns a dict with all required J-node fields."""
    node = make_j_node("step-1a", question="Is this generalizable?")
    assert node["type"] == "J"
    assert node["id"] == "step-1a"
    assert node["question"] == "Is this generalizable?"
    assert node["answer"] == ""  # unanswered default


def test_make_f_node_shape() -> None:
    """make_f_node returns a dict with all required F-node fields."""
    node = make_f_node("step-1b", slot="lesson_body")
    assert node["type"] == "F"
    assert node["id"] == "step-1b"
    assert node["slot"] == "lesson_body"
    assert node["filled"] == ""  # unfilled default


def test_make_b_node_shape() -> None:
    """make_b_node returns a dict with the generic two-slot B-node shape."""
    node = make_b_node("B1")
    assert node["type"] == "B"
    assert node["id"] == "B1"
    assert isinstance(node["pre_resolved_evidence"], dict)
    assert isinstance(node["em_adjudication"], dict)


def test_make_x_node_shape() -> None:
    """make_x_node returns a dict with all required X-node fields."""
    node = make_x_node("step-2.6.3", missing_signal="SESSION_START_TIME")
    assert node["type"] == "X"
    assert node["id"] == "step-2.6.3"
    assert node["missing_signal"] == "SESSION_START_TIME"


def test_node_factory_defaults_no_missing_keys() -> None:
    """Each node factory with only required args produces no key-absent fields."""
    nodes = [
        make_d_node("d1"),
        make_j_node("j1"),
        make_f_node("f1"),
        make_b_node("b1"),
        make_x_node("x1"),
    ]
    # Each should validate clean within a receipt
    r = make_receipt(
        ceremony="wsc",
        phase="phase-1",
        emitted_at="2026-07-06T00:00:00Z",
        scope_mode="architecture",
        nodes=nodes,
        op_tail=make_empty_op_tail("archival"),
    )
    errors = validate(r)
    assert errors == [], f"Unexpected validation errors: {errors}"


# ---------------------------------------------------------------------------
# (e) validate_valid — a fully-formed receipt validates clean
# ---------------------------------------------------------------------------


def test_validate_full_receipt_valid() -> None:
    """A receipt with all fields correctly typed returns no errors."""
    r = make_receipt(
        ceremony="wsc",
        phase="phase-2",
        emitted_at="2026-07-06T12:00:00Z",
        scope_mode="architecture",
        nodes=[
            make_d_node("step-0", resolving_op="wsc_session_init",
                        evidence={"disposition": "single-session"}),
            make_j_node("step-1a", question="Is this generalizable?", answer="yes"),
            make_f_node("step-1b", slot="lesson_body", filled="Learned X."),
            make_b_node("B1",
                        pre_resolved_evidence={"slice_count": 1, "partition": "main"},
                        em_adjudication={"verdict": "WARN", "disposition": "integrate"}),
            make_x_node("step-2.6.3", missing_signal="SESSION_START_TIME"),
            make_d_node("step-3.5a", resolving_op="cs_archive",
                        evidence={"acted": ["sid-abc"], "skipped": [], "failed": []},
                        tail_step=True),
        ],
        op_tail=make_empty_op_tail("archival"),
    )
    errors = validate(r)
    assert errors == [], f"Expected no validation errors; got: {errors}"


# ---------------------------------------------------------------------------
# (f) validate_missing — missing required field → error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("drop_field", [
    "schema_version", "ceremony", "phase", "emitted_at",
    "scope_mode", "nodes", "op_tail",
])
def test_validate_missing_required_field(drop_field: str) -> None:
    """Dropping any required top-level field produces a validation error."""
    r = _minimal_receipt()
    del r[drop_field]
    errors = validate(r)
    assert any(drop_field in e for e in errors), (
        f"Expected error mentioning {drop_field!r}; got {errors}"
    )


# ---------------------------------------------------------------------------
# (g) validate_types — wrong type on schema_version
# ---------------------------------------------------------------------------


def test_validate_schema_version_string_rejected() -> None:
    """schema_version as a string (semver-style) must be rejected."""
    r = _minimal_receipt()
    r["schema_version"] = "1"  # string — must fail
    errors = validate(r)
    assert any("schema_version" in e for e in errors), (
        f"Expected schema_version type error; got {errors}"
    )


def test_validate_schema_version_semver_rejected() -> None:
    """schema_version as a semver string must be rejected."""
    r = _minimal_receipt()
    r["schema_version"] = "1.0.0"
    errors = validate(r)
    assert any("schema_version" in e for e in errors)


def test_validate_schema_version_bool_rejected() -> None:
    """schema_version as a boolean (True/False) must be rejected (bool is subclass of int)."""
    r = _minimal_receipt()
    r["schema_version"] = True
    errors = validate(r)
    assert any("schema_version" in e for e in errors)


# ---------------------------------------------------------------------------
# (h) validate_op_tail — missing arrays caught; wrong type caught
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("drop_key", ["phase", "acted", "skipped", "failed"])
def test_validate_op_tail_missing_key(drop_key: str) -> None:
    """Dropping any op_tail key produces a validation error."""
    r = _minimal_receipt()
    del r["op_tail"][drop_key]
    errors = validate(r)
    assert any(drop_key in e for e in errors), (
        f"Expected error mentioning op_tail.{drop_key!r}; got {errors}"
    )


def test_validate_op_tail_wrong_type_for_arrays() -> None:
    """op_tail.acted as a non-list must be rejected."""
    r = _minimal_receipt()
    r["op_tail"]["acted"] = "should-be-a-list"
    errors = validate(r)
    assert any("acted" in e for e in errors)


# ---------------------------------------------------------------------------
# (i) validate_nodes — invalid node type and missing node fields
# ---------------------------------------------------------------------------


def test_validate_nodes_invalid_type() -> None:
    """A node with an unrecognized type produces a validation error."""
    r = _minimal_receipt()
    r["nodes"] = [{"id": "n1", "type": "Z"}]  # Z is not valid
    errors = validate(r)
    assert any("Z" in e or "type" in e for e in errors)


def test_validate_nodes_missing_d_field() -> None:
    """A D-node missing 'resolving_op' produces a validation error."""
    r = _minimal_receipt()
    node = make_d_node("step-0")
    del node["resolving_op"]
    r["nodes"] = [node]
    errors = validate(r)
    assert any("resolving_op" in e for e in errors)


def test_validate_nodes_missing_b_slot() -> None:
    """A B-node missing 'em_adjudication' produces a validation error."""
    r = _minimal_receipt()
    node = make_b_node("B1")
    del node["em_adjudication"]
    r["nodes"] = [node]
    errors = validate(r)
    assert any("em_adjudication" in e for e in errors)


def test_validate_nodes_wrong_evidence_type() -> None:
    """A D-node with evidence as a non-dict must be rejected."""
    r = _minimal_receipt()
    node = make_d_node("step-0")
    node["evidence"] = "not-a-dict"
    r["nodes"] = [node]
    errors = validate(r)
    assert any("evidence" in e for e in errors)


# ---------------------------------------------------------------------------
# (j) compute_op_tail — aggregates tail D-nodes only
# ---------------------------------------------------------------------------


def test_compute_op_tail_aggregates_tail_nodes() -> None:
    """compute_op_tail collects acted/skipped/failed from tail_step=True D-nodes."""
    nodes = [
        make_d_node("step-0", evidence={"acted": ["plan-a"], "skipped": [], "failed": []}),
        # Non-tail D-node — should NOT appear in op_tail
        make_d_node("step-2.65c-memos", resolving_op="cs_sweep_actioned_memos",
                    evidence={"acted": ["memo-1"], "skipped": [], "failed": []},
                    tail_step=True),
        make_d_node("step-3.5a", resolving_op="cs_archive",
                    evidence={"acted": ["sid-xyz"], "skipped": [], "failed": ["sid-err"]},
                    tail_step=True),
        make_j_node("j1"),  # J-node — ignored by compute_op_tail
    ]
    tail = compute_op_tail(nodes, phase="archival")
    assert tail["phase"] == "archival"
    assert "memo-1" in tail["acted"]
    assert "sid-xyz" in tail["acted"]
    assert "sid-err" in tail["failed"]
    # Non-tail D-node acted entry must NOT appear
    assert "plan-a" not in tail["acted"]


def test_compute_op_tail_non_tail_nodes_excluded() -> None:
    """Non-tail D-nodes, J-nodes, F-nodes, B-nodes, X-nodes do not feed op_tail."""
    nodes = [
        make_d_node("d-nontail", evidence={"acted": ["secret"], "skipped": [], "failed": []}),
        make_j_node("j1"),
        make_f_node("f1"),
        make_b_node("b1"),
        make_x_node("x1"),
    ]
    tail = compute_op_tail(nodes, phase="archival")
    assert tail["acted"] == []
    assert tail["skipped"] == []
    assert tail["failed"] == []


# ---------------------------------------------------------------------------
# (k) compute_op_tail — no tail nodes → empty arrays, phase preserved
# ---------------------------------------------------------------------------


def test_compute_op_tail_empty_nodes() -> None:
    """With no nodes, compute_op_tail returns empty arrays and the given phase."""
    tail = compute_op_tail([], phase="archival")
    assert tail["phase"] == "archival"
    assert tail["acted"] == []
    assert tail["skipped"] == []
    assert tail["failed"] == []


def test_make_empty_op_tail_preserves_phase() -> None:
    """make_empty_op_tail returns correct phase and empty arrays."""
    tail = make_empty_op_tail("archival")
    assert tail["phase"] == "archival"
    assert tail["acted"] == []
    assert tail["skipped"] == []
    assert tail["failed"] == []


# ---------------------------------------------------------------------------
# (l) b_node_generic — B-node uses generic slot names, not wsc-specific names
# ---------------------------------------------------------------------------


def test_b_node_has_generic_slot_names() -> None:
    """B-node must have pre_resolved_evidence and em_adjudication, NOT wsc-specific names."""
    node = make_b_node("B1")
    # Generic names present
    assert "pre_resolved_evidence" in node
    assert "em_adjudication" in node
    # wsc-specific or review-specific names must NOT appear in the schema
    for forbidden in ("dispatch_plan", "adjudication", "review_verdict",
                      "review_wave", "slice_count"):
        assert forbidden not in node, (
            f"B-node must not have wsc-specific key {forbidden!r} in the schema"
        )


def test_b_node_accepts_arbitrary_blob_content() -> None:
    """pre_resolved_evidence and em_adjudication accept any dict content."""
    wsc_pre = {"slice_count": 2, "partition": "A,B", "docs_checker": True}
    wsc_adj = {"verdict": "WARN", "disposition": "integrate-with-comments"}
    node = make_b_node("B1", pre_resolved_evidence=wsc_pre, em_adjudication=wsc_adj)
    assert node["pre_resolved_evidence"]["slice_count"] == 2
    assert node["em_adjudication"]["verdict"] == "WARN"

    # A completely different ceremony's B-node also fits without rename
    pm_pre = {"request": "PM approval for feature X", "context": "budget Q3"}
    pm_adj = {"approved": True, "notes": "conditional on scope"}
    pm_node = make_b_node("B-pm-approval", pre_resolved_evidence=pm_pre, em_adjudication=pm_adj)
    assert pm_node["pre_resolved_evidence"]["request"] == "PM approval for feature X"
    assert pm_node["em_adjudication"]["approved"] is True


# ---------------------------------------------------------------------------
# (m) pickup_phase — op_tail phase="consume" validates without schema change (A8 gate)
# ---------------------------------------------------------------------------


def test_op_tail_pickup_phase_validates() -> None:
    """op_tail with phase='consume' (pickup's label) validates clean — no schema change needed.

    A8 naming-generality gate (a): /pickup's deterministic op-tail fits
    op_tail: {phase: 'consume', ...} WITHOUT renaming the field.
    """
    tail = make_empty_op_tail("consume")
    assert tail["phase"] == "consume"
    r = make_receipt(
        ceremony="pickup",
        phase="phase-2",
        emitted_at="2026-07-06T12:00:00Z",
        scope_mode="standard",
        nodes=[],
        op_tail=tail,
    )
    errors = validate(r)
    assert errors == [], (
        f"op_tail with phase='consume' should validate clean; got {errors}"
    )


def test_op_tail_any_phase_label_validates() -> None:
    """op_tail.phase accepts any non-empty string — ceremony-instance-specific label."""
    for label in ("archival", "consume", "deploy", "review", "custom-label"):
        tail = make_empty_op_tail(label)
        r = _minimal_receipt()
        r["op_tail"] = tail
        errors = validate(r)
        assert errors == [], (
            f"op_tail.phase={label!r} should validate; got {errors}"
        )


# ---------------------------------------------------------------------------
# (n) b_node_noreview — non-review B bracket fits generic slots without rename (A8 gate)
# ---------------------------------------------------------------------------


def test_b_node_pm_approval_bracket_fits_schema() -> None:
    """A PM-approval B bracket (non-review) fits pre_resolved_evidence/em_adjudication.

    A8 naming-generality gate (b): a non-review B bracket fits the generic two-slot
    shape WITHOUT renaming sub-keys.  The schema remains stable; only content differs.
    """
    # Simulate a PM-approval bracket: different ceremony, different blob content
    pm_b_node = make_b_node(
        "B-pm-gate",
        pre_resolved_evidence={
            "feature_name": "dark-mode toggle",
            "rationale": "requested by PM, no technical blocker",
        },
        em_adjudication={
            "approved": True,
            "pm_comment": "proceed",
        },
    )
    r = make_receipt(
        ceremony="feature-ship",
        phase="phase-1",
        emitted_at="2026-07-06T12:00:00Z",
        scope_mode="standard",
        nodes=[pm_b_node],
        op_tail=make_empty_op_tail("deliver"),
    )
    errors = validate(r)
    assert errors == [], (
        f"PM-approval B bracket should validate without schema change; got {errors}"
    )


# ---------------------------------------------------------------------------
# Additional: valid_node_types constant completeness
# ---------------------------------------------------------------------------


def test_valid_node_types_covers_all_defined_types() -> None:
    """VALID_NODE_TYPES contains all five node type discriminants."""
    assert VALID_NODE_TYPES == frozenset({"D", "J", "F", "B", "X"})


# ---------------------------------------------------------------------------
# C3 — structured failure-class discriminator (failed_critical[])
#
# Spec backlink: pln-wsc-commit-ceremony-tail-harde-6c393d § C3
# ---------------------------------------------------------------------------


def test_make_empty_op_tail_has_failed_critical() -> None:
    """C3: make_empty_op_tail includes failed_critical[] (empty list, never key-absent).

    failed_critical[] is the structured discriminator for critical-class failures.
    Environmental failures stay in failed[]; critical failures that hard-exit the
    ceremony go into failed_critical[].  Both arrays must always be present (graceful-absent).
    """
    tail = make_empty_op_tail("archival")
    assert "failed_critical" in tail, (
        "C3: failed_critical must always be present in op_tail (graceful-absent)"
    )
    assert tail["failed_critical"] == [], (
        f"C3: failed_critical must be empty in make_empty_op_tail; got {tail['failed_critical']!r}"
    )
    assert isinstance(tail["failed_critical"], list), (
        f"C3: failed_critical must be a list; got {type(tail['failed_critical']).__name__}"
    )


def test_compute_op_tail_aggregates_failed_critical() -> None:
    """C3: compute_op_tail aggregates evidence.failed_critical[] from tail D-nodes.

    When a tail op returns failed_critical[], it flows through _set_tail_node into
    evidence and is aggregated by compute_op_tail — not substring-matched from failed[].
    Environmental failures in failed[] must remain independent.
    """
    nodes = [
        # Tail op with a critical failure (e.g. C6 review-trail integrity breach)
        make_d_node(
            "step-2.9d",
            resolving_op="coordinator-write-review-trail.sh",
            evidence={
                "acted": [], "skipped": [],
                "failed": [],
                "failed_critical": ["review-trail:b_adjudication-present-but-review_trail-absent"],
            },
            tail_step=True,
        ),
        # Another tail op with only an environmental failure
        make_d_node(
            "step-2.75",
            resolving_op="render-handoff-tracker.js",
            evidence={
                "acted": [], "skipped": [],
                "failed": ["render-handoff-tracker.js: not found (environmental)"],
                # No failed_critical — this is environmental
            },
            tail_step=True,
        ),
    ]
    tail = compute_op_tail(nodes, phase="archival")
    # Critical failure must appear in failed_critical[] (not failed[]).
    assert "review-trail:b_adjudication-present-but-review_trail-absent" in tail["failed_critical"], (
        f"C3: critical failure must be in failed_critical; got {tail['failed_critical']!r}"
    )
    # Environmental failure must appear only in failed[] (not failed_critical[]).
    assert "render-handoff-tracker.js: not found (environmental)" in tail["failed"], (
        f"C3: environmental failure must be in failed; got {tail['failed']!r}"
    )
    assert "render-handoff-tracker.js: not found (environmental)" not in tail.get("failed_critical", []), (
        "C3: environmental failure must NOT leak into failed_critical"
    )


def test_compute_op_tail_failed_critical_empty_when_no_critical_failures() -> None:
    """C3: failed_critical[] is empty when no tail op emits critical failures.

    Pre-C3 ops and environmental ops don't populate failed_critical; the aggregation
    must return an empty list (not a key-absent field) when none are present.
    """
    nodes = [
        make_d_node(
            "step-3.5a",
            resolving_op="cs_archive",
            evidence={"acted": ["sid-abc"], "skipped": [], "failed": []},
            tail_step=True,
        ),
    ]
    tail = compute_op_tail(nodes, phase="archival")
    assert "failed_critical" in tail, (
        "C3: failed_critical must be present in compute_op_tail result (graceful-absent)"
    )
    assert tail["failed_critical"] == [], (
        f"C3: failed_critical must be empty when no critical failures; got {tail['failed_critical']!r}"
    )


def test_compute_op_tail_graceful_absent_failed_critical_in_evidence() -> None:
    """C3: graceful-absent — compute_op_tail handles evidence without failed_critical[].

    Pre-C3 D-node evidence dicts do not have a failed_critical key.  compute_op_tail
    must handle this via .get('failed_critical', []) without raising KeyError.
    """
    nodes = [
        make_d_node(
            "step-old",
            resolving_op="pre-c3-op",
            # No failed_critical key — pre-C3 evidence shape.
            evidence={"acted": ["old-op"], "skipped": [], "failed": []},
            tail_step=True,
        ),
    ]
    # Must not raise; must return empty failed_critical.
    tail = compute_op_tail(nodes, phase="archival")
    assert tail["failed_critical"] == [], (
        f"C3: pre-C3 evidence without failed_critical key must yield empty list; "
        f"got {tail['failed_critical']!r}"
    )
    assert "old-op" in tail["acted"], "acted from pre-C3 evidence must still aggregate correctly"


def test_validate_op_tail_failed_critical_wrong_type_rejected() -> None:
    """C3: validate rejects op_tail.failed_critical when not a list."""
    r = make_receipt(
        ceremony="wsc", phase="phase-1",
        emitted_at="2026-07-07T12:00:00Z", scope_mode="architecture",
        op_tail=make_empty_op_tail("archival"),
    )
    r["op_tail"]["failed_critical"] = "should-be-a-list"
    errors = validate(r)
    assert any("failed_critical" in e for e in errors), (
        f"C3: expected validation error for failed_critical non-list; got {errors}"
    )


def test_validate_op_tail_failed_critical_list_accepted() -> None:
    """C3: validate accepts op_tail.failed_critical when it is a list."""
    r = make_receipt(
        ceremony="wsc", phase="phase-1",
        emitted_at="2026-07-07T12:00:00Z", scope_mode="architecture",
        op_tail=make_empty_op_tail("archival"),
    )
    r["op_tail"]["failed_critical"] = ["some-critical-failure"]
    errors = validate(r)
    assert errors == [], (
        f"C3: failed_critical as list must validate clean; got {errors}"
    )


def test_validate_op_tail_failed_critical_absent_accepted() -> None:
    """C3: validate accepts receipts without failed_critical (backward-compatible).

    Old receipts on disk written before C3 do not have failed_critical; they must
    still validate clean (graceful-absent — optional field).
    """
    r = make_receipt(
        ceremony="wsc", phase="phase-1",
        emitted_at="2026-07-07T12:00:00Z", scope_mode="architecture",
        op_tail=make_empty_op_tail("archival"),
    )
    # Ensure failed_critical is absent from op_tail (simulating a pre-C3 receipt).
    r["op_tail"].pop("failed_critical", None)
    errors = validate(r)
    assert errors == [], (
        f"C3: receipt without failed_critical must validate clean (backward-compat); got {errors}"
    )


# ---------------------------------------------------------------------------
# (o) engine_sha — optional/additive top-level field (C2)
# ---------------------------------------------------------------------------


def test_validate_engine_sha_absent_validates_clean() -> None:
    """C2: backward-compat — a receipt WITHOUT engine_sha still validates clean."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    assert "engine_sha" not in r
    errors = validate(r)
    assert errors == [], f"C2: engine_sha absent must validate clean (backward-compat); got {errors}"


# ---------------------------------------------------------------------------
# engine_dirty — optional/additive top-level field (coordinator_core.engine_version)
# ---------------------------------------------------------------------------


def test_validate_engine_dirty_absent_when_none_validates_clean() -> None:
    """engine_dirty=None (default) omits the key — backward-compat, graceful-absent."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    assert "engine_dirty" not in r
    errors = validate(r)
    assert errors == [], (
        f"engine_dirty absent must validate clean (backward-compat); got {errors}"
    )


# ---------------------------------------------------------------------------
# (p) scoping_method — optional/additive top-level field (C2, wsc concurrent-tree
#     race fix)
# ---------------------------------------------------------------------------


def test_validate_scoping_method_present_validates_clean() -> None:
    """C2: a receipt carrying a valid scoping_method enum value validates clean."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    r["scoping_method"] = "session_id_trailer"
    errors = validate(r)
    assert errors == [], f"C2: valid scoping_method must validate clean; got {errors}"


@pytest.mark.parametrize("method", sorted(VALID_SCOPING_METHODS))
def test_validate_scoping_method_all_enum_values_accepted(method: str) -> None:
    """C2: every VALID_SCOPING_METHODS member validates clean when present."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    r["scoping_method"] = method
    errors = validate(r)
    assert errors == [], f"C2: scoping_method={method!r} must validate clean; got {errors}"


def test_validate_scoping_method_absent_validates_clean() -> None:
    """C2: backward-compat — a receipt WITHOUT scoping_method still validates clean."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    assert "scoping_method" not in r
    errors = validate(r)
    assert errors == [], (
        f"C2: scoping_method absent must validate clean (backward-compat); got {errors}"
    )


def test_validate_scoping_method_invalid_enum_rejected() -> None:
    """C2: scoping_method must be one of VALID_SCOPING_METHODS; a bogus value is rejected."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    r["scoping_method"] = "bogus-method"
    errors = validate(r)
    assert any("scoping_method" in e for e in errors), (
        f"C2: invalid scoping_method must be rejected by validate(); got {errors}"
    )


# ---------------------------------------------------------------------------
# (q) foreign_commit_count — optional/additive top-level field (C2, wsc
#     concurrent-tree race fix)
# ---------------------------------------------------------------------------


def test_validate_foreign_commit_count_present_validates_clean() -> None:
    """C2: a receipt carrying foreign_commit_count (int) validates clean."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    r["foreign_commit_count"] = 3
    errors = validate(r)
    assert errors == [], f"C2: valid foreign_commit_count must validate clean; got {errors}"


def test_validate_foreign_commit_count_zero_validates_clean() -> None:
    """C2: foreign_commit_count=0 (falsy but valid int) validates clean."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    r["foreign_commit_count"] = 0
    errors = validate(r)
    assert errors == [], f"C2: foreign_commit_count=0 must validate clean; got {errors}"


def test_validate_foreign_commit_count_absent_validates_clean() -> None:
    """C2: backward-compat — a receipt WITHOUT foreign_commit_count still validates clean."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    assert "foreign_commit_count" not in r
    errors = validate(r)
    assert errors == [], (
        f"C2: foreign_commit_count absent must validate clean (backward-compat); got {errors}"
    )


def test_validate_foreign_commit_count_wrong_type_rejected() -> None:
    """C2: foreign_commit_count must be an int when present; a str is rejected."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    r["foreign_commit_count"] = "3"
    errors = validate(r)
    assert any("foreign_commit_count" in e for e in errors), (
        f"C2: wrong-type foreign_commit_count must be rejected by validate(); got {errors}"
    )


def test_validate_foreign_commit_count_bool_rejected() -> None:
    """C2: bool is a subtype of int in Python; must be explicitly rejected."""
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    r["foreign_commit_count"] = True
    errors = validate(r)
    assert any("foreign_commit_count" in e for e in errors), (
        f"C2: bool foreign_commit_count must be rejected by validate(); got {errors}"
    )


def test_validate_both_new_fields_absent_together_validates_clean() -> None:
    """C2: critical backward-compat case — receipt with NEITHER new field validates clean.

    This is the primary regression guard: pre-existing receipts on disk (example-retrieval-repo
    reader wire-compat) never carry scoping_method or foreign_commit_count and must
    continue to validate without modification.
    """
    r = _minimal_receipt(op_tail=make_empty_op_tail("archival"))
    assert "scoping_method" not in r
    assert "foreign_commit_count" not in r
    errors = validate(r)
    assert errors == [], (
        f"C2: receipt without scoping_method/foreign_commit_count must validate "
        f"clean (backward-compat); got {errors}"
    )
