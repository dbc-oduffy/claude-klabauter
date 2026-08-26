"""Characterization tests for coordinator_core.ops.emit_artifact_shape_contract.

Port source: DoE-claude coordinator/bin/emit-artifact-shape-contract.js (retired
2026-07-24, D1 of docs/plans/2026-07-24-python-ize-claude-klabauter-bin-oracles-doe-forwards-to.md
— zero fleet callers per the fleet-reachability gate, no trampoline). Independently
re-derives the assertions from DoE-claude coordinator/bin/tests/
test-emit-artifact-shape-contract.js (not a re-assertion of this module's own
transcription) — each `it(...)` block in that file has a corresponding test here,
run against a FRESH emit of the real DoE-claude coordinator/schemas/ tree, plus a
synthetic-schema-dir unit suite that exercises field_to_json_schema/schema_to_json_schema
edge cases directly (no cross-repo dependency).

The former node-shellout golden-oracle structural-parity test (independent execution of
the JS emitter against the same schemas/ tree) was retired alongside the oracle it
compared against — there is no longer a live emit-artifact-shape-contract.js anywhere to
shell out to. The real-tree assertions below (against DOE_SCHEMAS_DIR) and the
synthetic-schema-dir unit suite remain as this port's standalone coverage.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.emit_artifact_shape_contract import (
    CONTRACT_VERSION,
    LIVENESS_MAPPING,
    SUB_SHAPES,
    field_to_json_schema,
    main,
    schema_to_json_schema,
)
from coordinator_core.ops.records_query import liveness as _records_liveness
from coordinator_core.testing.doe_root import resolve_doe_root

DOE_ROOT = Path(resolve_doe_root() or "/doe-root-unresolved")
DOE_COORDINATOR_ROOT = DOE_ROOT / "coordinator"
DOE_SCHEMAS_DIR = DOE_COORDINATOR_ROOT / "schemas"

_SIBLING_AVAILABLE = DOE_SCHEMAS_DIR.is_dir()


def _run_python_emit(tmp_path: Path, monkeypatch) -> dict:
    out_dir = tmp_path / "out"
    monkeypatch.setenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", str(DOE_COORDINATOR_ROOT))
    monkeypatch.setenv("ARTIFACT_CONTRACT_OUT_DIR", str(out_dir))
    rc = main([])
    assert rc == 0
    out_file = out_dir / "artifact-shape-contract.schema.json"
    assert out_file.is_file()
    return json.loads(out_file.read_text(encoding="utf-8"))


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    if not _SIBLING_AVAILABLE:
        pytest.skip("DoE-claude sibling repo (coordinator/schemas) not available")
    return _run_python_emit(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Field/schema-translation unit tests — no cross-repo dependency.
# Independently re-derives fieldToJsonSchema's descriptor-shape contract from the
# JS source (schema.js lines 61-224) rather than re-asserting this port's own logic.
# ---------------------------------------------------------------------------


class TestFieldToJsonSchema:
    def test_scalar_string_type(self):
        assert field_to_json_schema("f", "string") == {"type": "string"}

    def test_scalar_iso_date_or_datetime_is_anyof(self):
        frag = field_to_json_schema("f", "iso-date-or-datetime")
        assert frag == {
            "anyOf": [
                {"type": "string", "format": "date"},
                {"type": "string", "format": "date-time"},
            ]
        }

    def test_scalar_types_are_independent_copies(self):
        """Mutating one field's returned fragment must not leak into a sibling field
        using the same scalar token — the structuredClone/deepcopy fix (JS review F3)."""
        frag_a = field_to_json_schema("a", "iso-date-or-datetime")
        frag_b = field_to_json_schema("b", "iso-date-or-datetime")
        frag_a["anyOf"].append({"type": "string", "format": "mutated"})
        assert frag_b == {
            "anyOf": [
                {"type": "string", "format": "date"},
                {"type": "string", "format": "date-time"},
            ]
        }

    def test_unrecognised_scalar_emits_note(self):
        frag = field_to_json_schema("weird_field", "not-a-real-type")
        assert frag == {
            "description": '[emit-note] unrecognised field type "not-a-real-type" for field "weird_field"'
        }

    def test_enum_descriptor(self):
        frag = field_to_json_schema("status", {"type": "enum", "values": ["a", "b"]})
        assert frag == {"enum": ["a", "b"]}

    def test_enum_descriptor_with_description(self):
        frag = field_to_json_schema("status", {"type": "enum", "values": ["a"], "description": "d"})
        assert frag == {"enum": ["a"], "description": "d"}

    def test_nested_object_with_fields(self):
        frag = field_to_json_schema(
            "loe", {"type": "object", "fields": {"tshirt": "string", "hours": "number"}}
        )
        assert frag == {
            "type": "object",
            "properties": {
                "tshirt": {"type": "string"},
                "hours": {"type": "number"},
            },
        }

    def test_bare_nested_object_no_type_wrapper(self):
        # Mirrors bug-backlog/debt-backlog/improvement-queue/lesson-entry `system` field shape.
        frag = field_to_json_schema("system", {"name": "string", "version": "string"})
        assert frag == {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "version": {"type": "string"},
            },
        }

    def test_null_descriptor_emits_note(self):
        frag = field_to_json_schema("f", None)
        assert frag == {"description": '[emit-note] null/missing descriptor for field "f"'}

    def test_unrecognised_nested_descriptor_emits_note(self):
        frag = field_to_json_schema("f", {"type": "totally-unknown-nested"})
        assert "[emit-note] unrecognised descriptor" in frag["description"]


class TestSchemaToJsonSchema:
    def test_yaml_dialect_required_and_optional(self):
        schema = {
            "required": {"id": "string"},
            "optional": {"note": "string"},
        }
        js = schema_to_json_schema("widget", schema, "widget.yaml")
        assert js["type"] == "object"
        assert js["required"] == ["id"]
        assert set(js["properties"].keys()) == {"id", "note"}
        assert "note" not in js["required"]

    def test_applies_to_carried_as_x_extension(self):
        schema = {"required": {}, "applies_to": "state/widgets/*.yaml"}
        js = schema_to_json_schema("widget", schema, "widget.yaml")
        assert js["x-coordinator-applies_to"] == "state/widgets/*.yaml"

    def test_json_schema_backed_passthrough(self):
        schema = {
            "_isJsonSchema": True,
            "type": "object",
            "description": "d",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "applies_to": "state/x/*.json",
        }
        js = schema_to_json_schema("widget", schema, "widget.schema.json")
        assert js["properties"] == {"id": {"type": "string"}}
        assert js["required"] == ["id"]
        assert js["x-coordinator-applies_to"] == "state/x/*.json"

    def test_json_schema_backed_empty_required_array_omitted(self):
        schema = {"_isJsonSchema": True, "properties": {}, "required": []}
        js = schema_to_json_schema("widget", schema, "widget.schema.json")
        assert "required" not in js

    def test_json_schema_backed_carries_allof_and_additional_properties(self):
        """Regression test for the silently-dropped-conditionals defect (filed
        state/bug-backlog/2026-07-28-artifact-shape-contract-emitter-silently-…):
        a top-level `allOf` (cross-field rule) and `additionalProperties` on a
        `.schema.json`-backed source must survive into the emitted $def — a
        real consumer (example-retrieval-repo bin/validate-artifacts.py,
        coordinator/tests/test_handoff_corpus_conformance.py) runs
        jsonschema.Draft202012Validator against these emitted $defs, so a
        dropped allOf/additionalProperties is a silently-unenforced constraint,
        not a cosmetic omission."""
        schema = {
            "_isJsonSchema": True,
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
            "allOf": [{"if": {"required": ["b"]}, "then": {"required": ["a"]}}],
        }
        js = schema_to_json_schema("widget", schema, "widget.schema.json")
        assert js["additionalProperties"] is False
        assert js["allOf"] == [{"if": {"required": ["b"]}, "then": {"required": ["a"]}}]

    def test_json_schema_backed_omits_allof_and_additional_properties_when_absent(self):
        """Absence must stay absence — the carry-through must not fabricate either
        key for a source schema that never declared it."""
        schema = {"_isJsonSchema": True, "properties": {}, "required": []}
        js = schema_to_json_schema("widget", schema, "widget.schema.json")
        assert "allOf" not in js
        assert "additionalProperties" not in js


# ---------------------------------------------------------------------------
# Constants — LIVENESS_MAPPING / SUB_SHAPES shape locks (independent of any emit run)
# ---------------------------------------------------------------------------


def test_liveness_mapping_version_matches_contract_version():
    assert LIVENESS_MAPPING["version"] == CONTRACT_VERSION


def test_liveness_mapping_handoff_is_two_axis():
    assert LIVENESS_MAPPING["types"]["handoff"]["combination_rule"] == "two-axis"


# ---------------------------------------------------------------------------
# C8b dedup (2026-07-27, plan-line-item-resolution-model): LIVENESS_MAPPING's
# per-status leaf values are now DERIVED from records_query.liveness() (see
# emit_artifact_shape_contract.py's own comment at the LIVENESS_MAPPING
# definition) rather than hand-transcribed a second time. These tests assert
# agreement BY CONSTRUCTION — every published (record_type, status) pair is
# re-evaluated against the live liveness() function at test time, so a future
# edit to liveness() that silently changes a published value fails loud here,
# instead of the two copies coincidentally continuing to match.
# ---------------------------------------------------------------------------

_SINGLE_AXIS_CONTRACT_TO_QUERY_TYPE = {
    "cross-repo-memo": "cross-repo-memo",
    # "plan" was the last literal-dict holdout among the single-axis types (see
    # emit_artifact_shape_contract.py's own comment at LIVENESS_MAPPING["types"]["plan"])
    # until the 2026-07-27 rename-coupling fix converted it to _derive_status_mapping(...)
    # like every sibling here — it is asserted by the SAME parametrized case as the rest,
    # not a separate one, because there is no longer a shape difference to test for.
    "plan": "plan",
    "decision": "decision",
    "improvement-queue": "improvement",
    "bug-backlog": "bug",
    "debt-backlog": "debt",
    "lesson": "lesson",
    "roadmap": "roadmap",
    "tracker": "tracker",
    "health-status": "health-status",
    "decision-guide": "decision-guide",
}


@pytest.mark.parametrize("contract_key,query_type", sorted(_SINGLE_AXIS_CONTRACT_TO_QUERY_TYPE.items()))
def test_liveness_mapping_single_axis_agrees_with_records_query_by_construction(contract_key, query_type):
    mapping = LIVENESS_MAPPING["types"][contract_key]["mapping"]
    for status, expected in mapping.items():
        assert _records_liveness({"status": status}, query_type) == expected, (
            f"{contract_key}/{status}: LIVENESS_MAPPING says {expected!r}, "
            f"records_query.liveness() says "
            f"{_records_liveness({'status': status}, query_type)!r}"
        )


def test_liveness_mapping_handoff_axes_agree_with_records_query_by_construction():
    axes = LIVENESS_MAPPING["types"]["handoff"]["axes"]
    for status, expected in axes["status"].items():
        assert _records_liveness({"status": status}, "handoff") == expected
    for deployment_state, expected in axes["deployment_state"].items():
        assert _records_liveness({"deployment_state": deployment_state}, "handoff") == expected


def test_sub_shapes_provenance_envelope_required_fields():
    required = sorted(SUB_SHAPES["ProvenanceEnvelope"]["required"])
    assert required == ["derivation", "entity_anchor", "observed_at", "path", "ref", "repo", "source_kind"]


def test_sub_shapes_provenance_envelope_has_four_conditionals():
    assert len(SUB_SHAPES["ProvenanceEnvelope"]["allOf"]) == 4


# ---------------------------------------------------------------------------
# Exit-code contract (business codes, no sibling repo required)
# ---------------------------------------------------------------------------


def test_missing_coordinator_root_env_returns_2(monkeypatch):
    monkeypatch.delenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", raising=False)
    assert main([]) == 2


class TestArgvIsNotIgnored:
    """`main` once accepted argv and discarded it, so `--help` emitted the bundle into
    DoE-claude's working tree instead of printing usage. These pin that an operator
    reaching for an interface, or fat-fingering a flag, never triggers a peer-tree write.
    """

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_prints_usage_and_emits_nothing(self, flag, tmp_path, monkeypatch, capsys):
        # A valid root is deliberately present: help must win over a runnable config,
        # or the regression reappears the moment the env happens to be set.
        (tmp_path / "schemas").mkdir()
        out_dir = tmp_path / "out"
        monkeypatch.setenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", str(tmp_path))
        monkeypatch.setenv("ARTIFACT_CONTRACT_OUT_DIR", str(out_dir))

        assert main([flag]) == 0
        assert "usage: emit-artifact-shape-contract" in capsys.readouterr().out
        assert not out_dir.exists()

    def test_unrecognized_argument_returns_2_and_emits_nothing(self, tmp_path, monkeypatch):
        (tmp_path / "schemas").mkdir()
        out_dir = tmp_path / "out"
        monkeypatch.setenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", str(tmp_path))
        monkeypatch.setenv("ARTIFACT_CONTRACT_OUT_DIR", str(out_dir))

        assert main(["--dry-run"]) == 2
        assert not out_dir.exists()


def test_nonexistent_schemas_dir_returns_2(tmp_path, monkeypatch):
    monkeypatch.setenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", str(tmp_path / "nope"))
    assert main([]) == 2


def test_empty_schemas_dir_returns_1(tmp_path, monkeypatch):
    (tmp_path / "schemas").mkdir()
    monkeypatch.setenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_CONTRACT_OUT_DIR", str(tmp_path / "out"))
    assert main([]) == 1


def test_sub_shapes_collision_returns_1(tmp_path, monkeypatch):
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    # A schema literally named ProvenanceEnvelope collides with the injected SUB_SHAPES key.
    (schemas_dir / "x.yaml").write_text("schema: ProvenanceEnvelope\nrequired:\n  id: string\n")
    monkeypatch.setenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_CONTRACT_OUT_DIR", str(tmp_path / "out"))
    assert main([]) == 1


# ---------------------------------------------------------------------------
# Cross-schema $ref rewrite (2026-08-03, contract v3.2.0). Regression coverage for
# cross-repo/inbox/2026-08-03-doe-claude-em-artifact-contract-external-ref-survives-bundling.md:
# a $id-style cross-schema $ref (https://coordinator.local/schemas/<name>.schema.json)
# must be rewritten to its bundled #/$defs/<name> location, and an unregistered target
# must refuse to emit rather than ship an unresolvable ref.
# shell-doc-ok: quotes real JSON-Schema $id/$defs/$ref pointer syntax, not shell.
# ---------------------------------------------------------------------------


def _write_json_schema_source(schemas_dir: Path, filename: str, schema: dict) -> None:
    (schemas_dir / filename).write_text(json.dumps(schema), encoding="utf-8")


def test_unregistered_cross_schema_ref_refuses_to_emit(tmp_path, monkeypatch):
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    _write_json_schema_source(
        schemas_dir,
        "widget.schema.json",
        {
            "_isJsonSchema": True,
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "https://coordinator.local/schemas/nonexistent-thing.schema.json"},
                },
            },
        },
    )
    monkeypatch.setenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_CONTRACT_OUT_DIR", str(tmp_path / "out"))
    assert main([]) == 1
    assert not (tmp_path / "out" / "artifact-shape-contract.schema.json").exists()


def test_registered_cross_schema_ref_rewritten_to_intra_bundle_pointer(tmp_path, monkeypatch):
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    _write_json_schema_source(
        schemas_dir,
        "gadget-part.schema.json",
        {"_isJsonSchema": True, "type": "object", "properties": {"id": {"type": "string"}}},
    )
    _write_json_schema_source(
        schemas_dir,
        "gadget.schema.json",
        {
            "_isJsonSchema": True,
            "type": "object",
            "properties": {
                "parts": {
                    "type": "array",
                    "items": {"$ref": "https://coordinator.local/schemas/gadget-part.schema.json"},
                },
            },
        },
    )
    monkeypatch.setenv("EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_CONTRACT_OUT_DIR", str(tmp_path / "out"))
    assert main([]) == 0
    out = json.loads((tmp_path / "out" / "artifact-shape-contract.schema.json").read_text(encoding="utf-8"))
    assert out["$defs"]["gadget"]["properties"]["parts"]["items"]["$ref"] == "#/$defs/gadget-part"


# ---------------------------------------------------------------------------
# Real-tree parity — re-derives every assertion from the JS oracle's own test
# file (test-emit-artifact-shape-contract.js) independently against a fresh
# Python emit. Skipped when the DoE-claude sibling repo isn't checked out.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SIBLING_AVAILABLE, reason="DoE-claude sibling repo not available for real-tree parity check")
class TestRealTreeParity:
    def test_version_pin(self, bundle):
        # Deliberately a LITERAL, not CONTRACT_VERSION — the `bundle` fixture emits
        # fresh from the emitter, so asserting against the constant that produced it
        # is a tautology that catches nothing. The literal is the pin: a version bump
        # must be an explicit two-place edit, never a silent side effect. Bumped to
        # 8.0.0 alongside CONTRACT_VERSION (emit_artifact_shape_contract.py's history
        # comment, 2026-08-14 conversion-census-row mechanical_part/judgment_part
        # string -> object narrow, one MAJOR) — this is the second of the two places.
        assert bundle["version"] == "8.0.0"

    def test_no_external_ref_values_anywhere_in_bundle(self, bundle):
        # Regression for cross-repo/inbox/2026-08-03-doe-claude-em-artifact-contract-
        # external-ref-survives-bundling.md — assert over the WHOLE serialized bundle
        # (not just $defs.plan) so a future second such ref trips this too, not only
        # the one known offender.
        import re

        serialized = json.dumps(bundle)
        for match in re.finditer(r'"\$ref"\s*:\s*"(https?://[^"]*)"', serialized):
            pytest.fail(f'external $ref value survived bundling: {match.group(1)!r}')

    def test_plan_tasks_ref_rewritten_to_intra_bundle_pointer(self, bundle):
        assert (
            bundle["$defs"]["plan"]["properties"]["tasks"]["items"]["$ref"]
            == "#/$defs/plan-tasks"
        )

    def test_bundle_id_unchanged_by_ref_rewrite(self, bundle):
        # Regression guard for "rewrite only $ref values, never $id" — the bundle's
        # own top-level $id must survive byte-identical.
        assert bundle["$id"] == "https://coordinator.local/artifact-shape-contract.schema.json"

    def test_plan_tasks_ref_resolves_for_nonempty_tasks_array(self, bundle):
        # The actual defect: a stock Draft202012Validator raised Unresolvable for a
        # plan record with a non-empty `tasks:` array, because jsonschema resolves
        # `items` lazily (so `{"tasks": []}` passed and masked the dangling ref).
        jsonschema = pytest.importorskip("jsonschema")

        # Validate against the whole bundle so $ref: "#/$defs/plan-tasks" resolves
        # relative to the bundle root, not to $defs.plan alone (plan has no local
        # $defs of its own — the pointer is bundle-root-relative by construction).
        resolver_schema = dict(bundle)
        resolver_schema["$ref"] = "#/$defs/plan"
        validator = jsonschema.Draft202012Validator(resolver_schema)
        task_row = {
            row_name: {"type": "string"}
            for row_name in bundle["$defs"]["plan-tasks"].get("properties", {})
        }
        # Errors are irrelevant here — only that resolution itself does not raise
        # jsonschema.exceptions.RefResolutionError / referencing.exceptions.Unresolvable.
        list(validator.iter_errors({"tasks": [task_row]}))

    def test_source_memo_union_shape(self, bundle):
        plan_def = bundle["$defs"]["plan"]
        source_memo = plan_def["properties"]["source_memo"]
        assert source_memo["anyOf"] == [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]

    @pytest.mark.parametrize(
        "def_name", ["improvement-queue", "bug-backlog", "debt-backlog", "lessons-outbox"]
    )
    def test_queue_defs_present_with_applies_to(self, bundle, def_name):
        d = bundle["$defs"][def_name]
        assert "x-coordinator-applies_to" in d

    def test_no_emit_note_sentinels(self, bundle):
        occurrences = json.dumps(bundle).count("[emit-note]")
        assert occurrences == 0

    def test_routing_strategy_pin(self, bundle):
        assert bundle["routing_strategy"] == "most-specific-glob-wins"

    def test_review_integration_record_permissive(self, bundle):
        d = bundle["$defs"]["review-integration-record"]
        assert d["x-coordinator-applies_to"] == "state/review-trail/*-integration.json"
        assert "required" not in d
        assert d.get("additionalProperties") is not False

    def test_review_integration_record_relaxed_properties(self, bundle):
        d = bundle["$defs"]["review-integration-record"]
        assert "type" not in d["properties"]["rationale"]
        assert "type" not in d["properties"]["findings_received"]
        assert isinstance(d["properties"]["artifact"].get("anyOf"), list)

    @pytest.mark.parametrize(
        "field", ["deliverable_id", "initiative", "caption", "status_reason", "owner"]
    )
    def test_handoff_deliverable_spine_fields_nullable(self, bundle, field):
        prop = bundle["$defs"]["handoff"]["properties"][field]
        assert len(prop["anyOf"]) == 2

    @pytest.mark.parametrize(
        "field",
        ["deliverable_id", "plan_id", "initiative", "caption", "status_reason", "owner"],
    )
    def test_plan_deliverable_spine_fields_nullable(self, bundle, field):
        prop = bundle["$defs"]["plan"]["properties"][field]
        assert len(prop["anyOf"]) == 2

    @pytest.mark.parametrize("field", ["deliverable_id", "initiative"])
    def test_completion_entry_deliverable_spine_fields_nullable(self, bundle, field):
        prop = bundle["$defs"]["completion-entry"]["properties"][field]
        assert len(prop["anyOf"]) == 2

    def test_initiative_def_shape(self, bundle):
        d = bundle["$defs"]["initiative"]
        assert d["x-coordinator-applies_to"] == "state/initiatives/*.yaml"
        assert sorted(d["required"]) == ["id", "label", "status"]

    def test_provenance_envelope_shape(self, bundle):
        d = bundle["$defs"]["ProvenanceEnvelope"]
        assert sorted(d["required"]) == [
            "derivation",
            "entity_anchor",
            "observed_at",
            "path",
            "ref",
            "repo",
            "source_kind",
        ]
        assert "x-coordinator-applies_to" not in d
        assert d["additionalProperties"] is False

    def test_provenance_envelope_ref_anyof(self, bundle):
        ref = bundle["$defs"]["ProvenanceEnvelope"]["properties"]["ref"]
        assert len(ref["anyOf"]) == 2
        assert ref["anyOf"][0]["type"] == "object"
        assert ref["anyOf"][0]["additionalProperties"] is False
        assert sorted(ref["anyOf"][0]["required"]) == ["branch", "sha"]
        assert ref["anyOf"][1]["type"] == "null"

    def test_provenance_envelope_not_counted_in_schema_count(self, bundle):
        # schema_count reflects only schemas/*.yaml|*.schema.json artifact types — the
        # exact number drifts as schemas are added; this locks the STRUCTURAL invariant
        # (injected/hoisted non-artifact defs excluded) rather than a specific magic count,
        # since a magic count pin would go stale independently of any behavior change here.
        # $defs holds three key classes:
        #   - artifact types  — emitted by schema_to_json_schema, ALWAYS carry a `$schema` key
        #                       (plan, strategic-self-description); COUNTED.
        #   - SUB_SHAPES      — injected (ProvenanceEnvelope); NOT counted (excluded explicitly).
        #   - hoisted defs    — source-schema `$defs` hoisted to the flat bundle root (contract
        #                       v1.18.0: repoIdentityTuple, provenance, section, …); raw sub-schemas
        #                       with NO `$schema` wrapper, so the `$schema` filter excludes them.
        # Naming convention is NOT a safe discriminator — two hoisted defs (`provenance`, `section`)
        # are all-lowercase like artifact types; the `$schema`-presence structural test is exact.
        artifact_type_defs = {
            k
            for k, v in bundle["$defs"].items()
            if isinstance(v, dict) and v.get("$schema") and k not in SUB_SHAPES
        }
        assert bundle["schema_count"] == len(artifact_type_defs)

    def test_allof_four_conditionals(self, bundle):
        all_of = bundle["$defs"]["ProvenanceEnvelope"]["allOf"]
        assert len(all_of) == 4
        assert sorted(all_of[0]["if"]["properties"]["source_kind"]["enum"]) == [
            "git_commit",
            "github_graphql",
            "github_rest",
        ]
        assert all_of[0]["then"]["properties"]["ref"] == {"not": {"type": "null"}}
        assert sorted(all_of[1]["if"]["properties"]["source_kind"]["enum"]) == [
            "code_comparison",
            "coordinator_artifact",
            "local_fs",
            "sec_edgar",
            "transcript_summary",
        ]
        assert all_of[1]["then"]["properties"]["ref"] == {"type": "null"}
