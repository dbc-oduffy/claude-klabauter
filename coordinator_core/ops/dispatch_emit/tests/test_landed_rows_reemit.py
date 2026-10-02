"""Re-emitting a run that ended incomplete: landed rows drop, their edges are satisfied."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import _drop_landed_rows, landed_rows_from_text
from coordinator_core.ops.dispatch_emit.spine_read import EmitterRow


def _row(id_, deps=()):
    return EmitterRow(
        id=id_, title=id_, surface="s", writes=[f"p/{id_}.py"], reads=(),
        depends_on=[{"chunk": d} for d in deps],
    )


def test_landed_ids_parsed_from_checkpoint_subjects():
    text = "abc checkpoint(wave 1): 2 rows — A, B\ndef checkpoint(wave 2): 1 rows — C\nnoise"
    assert landed_rows_from_text(text) == {"A", "B", "C"}


def test_drop_landed_strips_edges_and_keeps_rest():
    rows = [_row("A"), _row("B", ["A"]), _row("C", ["A", "B"])]
    kept = _drop_landed_rows(rows, frozenset({"A"}))
    assert [r.id for r in kept] == ["B", "C"]
    assert kept[0].depends_on == []
    assert kept[1].depends_on == [{"chunk": "B"}]


def test_unknown_landed_id_refused():
    with pytest.raises(ValueError):
        _drop_landed_rows([_row("A")], frozenset({"Z"}))
