"""Tests for coordinator_core.ops.dispatch_emit.spine_read (C1)."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    KNOWN_DISPOSITIONS,
    NON_DISPATCHABLE_DISPOSITIONS,
    UNDECLARED,
    DanglingDependencyError,
    InvalidFieldTypeError,
    InvalidRowIdError,
    MalformedDependencyEdgeError,
    SpineReadError,
    UnknownDispositionError,
    read_spine,
)

_HEADER = "# fixture plan\n\n## Tasks\n\n"


def _write_plan(tmp_path, body: str):
    path = tmp_path / "plan.md"
    path.write_text(_HEADER + "```yaml plan-tasks\n" + body + "\n```\n", encoding="utf-8")
    return path


def test_writes_declared_vs_undeclared(tmp_path):
    body = """\
- id: C1
  title: has writes
  surface: some/surface
  writes:
    - some/file.py
- id: C2
  title: no writes key at all
  surface: some/surface
- id: C3
  title: declared empty writes
  surface: some/surface
  writes: []
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}

    assert rows["C1"].writes == ["some/file.py"]
    assert rows["C2"].writes is UNDECLARED
    assert rows["C3"].writes == []
    # UNDECLARED is never None and never confusable with [] by identity.
    assert rows["C2"].writes is not None
    assert rows["C2"].writes != []


def test_writes_present_but_empty_value_collapses_to_undeclared(tmp_path):
    body = """\
- id: C1
  title: writes key present with no value
  surface: some/surface
  writes:
- id: C2
  title: writes key present with explicit null
  surface: some/surface
  writes: null
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}

    # A present-but-empty writes: value is a third state AC2 forbids — it
    # must collapse to the same UNDECLARED sentinel as an absent key, never
    # leak through as None.
    assert rows["C1"].writes is UNDECLARED
    assert rows["C2"].writes is UNDECLARED


def test_scalar_writes_raises_invalid_field_type_error(tmp_path):
    body = """\
- id: C1
  title: writes declared as a bare scalar, not a list
  surface: some/surface
  writes: some/file.py
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(InvalidFieldTypeError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "C1" in message
    assert "some/file.py" in message


def test_scalar_reads_raises_invalid_field_type_error(tmp_path):
    body = """\
- id: C1
  title: reads declared as a bare scalar, not a list
  surface: some/surface
  reads: some/file.py
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(InvalidFieldTypeError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "C1" in message
    assert "some/file.py" in message


def test_dangling_depends_on_raises(tmp_path):
    body = """\
- id: C1
  title: depends on a chunk that does not exist
  surface: some/surface
  depends_on:
    - chunk: C99
      gate_kind: epistemic-premise
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(DanglingDependencyError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "C1" in message
    assert "C99" in message


def test_dangling_depends_on_error_is_a_spine_read_error(tmp_path):
    # Review-B MAJOR: composition_graph.py's chunk_overlap/path_rename_or_move
    # catch `except SpineReadError` around read_spine and degrade to
    # undetermined(...) on any unreadable spine. DanglingDependencyError must
    # be a SpineReadError subclass — not merely a ValueError — or a dangling
    # depends_on edge raises uncaught through that predicate instead of
    # degrading like every other spine-read failure. Assert the subclass
    # relationship explicitly so a future edit re-narrowing the base class
    # is caught here, not downstream.
    body = """\
- id: C1
  title: depends on a chunk that does not exist
  surface: some/surface
  depends_on:
    - chunk: C99
      gate_kind: epistemic-premise
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(SpineReadError):
        read_spine(plan_path)


def test_truthy_non_list_depends_on_raises_a_spine_read_error(tmp_path):
    # Review-B MAJOR (the actual regression): commit ab7df1af0's schema-shape
    # preflight fires on a TRUTHY non-list depends_on (e.g. a dict where a
    # list belongs) before spine_read's own `elif not isinstance(depends_on,
    # list)` check ever runs, raising MalformedDependencyEdgeError instead of
    # InvalidFieldTypeError -- but only once the row otherwise satisfies the
    # base schema's required fields (here: `change_kind`, matching
    # `test_schema_shape_preflight_raises_with_validator_message` above);
    # without it, the preflight's first hit is the unrelated missing-field
    # error and this module's own InvalidFieldTypeError check fires instead
    # (also a SpineReadError, but not the regression this test targets).
    # Either way is fine PROVIDED MalformedDependencyEdgeError is itself a
    # SpineReadError -- composition_graph.py's `except SpineReadError:
    # return undetermined(...)` sites must still catch it. Assert the
    # subclass relationship, not the concrete class: that is the actual
    # contract this test protects, and pinning only the concrete class would
    # let a future edit re-detach it from SpineReadError unnoticed.
    body = """\
- id: C1
  title: depends_on declared as a dict instead of a list
  change_kind: code-edit
  surface: some/surface
  depends_on:
    chunk: C1
    gate_kind: epistemic-premise
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(SpineReadError) as excinfo:
        read_spine(plan_path)
    # Pin the actual regression path too: the preflight, not spine_read's
    # own coercion check, must be what fires here (see comment above).
    assert isinstance(excinfo.value, MalformedDependencyEdgeError)


def test_resolvable_depends_on_succeeds(tmp_path):
    body = """\
- id: C1
  title: predecessor
  surface: some/surface
  writes:
    - some/file.py
- id: C2
  title: successor
  surface: some/surface
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}

    assert rows["C2"].depends_on == [{"chunk": "C1", "gate_kind": "output-consumption-runtime"}]


def test_absent_spine_block_raises_spine_read_error(tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# fixture plan\n\nno tasks block here.\n", encoding="utf-8")

    with pytest.raises(SpineReadError):
        read_spine(plan_path)


def test_reads_and_depends_on_default_to_empty_list(tmp_path):
    body = """\
- id: C1
  title: minimal row
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)
    rows = read_spine(plan_path)

    assert rows[0].reads == []
    assert rows[0].depends_on == []


def test_duplicate_id_raises_invalid_row_id_error(tmp_path):
    # Review: coordinator:code-reviewer (wsc-A, ecb99d36) P1 — a duplicate
    # id silently collapsed wave_map._predecessors' dict-keyed-by-id graph
    # instead of raising; the fix is to fail loud here, once, for every
    # downstream consumer.
    body = """\
- id: C1
  title: first
  surface: some/surface
- id: C1
  title: duplicate of the first
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(InvalidRowIdError) as excinfo:
        read_spine(plan_path)

    assert "C1" in str(excinfo.value)


def test_missing_id_raises_invalid_row_id_error(tmp_path):
    body = """\
- title: no id key at all
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(InvalidRowIdError):
        read_spine(plan_path)


def test_non_string_id_raises_invalid_row_id_error(tmp_path):
    body = """\
- id: 123
  title: numeric id
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(InvalidRowIdError):
        read_spine(plan_path)


def test_falsy_scalar_reads_raises_instead_of_silently_coercing(tmp_path):
    # Review: coordinator:code-reviewer (wsc-A, ecb99d36) P2 — `reads:` used
    # `raw.get("reads") or []`, which silently coerced a falsy-but-invalid
    # declared value (e.g. `reads: 0`) to `[]` instead of raising, an
    # asymmetry with `writes:`'s explicit `is None` check.
    body = """\
- id: C1
  title: reads declared as a falsy scalar
  surface: some/surface
  reads: 0
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(InvalidFieldTypeError) as excinfo:
        read_spine(plan_path)

    assert "C1" in str(excinfo.value)


def test_falsy_scalar_depends_on_raises_instead_of_silently_coercing(tmp_path):
    body = """\
- id: C1
  title: depends_on declared as a falsy scalar
  surface: some/surface
  depends_on: 0
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(InvalidFieldTypeError) as excinfo:
        read_spine(plan_path)

    assert "C1" in str(excinfo.value)


@pytest.mark.parametrize("disposition", sorted(NON_DISPATCHABLE_DISPOSITIONS))
def test_closed_disposition_rows_are_excluded(tmp_path, disposition):
    body = f"""\
- id: C1
  title: closed row
  surface: some/surface
  disposition: {disposition}
- id: C2
  title: live row
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C2"}


def test_open_and_absent_disposition_rows_are_kept(tmp_path):
    body = """\
- id: C1
  title: explicitly open
  surface: some/surface
  disposition: open
- id: C2
  title: absent disposition defaults to open
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C1", "C2"}


def test_deferred_row_is_excluded_independent_of_disposition(tmp_path):
    body = """\
- id: C1
  title: deferred but otherwise open
  surface: some/surface
  deferred: true
- id: C2
  title: live row
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C2"}


def test_live_row_depends_on_filtered_row_does_not_raise_and_edge_is_stripped(tmp_path):
    body = """\
- id: C1
  title: shipped predecessor
  surface: some/surface
  disposition: coded
  writes:
    - some/file.py
- id: C2
  title: live successor depending on the shipped row
  surface: some/surface
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}

    assert set(rows) == {"C2"}
    assert rows["C2"].depends_on == []


def test_genuinely_dangling_referent_still_raises_after_filtering(tmp_path):
    body = """\
- id: C1
  title: shipped row, unrelated
  surface: some/surface
  disposition: coded
- id: C2
  title: depends on a chunk that never existed
  surface: some/surface
  depends_on:
    - chunk: C99
      gate_kind: epistemic-premise
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(DanglingDependencyError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "C2" in message


def test_external_gate_absent_blocks_key_and_no_evidence_excludes_row(tmp_path):
    body = """\
- id: C1
  title: blocked on another repo, no blocks key
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship first
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set()


def test_external_gate_explicit_blocks_execution_excludes_row(tmp_path):
    body = """\
- id: C1
  title: blocked on another repo, explicit blocks
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship first
      blocks: execution
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set()


def test_external_gate_blocks_ac_closure_does_not_exclude_row(tmp_path):
    body = """\
- id: C1
  title: only an acceptance criterion is gated
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship before the AC can close
      blocks: ac-closure
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C1"}


def test_external_gate_with_closure_evidence_now_excludes_row(tmp_path):
    # INVERTED by the joint gate-reader bump (2026-08-20). `closure_evidence`
    # is authored when the evidence has NOT arrived, so its natural content
    # describes what is being awaited -- and under the retired rule that
    # description cleared its own gate. Only `cleared: true` clears now.
    body = """\
- id: C1
  title: gate cleared
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship first
      blocks: execution
      closure_evidence: abc1234
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set(), (
        "a closure_evidence must no longer clear its own gate"
    )


def test_cleared_false_overrides_closure_evidence_and_excludes_row(tmp_path):
    # plan-tasks.schema.json 1.9.0: `cleared` asserts whether the gate IS
    # discharged; `closure_evidence` only names how that was or will be
    # verified. An explicit `cleared: false` therefore outranks evidence.
    # Regression: the reader ignored `cleared` entirely, so a status note
    # parked in `closure_evidence` silently disarmed the gate it documented
    # -- found against sat-06 C4, a row that writes into a sibling repo's
    # tree, gated on a DR still at `status: proposed`.
    body = """- id: C1
  title: gate documented but NOT discharged
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their DR must be RATIFIED first
      blocks: execution
      cleared: false
      closure_evidence: >-
        Status only - the DR was authored but is still proposed.
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set()


def test_cleared_absent_means_uncleared_whatever_evidence_is_named(tmp_path):
    # The 1.9.0 bump was the additive half only and left `closure_evidence`
    # clearing on its own. The joint two-repo bump its x-bump-note deferred
    # has now landed, so absence of `cleared` means UNCLEARED -- which is what
    # the schema always said the field meant.
    body = """- id: C1
  title: gate cleared by evidence, no cleared key
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship first
      blocks: execution
      closure_evidence: abc1234
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set(), (
        "with no `cleared` key the gate is uncleared, whatever evidence it names"
    )


def test_cleared_non_true_value_does_not_clear(tmp_path):
    # The fail-closed posture SURVIVES the bump and gets stronger. Before, a
    # malformed `cleared` fell through to closure_evidence and the row was
    # admitted; now only the literal True clears, so a malformed value cannot
    # clear a gate at all -- matching this module's posture for `blocks`.
    body = """- id: C1
  title: malformed cleared value
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship first
      blocks: execution
      cleared: "no"
      closure_evidence: abc1234
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set(), (
        "only the literal True clears; a malformed value cannot"
    )


def test_live_row_depends_on_gated_row_is_also_excluded(tmp_path):
    # A gated predecessor's work has not run -- unlike a satisfied
    # (shipped/deferred) predecessor, its dependent is NOT known-satisfied
    # and must not be promoted into a wave with the edge merely stripped.
    body = """\
- id: C1
  title: blocked predecessor
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship first
- id: C2
  title: live successor depending on the gated row
  surface: some/surface
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set()


def test_transitive_dependent_of_gated_row_is_excluded_two_hops_out(tmp_path):
    # C1b gated -> C3 depends on C1b -> C7 depends on C3. C7 never
    # references C1b directly; the exclusion must propagate through C3.
    body = """\
- id: C1b
  title: gated predecessor
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship first
- id: C3
  title: depends directly on the gated row
  surface: some/surface
  depends_on:
    - chunk: C1b
      gate_kind: output-consumption-runtime
- id: C7
  title: depends on C3, two hops from the gate
  surface: some/surface
  depends_on:
    - chunk: C3
      gate_kind: output-consumption-runtime
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set()


def test_coded_rows_dependent_is_still_promoted_with_edge_stripped(tmp_path):
    # Must not regress: a satisfied (shipped) exclusion still only strips
    # the edge -- the dependent stays dispatchable.
    body = """\
- id: C1
  title: shipped predecessor
  surface: some/surface
  disposition: coded
- id: C2
  title: live successor depending on the shipped row
  surface: some/surface
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}

    assert set(rows) == {"C2"}
    assert rows["C2"].depends_on == []


def test_row_both_coded_and_gated_resolves_as_satisfied_not_blocked(tmp_path):
    # A row that is both closed-disposition AND carries an uncleared gate
    # resolves as satisfied: its work shipped, so the stale gate is
    # bookkeeping, not a live blocker -- its dependent is edge-stripped and
    # kept, not excluded.
    body = """\
- id: C1
  title: shipped but with a stale uncleared gate
  surface: some/surface
  disposition: coded
  external_gate:
    - owner_repo: some-other-repo
      condition: stale, never cleared
- id: C2
  title: live successor
  surface: some/surface
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}

    assert set(rows) == {"C2"}
    assert rows["C2"].depends_on == []


def test_row_gated_with_blocks_ac_closure_is_still_scheduled(tmp_path):
    # `blocks: ac-closure` never counts as an execution-blocking gate (see
    # _has_uncleared_execution_gate) -- a dependent on such a row is
    # neither excluded nor edge-stripped.
    body = """\
- id: C1
  title: only an acceptance criterion is gated
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship before the AC can close
      blocks: ac-closure
- id: C2
  title: live successor depending on the ac-closure-gated row
  surface: some/surface
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}

    assert set(rows) == {"C1", "C2"}
    assert rows["C2"].depends_on == [{"chunk": "C1", "gate_kind": "output-consumption-runtime"}]


def test_external_gate_two_entries_one_cleared_one_uncleared_excludes_row(tmp_path):
    body = """\
- id: C1
  title: two gates, one cleared one not
  surface: some/surface
  external_gate:
    - owner_repo: repo-a
      condition: cleared already
      closure_evidence: def5678
    - owner_repo: repo-b
      condition: still open
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set()


def test_malformed_external_gate_scalar_does_not_raise(tmp_path):
    body = """\
- id: C1
  title: external_gate declared as a bare string
  surface: some/surface
  external_gate: repo-a is blocking
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C1"}


def test_malformed_external_gate_entry_bare_string_does_not_raise(tmp_path):
    body = """\
- id: C1
  title: external_gate entry is a bare string, not a mapping
  surface: some/surface
  external_gate:
    - repo-a is blocking
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C1"}


def test_bare_string_depends_on_entry_raises_malformed_dependency_edge_error(tmp_path):
    # Audit A2: a bare-string edge used to coerce to `chunk=None` and raise
    # DanglingDependencyError naming an id that was never looked at. This
    # row omits `change_kind`, so the schema-shape preflight's first hit is
    # the unrelated missing-required-field error (not depends_on-prefixed)
    # and is ignored — this exercises spine_read's OWN malformed-edge check.
    body = """\
- id: C1
  title: depends_on entry is a bare string, not a mapping
  surface: some/surface
  depends_on:
    - C2
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(MalformedDependencyEdgeError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "C1" in message
    assert "C2" in message
    # The coercion artefact this fix removes: chunk=None must never appear.
    assert "None" not in message


def test_depends_on_entry_missing_chunk_key_raises_malformed_dependency_edge_error(tmp_path):
    body = """\
- id: C1
  title: depends_on entry has no chunk key
  surface: some/surface
  depends_on:
    - gate_kind: epistemic-premise
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(MalformedDependencyEdgeError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "C1" in message
    assert "None" not in message


def test_scalar_depends_on_entry_raises_malformed_dependency_edge_error(tmp_path):
    body = """\
- id: C1
  title: depends_on entry is a bare scalar
  surface: some/surface
  depends_on:
    - 3
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(MalformedDependencyEdgeError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "C1" in message
    assert "3" in message
    assert "None" not in message


def test_schema_shape_preflight_raises_with_validator_message(tmp_path):
    # Audit A3: when the row otherwise satisfies the base schema's required
    # fields (id/title/change_kind/surface), the depends_on shape preflight
    # via schema_validate.check_plan_tasks_source fires FIRST and its own
    # diagnosis — not spine_read's hand-rolled one — is what gets raised.
    body = """\
- id: C1
  title: depends_on entry is a bare string, not a mapping
  change_kind: code-edit
  surface: some/surface
  depends_on:
    - C2
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(MalformedDependencyEdgeError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "depends_on[0]" in message
    assert "expected object, got str" in message


def test_schema_shape_preflight_catches_out_of_enum_gate_kind_for_free(tmp_path):
    # Audit follow-up: an out-of-enum gate_kind is not itself checked by
    # spine_read, but the schema-shape preflight catches it for free WHEN
    # every row otherwise satisfies the base required-field schema — the
    # gate_kind enum lives on the same depends_on[].gate_kind schema node
    # the preflight already consults.
    body = """\
- id: C0
  title: predecessor
  change_kind: code-edit
  surface: some/surface
- id: C1
  title: successor with an invalid gate_kind
  change_kind: code-edit
  surface: some/surface
  depends_on:
    - chunk: C0
      gate_kind: banana
"""
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(MalformedDependencyEdgeError) as excinfo:
        read_spine(plan_path)

    message = str(excinfo.value)
    assert "gate_kind" in message
    assert "banana" in message


def test_out_of_enum_gate_kind_passes_silently_when_other_required_fields_absent(tmp_path):
    # Residual gap, deliberately NOT fixed here: check_plan_tasks_source
    # returns only its FIRST row-order error, and this row (like every
    # other fixture in this file) omits `change_kind` — that missing-field
    # error is not depends_on-prefixed, so the preflight is silent and
    # spine_read's own reader, which never inspects gate_kind's value,
    # lets `banana` straight through unchallenged.
    body = """\
- id: C0
  title: predecessor
  surface: some/surface
- id: C1
  title: successor with an invalid gate_kind
  surface: some/surface
  depends_on:
    - chunk: C0
      gate_kind: banana
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}

    assert rows["C1"].depends_on == [{"chunk": "C0", "gate_kind": "banana"}]


def test_unrecognized_blocks_value_still_excludes(tmp_path):
    """A `blocks` value outside the schema enum resolves to execution.

    Fail-closed: only the literal ``ac-closure`` spares a row, so an author's
    typo cannot silently disarm the gate and re-admit the row to a wave.
    """
    body = """- id: C1
  title: gate whose blocks value is misspelled
  surface: some/surface
  external_gate:
    - owner_repo: doe-claude
      condition: their reader lands
      blocks: exection
"""
    plan_path = _write_plan(tmp_path, body)

    assert read_spine(plan_path) == []


# --- An unrecognized disposition refuses instead of dispatching ---
# example-retrieval-repo-ue-addon-em, 2026-08-20: `disposition: done` is schema-invalid AND
# fell through the closed-value membership test, so a reconciliation pass using a
# plausible-but-wrong word dispatched exactly as if no reconciliation had run.


def test_an_unknown_disposition_refuses_rather_than_dispatching(tmp_path):
    body = """\
- id: C1
  title: reconciled with a plausible but invalid word
  surface: some/surface
  disposition: done
- id: C2
  title: live row
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)
    with pytest.raises(UnknownDispositionError) as excinfo:
        read_spine(plan_path)
    message = str(excinfo.value)
    assert "'C1'" in message
    assert "'done'" in message
    # The correction is readable off the error, not only out of the schema.
    assert "coded" in message


def test_unknown_disposition_error_is_a_spine_read_error(tmp_path):
    # Callers catching SpineReadError must not miss this one.
    body = """\
- id: C1
  title: bad value
  surface: some/surface
  disposition: complete
"""
    plan_path = _write_plan(tmp_path, body)
    with pytest.raises(SpineReadError):
        read_spine(plan_path)


@pytest.mark.parametrize("disposition", sorted(KNOWN_DISPOSITIONS))
def test_every_schema_disposition_is_accepted(tmp_path, disposition):
    # Guards the two sets drifting apart: a value the schema adds that this
    # module does not learn would refuse every spine using it.
    body = f"""\
- id: C1
  title: schema-legal row
  surface: some/surface
  disposition: {disposition}
- id: C2
  title: live row
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)
    read_spine(plan_path)  # must not raise


# ---------------------------------------------------------------------------
# execution_mode: a row a human must run
# ---------------------------------------------------------------------------


def test_operator_row_is_not_dispatched():
    from coordinator_core.ops.dispatch_emit.spine_read import _is_operator_row

    assert _is_operator_row({"execution_mode": "operator"}) is True


def test_exclusion_is_opt_in_so_no_existing_plan_changes_behaviour():
    """Absent, `agent`, null, and any unrecognised value all still dispatch.

    A typo must fail toward the OLD default. A row that vanishes from a wave
    map is far harder to notice than one dispatched to an agent that reports
    it cannot proceed, so `"Operator"` and `"human"` dispatch rather than
    silently pulling the row out of the run.
    """
    from coordinator_core.ops.dispatch_emit.spine_read import _is_operator_row

    for raw in ({}, {"execution_mode": "agent"}, {"execution_mode": None},
                {"execution_mode": "Operator"}, {"execution_mode": "human"}):
        assert _is_operator_row(raw) is False, raw


def test_operator_blocks_like_a_gate_not_like_a_deferral():
    """An operator row's work has NOT run at emit time, so a dependent
    dispatched now would run against work that does not exist. It must join
    `blocked_ids` (transitive) rather than `satisfied_ids` (edge-stripped)."""
    import inspect

    from coordinator_core.ops.dispatch_emit import spine_read

    src = inspect.getsource(spine_read.read_spine)
    # The operator predicate sits on the gate arm, beside the gate check.
    assert "_has_uncleared_execution_gate(raw) or _is_operator_row(raw)" in src


def test_an_excluded_row_is_reported_not_silently_dropped():
    """Exclusion without surfacing is a silent skip, and for an operator row
    that is worse than the mis-dispatch the field exists to prevent."""
    from coordinator_core.ops.dispatch_emit.emit import _excluded_rows_narration

    out = _excluded_rows_narration([
        {"id": "C7", "reason": "operator", "detail": "execution_mode: operator — a human must run this row"},
    ])
    assert "C7" in out
    assert "DOES NOT RUN" in out
    assert "OWED WORK, not skipped work" in out


def test_operator_rows_are_called_out_separately_from_other_exclusions():
    """A deferred row's work is done or dropped; an operator row's is owed."""
    from coordinator_core.ops.dispatch_emit.emit import _excluded_rows_narration

    out = _excluded_rows_narration([
        {"id": "C3", "reason": "deferred", "detail": "deferred: true"},
    ])
    assert "C3" in out
    assert "OWED WORK" not in out  # no operator row -> no owed-work banner
