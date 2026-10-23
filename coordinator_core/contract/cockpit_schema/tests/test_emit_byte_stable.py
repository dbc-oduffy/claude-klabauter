"""Regen over an unchanged contract is byte-stable: two emits of the full registry are identical."""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import emit_schemas


def test_two_consecutive_emits_are_byte_identical(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    emit_schemas(ENTITY_SCHEMAS, out_dir=first)
    emit_schemas(ENTITY_SCHEMAS, out_dir=second)

    names = sorted(p.name for p in first.glob("*.schema.json"))
    assert names == sorted(p.name for p in second.glob("*.schema.json"))
    assert "cockpit-contract.schema.json" in names
    differing = [n for n in names if (first / n).read_bytes() != (second / n).read_bytes()]
    assert differing == []
    assert '"description"' in (first / "cockpit-contract.schema.json").read_text(encoding="utf-8")
