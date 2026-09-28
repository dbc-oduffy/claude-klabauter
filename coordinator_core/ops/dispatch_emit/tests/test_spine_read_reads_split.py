"""Tests for spine_read's reads_at_head/consumes split (C4, § Design D5).

`reads_at_head:` never orders; `consumes:` orders (same as `reads:`, which
keeps its meaning but logs a deprecation warning). AC4 is pinned end to end
through `wave_map.build_waves`.
"""

from __future__ import annotations

import logging

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    ContradictoryReadDeclarationError,
    InvalidFieldTypeError,
    read_spine,
)
from coordinator_core.ops.dispatch_emit.wave_map import build_waves

_HEADER = "# fixture plan\n\n## Tasks\n\n"


def _write_plan(tmp_path, body: str):
    path = tmp_path / "plan.md"
    path.write_text(_HEADER + "```yaml plan-tasks\n" + body + "\n```\n", encoding="utf-8")
    return path


def test_reads_at_head_defaults_to_empty_tuple(tmp_path):
    body = """\
- id: C1
  title: no reads_at_head declared
  surface: some/surface
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}
    assert rows["C1"].reads_at_head == ()
    assert rows["C1"].reads == []


def test_consumes_populates_ordering_reads(tmp_path):
    body = """\
- id: C1
  title: consumes some path
  surface: some/surface
  consumes:
    - some/file.py
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}
    assert rows["C1"].reads == ["some/file.py"]
    assert rows["C1"].reads_at_head == ()


def test_reads_and_consumes_union_into_ordering_set(tmp_path):
    body = """\
- id: C1
  title: both reads and consumes
  surface: some/surface
  reads:
    - a.py
  consumes:
    - b.py
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}
    assert rows["C1"].reads == ["a.py", "b.py"]


def test_reads_at_head_declared_and_never_orders(tmp_path):
    body = """\
- id: writer
  title: writes the file
  surface: some/surface
  writes:
    - some/file.py
- id: head_reader
  title: reads at head, never orders
  surface: some/surface
  writes: []
  reads_at_head:
    - some/file.py
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}
    assert rows["head_reader"].reads_at_head == ("some/file.py",)
    assert rows["head_reader"].reads == []

    waves = build_waves(list(rows.values()))
    # No read-after-write edge: writer and head_reader may share a wave.
    wave_ids = [{row.id for row in wave} for wave in waves]
    assert any({"writer", "head_reader"} <= ids for ids in wave_ids)


def test_consumes_still_orders_after_writer(tmp_path):
    body = """\
- id: writer
  title: writes the file
  surface: some/surface
  writes:
    - some/file.py
- id: consumer
  title: consumes it, orders after writer
  surface: some/surface
  consumes:
    - some/file.py
"""
    plan_path = _write_plan(tmp_path, body)
    rows = {row.id: row for row in read_spine(plan_path)}
    waves = build_waves(list(rows.values()))
    wave_of = {}
    for i, wave in enumerate(waves):
        for row in wave:
            wave_of[row.id] = i
    assert wave_of["writer"] < wave_of["consumer"]


def test_same_path_in_reads_at_head_and_consumes_raises(tmp_path):
    body = """\
- id: C1
  title: contradiction
  surface: some/surface
  reads_at_head:
    - some/file.py
  consumes:
    - some/file.py
"""
    plan_path = _write_plan(tmp_path, body)
    with pytest.raises(ContradictoryReadDeclarationError):
        read_spine(plan_path)


def test_same_path_in_reads_at_head_and_reads_raises(tmp_path):
    body = """\
- id: C1
  title: contradiction via legacy reads
  surface: some/surface
  reads_at_head:
    - some/file.py
  reads:
    - some/file.py
"""
    plan_path = _write_plan(tmp_path, body)
    with pytest.raises(ContradictoryReadDeclarationError):
        read_spine(plan_path)


def test_contradictory_read_declaration_is_a_field_type_error_subclass():
    assert issubclass(ContradictoryReadDeclarationError, InvalidFieldTypeError)


def test_reads_at_head_scalar_raises_invalid_field_type(tmp_path):
    body = """\
- id: C1
  title: scalar reads_at_head
  surface: some/surface
  reads_at_head: some/file.py
"""
    plan_path = _write_plan(tmp_path, body)
    with pytest.raises(InvalidFieldTypeError):
        read_spine(plan_path)


def test_consumes_scalar_raises_invalid_field_type(tmp_path):
    body = """\
- id: C1
  title: scalar consumes
  surface: some/surface
  consumes: some/file.py
"""
    plan_path = _write_plan(tmp_path, body)
    with pytest.raises(InvalidFieldTypeError):
        read_spine(plan_path)


def test_reads_key_logs_deprecation_warning(tmp_path, caplog):
    body = """\
- id: C1
  title: uses legacy reads key
  surface: some/surface
  reads:
    - a.py
"""
    plan_path = _write_plan(tmp_path, body)
    with caplog.at_level(logging.WARNING):
        read_spine(plan_path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "C1" in warnings[0].getMessage()
    assert "reads:" in warnings[0].getMessage()


def test_reads_key_absent_logs_no_warning(tmp_path, caplog):
    body = """\
- id: C1
  title: no legacy reads key
  surface: some/surface
  consumes:
    - a.py
"""
    plan_path = _write_plan(tmp_path, body)
    with caplog.at_level(logging.WARNING):
        read_spine(plan_path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings


def test_warning_names_every_row_using_reads_key(tmp_path, caplog):
    body = """\
- id: C1
  title: legacy reads
  surface: some/surface
  reads:
    - a.py
- id: C2
  title: also legacy reads
  surface: some/surface
  reads:
    - b.py
- id: C3
  title: consumes, not reads
  surface: some/surface
  consumes:
    - c.py
"""
    plan_path = _write_plan(tmp_path, body)
    with caplog.at_level(logging.WARNING):
        read_spine(plan_path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "C1" in message
    assert "C2" in message
    assert "C3" not in message
