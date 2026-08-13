"""Tests for coordinator_core.ops.dispatch_emit.spine_read (C1)."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    UNDECLARED,
    DanglingDependencyError,
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
