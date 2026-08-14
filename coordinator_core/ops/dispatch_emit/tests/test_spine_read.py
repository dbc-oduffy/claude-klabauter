"""Tests for coordinator_core.ops.dispatch_emit.spine_read (C1)."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    NON_DISPATCHABLE_DISPOSITIONS,
    UNDECLARED,
    DanglingDependencyError,
    InvalidFieldTypeError,
    InvalidRowIdError,
    SpineReadError,
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
    assert "C99" in message
