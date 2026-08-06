"""
Tests for coordinator_core.contract.emit_memo_schema — the generate-from-SSOT
memo schema emitter (Decision-0, plan
docs/plans/2026-07-24-cross-repo-memo-ownership-and-redesign.md § C5).

Five invariants under test:
  (a) Lenient-receiver invariant preserved — a foreign memo missing a
      base-required field still validates cross-field-only (memos are
      foreign-authored; a sender's field slip must never block a legitimate
      receiver transition).
  (b) Drift/sync gate stays green against the generated shapes — the emitted
      files' query types are recognised by the natively-derived registry map.
  (c) Regression at claude-klabauter's own auto-load dir: match_schema with a memo
      `kind` present does NOT resolve to a shape-validating memo schema at
      `frontmatter/schemas/` — this dir is structurally unreachable by the
      emitted files (see test_refuses_to_write_into_auto_load_dir), so this
      class alone cannot catch a `_byKind`-arming field; see (d).
  (d) Regression at the REAL delivery-dir shape (Review: code-reviewer,
      Finding 1/2): `load_schemas()` over `emit_schemas()`'s own output
      directory — the shape actually delivered to the shared `schemas_dir`
      — must register NO memo kind into `_byKind`. This is the test the
      CRITICAL invariant in emit_memo_schema's module docstring needs.
  (e) Golden-output drift guard (Review: code-reviewer, Finding 3): the
      committed `.schema.json` files must match a fresh `emit_schemas()`
      call byte-for-byte, so an un-regenerated commit fails CI.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.contract.emit_memo_schema import (
    MEMO_SCHEMA_BUMP_CLASS,
    MEMO_SCHEMA_BUMP_NOTE,
    MEMO_SCHEMA_VERSION,
    emit_schemas,
)
from coordinator_core.frontmatter.schema_validate import (
    load_schemas,
    match_schema,
    validate_memo_cross_fields,
)
from coordinator_core.ops import verify_schema_registry_sync as vsrs

_CLAUDE_KLABAUTER_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "frontmatter" / "schemas"


class TestEmitSchemas:
    def test_emits_both_entities(self, tmp_path: Path) -> None:
        emitted = emit_schemas(out_dir=tmp_path)
        assert set(emitted.keys()) == {"cross-repo-memo", "archived-memo"}
        assert (tmp_path / "cross-repo-memo.schema.json").is_file()
        assert (tmp_path / "archived-memo.schema.json").is_file()

    def test_required_excludes_kind_and_summary(self, tmp_path: Path) -> None:
        """DEC-1: kind/summary required-ness is a send-time gate, never a
        schema `required` tightening."""
        emitted = emit_schemas(out_dir=tmp_path)
        memo_schema = emitted["cross-repo-memo"]
        assert "kind" not in memo_schema["required"]
        assert "summary" not in memo_schema["required"]
        assert "kind" in memo_schema["properties"]
        assert "summary" in memo_schema["properties"]

    def test_version_stamped(self, tmp_path: Path) -> None:
        emitted = emit_schemas(out_dir=tmp_path)
        for schema in emitted.values():
            assert schema["x-schema-version"] == MEMO_SCHEMA_VERSION
            assert schema["x-generated-by"] == (
                "coordinator_core.contract.emit_memo_schema.emit_schemas"
            )

    def test_bump_class_and_note_stamped(self, tmp_path: Path) -> None:
        """example-doctrine-repo bump-class annotation (memo
        2026-07-27-example-doctrine-repo-em-bump-class-shipped-and-a-correction.md):
        x-bump-class/x-bump-note sit immediately alongside x-schema-version
        on both entities so a future emitter edit cannot silently drop
        them and re-open the vendored-schema drift commit 5140d176 closed."""
        emitted = emit_schemas(out_dir=tmp_path)
        for schema in emitted.values():
            assert schema["x-bump-class"] == MEMO_SCHEMA_BUMP_CLASS == "nested-field-additive"
            assert schema["x-bump-note"] == MEMO_SCHEMA_BUMP_NOTE

    def test_refuses_to_write_into_auto_load_dir(self) -> None:
        """CRITICAL invariant: emitting into the load_schemas() auto-load dir
        would arm _byKind full-shape validation against foreign memos."""
        with pytest.raises(RuntimeError, match="auto-load"):
            emit_schemas(out_dir=_CLAUDE_KLABAUTER_SCHEMAS_DIR)

    def test_generated_files_not_present_in_auto_load_dir(self) -> None:
        """The real (non-tmp) frontmatter/schemas dir must never carry either
        generated memo schema on disk — this is what closes the glob path."""
        names = {p.name for p in _CLAUDE_KLABAUTER_SCHEMAS_DIR.glob("*.schema.json")}
        assert "cross-repo-memo.schema.json" not in names
        assert "archived-memo.schema.json" not in names


class TestLenientReceiverInvariantPreserved:
    """(a) A foreign memo missing a base-required field (per the generated
    schema's `required` array) still validates cross-field-only — the
    generated schema exists as a drift-reference artifact only and is never
    consulted by validate_memo_cross_fields."""

    def test_missing_title_does_not_fail_cross_field_validation(self) -> None:
        fm = {
            # "title" deliberately omitted — required per the generated
            # cross-repo-memo.schema.json, but cross-field validation never
            # checks base-required fields.
            "from": "some-foreign-repo",
            "to": "claude-klabauter",
            "created": "2026-07-24",
            "status": "open",
            "delivery_mode": "receiver-repo",
            "kind": "ask",
        }
        errors = validate_memo_cross_fields(fm)
        assert not any(e.get("field") == "title" for e in errors)

    def test_foreign_memo_missing_several_required_fields_still_cross_field_only(self) -> None:
        # Only "created" present — from/to/status/delivery_mode/title all
        # missing. Base-required validation would reject this outright;
        # cross-field-only validation must not.
        fm = {"created": "2026-07-24"}
        errors = validate_memo_cross_fields(fm)
        for err in errors:
            assert err.get("field") not in {"title", "from", "to", "status", "delivery_mode"}


class TestDriftSyncGateStaysGreen:
    """(b) The types the generated schemas derive are recognised by the
    natively-derived registry map, using the ACTUAL emitted shapes (not a
    hand-typed fixture) as the corpus under test."""

    def test_generated_schemas_recognised_by_registry(self, tmp_path: Path) -> None:
        emit_schemas(out_dir=tmp_path / "schemas")
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "query-records.js").write_text("", encoding="utf-8")

        exit_code, stdout_lines, stderr_lines = vsrs.run(tmp_path)

        assert exit_code == 0, f"stderr={stderr_lines!r} stdout={stdout_lines!r}"


class TestMatchSchemaRegressionBothPaths:
    """(c) match_schema(path, {"kind": "proposal"}, load_schemas(claude_klabauter_dir))
    must NOT resolve to a shape-validating memo schema — closes both the
    glob path (generated files absent from the auto-load dir) and the
    kind-first path (no _byKind registration for any memo kind)."""

    @pytest.mark.parametrize("kind_value", ["ask", "consult", "fyi", "proposal"])
    def test_kind_first_path_does_not_resolve_memo_schema(self, kind_value: str) -> None:
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        result = match_schema(
            "cross-repo/inbox/2026-07-24-foreign-repo-topic.md",
            {"kind": kind_value},
            schemas,
        )
        if result is not None:
            assert result["schemaName"] not in ("cross-repo-memo", "archived-memo")

    def test_glob_path_does_not_resolve_memo_schema(self) -> None:
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        result = match_schema(
            "cross-repo/inbox/2026-07-24-foreign-repo-topic.md",
            None,
            schemas,
        )
        if result is not None:
            assert result["schemaName"] not in ("cross-repo-memo", "archived-memo")

    def test_archived_glob_path_does_not_resolve_memo_schema(self) -> None:
        schemas = load_schemas(_CLAUDE_KLABAUTER_SCHEMAS_DIR)
        result = match_schema(
            "cross-repo/archive/2026-07-24-foreign-repo-topic.md",
            {"kind": "fyi"},
            schemas,
        )
        if result is not None:
            assert result["schemaName"] not in ("cross-repo-memo", "archived-memo")


class TestByKindNeverArmedAtRealDeliveryShape:
    """(d) Review: code-reviewer — Finding 1/2 (P1/P2). The prior
    TestMatchSchemaRegressionBothPaths class only loads _CLAUDE_KLABAUTER_SCHEMAS_DIR,
    a directory `emit_schemas()` structurally refuses to write into (see
    test_refuses_to_write_into_auto_load_dir) — so it can never catch a
    `_byKind`-arming field on either emitted schema. This class instead
    loads `load_schemas()` over `emit_schemas()`'s OWN output directory —
    the actual shape delivered to the shared `schemas_dir` per
    verify_schema_registry_sync.py's "DOWNSTREAM CONSUMED ARTIFACT" framing
    — and asserts neither emitted file registers ANY memo kind into
    `_byKind`. This is the test the CRITICAL invariant in
    emit_memo_schema's module docstring actually needs."""

    @pytest.mark.parametrize(
        "kind_value", ["ask", "consult", "fyi", "proposal", "memo"]
    )
    def test_no_byKind_registration_at_emitted_output_dir(
        self, tmp_path: Path, kind_value: str
    ) -> None:
        emit_schemas(out_dir=tmp_path)
        schemas = load_schemas(tmp_path)
        assert kind_value not in schemas["_byKind"], (
            f"kind {kind_value!r} resolved to schema "
            f"{schemas['_byKind'].get(kind_value)!r} via _byKind at the "
            "real emitted-output-dir shape — this arms full-shape "
            "validation against a foreign-authored memo before the glob "
            "path is ever consulted."
        )


class TestToRepoFieldPresentAndOptional:
    """example-doctrine-repo parity (2026-07-27): `to_repo` was a example-doctrine-repo-local vendored
    extension on both schemas that claude-klabauter's generation permanently
    re-drifted every regen. The field now comes home to the emitter, byte-
    identical to example-doctrine-repo's carried copy, on both entities — and stays optional
    (historical memos and archived memos predate it and must keep
    validating)."""

    def test_to_repo_present_on_cross_repo_memo_schema(self, tmp_path: Path) -> None:
        emitted = emit_schemas(out_dir=tmp_path)
        memo_schema = emitted["cross-repo-memo"]
        assert "to_repo" in memo_schema["properties"]
        assert memo_schema["properties"]["to_repo"]["type"] == "string"
        assert "to_repo" not in memo_schema["required"]

    def test_to_repo_present_on_archived_memo_schema(self, tmp_path: Path) -> None:
        emitted = emit_schemas(out_dir=tmp_path)
        archived_schema = emitted["archived-memo"]
        assert "to_repo" in archived_schema["properties"]
        assert archived_schema["properties"]["to_repo"]["type"] == "string"
        assert "to_repo" not in archived_schema["required"]


class TestGoldenOutputPinsCommittedJson:
    """(e) Review: code-reviewer — Finding 3 (P1). The whole "generate,
    don't vendor" rationale is unenforced unless something actually diffs
    the checked-in .schema.json files against a fresh emit_schemas() call —
    without this, a future edit to either builder function that lands
    without a re-run + re-commit of the JSON silently drifts the committed
    artifact from what the generator now produces."""

    def test_committed_json_matches_fresh_emission(self, tmp_path: Path) -> None:
        contract_dir = Path(__file__).resolve().parent
        emit_schemas(out_dir=tmp_path)
        for name in ("cross-repo-memo", "archived-memo"):
            fresh = (tmp_path / f"{name}.schema.json").read_text()
            committed = (contract_dir / f"{name}.schema.json").read_text()
            assert fresh == committed, (
                f"{name}.schema.json is stale — re-run "
                "`python3 -m coordinator_core.contract.emit_memo_schema` "
                "and re-commit the emitted JSON."
            )
