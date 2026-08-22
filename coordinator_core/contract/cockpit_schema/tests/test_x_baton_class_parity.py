"""
test_x_baton_class_parity — AC2/AC3 for "x-baton-class travels to the tools
that vendor it" (docs/plans/2026-08-21-x-baton-class-travels-to-the-tools-
that-vendor-it.md § C1).

AC2: the emitted `handoff-summary.schema.json`'s top-level `x-baton-class.
mapping` has exactly one entry per member of that same schema's own
`properties.kind.enum` (10 today), and for every one of them
`mapping[k] == coordinator_core.frontmatter.baton_class.baton_class(k)` —
`spike-result` present, valued `null`.

AC3: the mapping is GENERATED at emit time (no second hand-typed `kind ->
class` table anywhere in `cockpit_schema/`), and the `_reorder`/pipeline
behaviour that lets a schema-LEVEL key absent from `_KEY_ORDER` survive
emission is asserted directly, not assumed.

Spec backlink: docs/plans/2026-08-21-x-baton-class-travels-to-the-tools-that-vendor-it.md § C1.
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import (
    _KEY_ORDER,
    _reorder,
    build_entity_schema,
    emit_schemas,
)
from coordinator_core.contract.cockpit_schema.entities.summaries import HandoffSummary
from coordinator_core.frontmatter.baton_class import baton_class


def _emitted_handoff_summary() -> dict:
    return build_entity_schema(HandoffSummary)


def test_x_baton_class_top_level_key_present_with_mapping_and_description():
    shaped = _emitted_handoff_summary()
    kind_enum = shaped["properties"]["kind"]["enum"]

    from coordinator_core.contract.cockpit_schema.emit_schema import (
        _build_x_baton_class_annotation,
    )

    annotation = _build_x_baton_class_annotation(kind_enum)
    assert isinstance(annotation.get("description"), str) and annotation["description"]
    assert isinstance(annotation.get("mapping"), dict)


def test_mapping_has_one_entry_per_kind_enum_member():
    shaped = _emitted_handoff_summary()
    kind_enum = shaped["properties"]["kind"]["enum"]
    assert len(kind_enum) == 10, (
        f"HandoffKind enum grew/shrank ({len(kind_enum)} members) — this test's "
        "'10 today' assumption (AC2) needs a look, not a blind bump."
    )

    from coordinator_core.contract.cockpit_schema.emit_schema import (
        _build_x_baton_class_annotation,
    )

    mapping = _build_x_baton_class_annotation(kind_enum)["mapping"]
    assert set(mapping.keys()) == set(kind_enum)


def test_mapping_values_equal_baton_class_for_every_kind_including_spike_result():
    shaped = _emitted_handoff_summary()
    kind_enum = shaped["properties"]["kind"]["enum"]

    from coordinator_core.contract.cockpit_schema.emit_schema import (
        _build_x_baton_class_annotation,
    )

    mapping = _build_x_baton_class_annotation(kind_enum)["mapping"]
    for kind in kind_enum:
        assert mapping[kind] == baton_class(kind), (
            f"x-baton-class.mapping[{kind!r}] ({mapping[kind]!r}) diverges from "
            f"baton_class({kind!r}) ({baton_class(kind)!r})"
        )

    assert "spike-result" in mapping
    assert mapping["spike-result"] is None


def test_full_emission_carries_x_baton_class_on_handoff_summary_and_bundle(tmp_path):
    emitted = emit_schemas(ENTITY_SCHEMAS, out_dir=tmp_path)

    handoff_summary = emitted["handoff-summary"]
    assert "x-baton-class" in handoff_summary
    assert set(handoff_summary["x-baton-class"]["mapping"].keys()) == set(
        handoff_summary["properties"]["kind"]["enum"]
    )

    import json

    bundle = json.loads((tmp_path / "cockpit-contract.schema.json").read_text(encoding="utf-8"))
    assert "x-baton-class" in bundle["$defs"]["handoff-summary"]
    assert bundle["$defs"]["handoff-summary"]["x-baton-class"] == handoff_summary["x-baton-class"]

    # No leak into a NESTED inlining of HandoffSummary (snapshot-envelope.schema.json
    # embeds `handoffs: list[HandoffSummary]` — the plan's Anti-scope forbids
    # reshaping any OTHER entity's emission while landing this one).
    snapshot_envelope = emitted["snapshot-envelope"]
    assert "x-baton-class" not in snapshot_envelope["properties"]["handoffs"]["items"]


def test_reorder_appends_a_top_level_key_absent_from_key_order_rather_than_dropping_it():
    """Verify (not assume) `_reorder`'s documented top-level-append guarantee for
    a schema-level extension key, per the plan's substrate-verify instruction."""
    assert "x-baton-class" not in _KEY_ORDER

    node = {
        "type": "object",
        "x-baton-class": {"description": "d", "mapping": {"a": "b"}},
        "properties": {},
    }
    reordered = _reorder(node)
    assert "x-baton-class" in reordered
    assert reordered["x-baton-class"] == {"description": "d", "mapping": {"a": "b"}}


def test_no_second_hand_typed_kind_to_class_table_in_cockpit_schema_package():
    """AC3 guard: the only place `cockpit_schema/` may compute a `kind ->
    baton_class` value is via `frontmatter.baton_class.baton_class()` —
    fail loud if a second literal `kind: class` mapping is introduced.

    Scans this package's own source (not tests/ — a test fixture asserting
    an expected value is not a second SOURCE of truth) for the tell-tale
    shape: a dict literal whose keys look like `HandoffKind` values and
    whose values look like `BatonClass` values, anywhere other than inside
    `_build_x_baton_class_annotation` itself (which computes, never
    hand-types, via a dict comprehension over `baton_class()`).
    """
    import ast
    from pathlib import Path

    package_dir = Path(__file__).resolve().parent.parent
    kind_values = {
        "session-handoff", "spinoff", "spinoff-roadmap", "recovery",
        "spinoff-goal", "spinoff-roadmap-creator", "spike-result",
        "roadmap-baton", "roadmap-seed", "goal-seed",
    }
    class_values = {"continuation", "deflection", "intention"}

    offenders = []
    for path in package_dir.rglob("*.py"):
        if "tests" in path.relative_to(package_dir).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            values = {
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            }
            if keys & kind_values and values & class_values:
                offenders.append(f"{path}:{node.lineno}")

    assert offenders == [], (
        "found a hand-typed kind->baton_class-shaped dict literal outside "
        f"baton_class(): {offenders} — the mapping must be GENERATED via "
        "frontmatter.baton_class.baton_class(), never re-declared."
    )
