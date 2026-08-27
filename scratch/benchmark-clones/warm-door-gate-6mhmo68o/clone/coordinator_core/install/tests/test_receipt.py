"""
coordinator_core.install.tests.test_receipt

Behavioural tests for the install receipt (coordinator_core.install.receipt)
— derivation of concrete on-this-machine facts from a writer's declared
write surface.

Spec backlink: pln-writer-declared-write-surface-49d3bd,
chunk C5
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.install.receipt import (
    RECEIPT_SCHEMA_VERSION,
    ClauseResolution,
    InstallReceipt,
    ReceiptEntry,
    UnresolvedShapedClauseError,
    build_receipt,
    derive_receipt_entries,
    load_receipt,
    persist_receipt,
)
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)


def test_static_clause_derives_directly_with_no_resolution() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="configure-git",
        source_module="coordinator_core.ops.configure_git",
        clauses=(
            StaticClause(
                entries=(
                    WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach"),
                    WriteSurfaceEntry(kind="git-config-key", key="core.checkStat"),
                ),
            ),
        ),
    )
    entries = derive_receipt_entries(decl)
    assert entries == (
        ReceiptEntry(writer_id="configure-git", kind="git-config-key", key="gc.autoDetach"),
        ReceiptEntry(writer_id="configure-git", kind="git-config-key", key="core.checkStat"),
    )


def test_shaped_clause_without_resolution_raises() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="register-discovered-repos",
        source_module="coordinator_core.install.substrate",
        clauses=(
            ShapedClause(
                discovered_by="discover_working_repos",
                entry_template=WriteSurfaceEntry(kind="machine-local-key", key="repos.<derived-key>"),
            ),
        ),
    )
    with pytest.raises(UnresolvedShapedClauseError):
        derive_receipt_entries(decl)


def test_shaped_clause_with_resolution_derives_the_pinned_down_entries() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="register-discovered-repos",
        source_module="coordinator_core.install.substrate",
        clauses=(
            ShapedClause(
                discovered_by="discover_working_repos",
                entry_template=WriteSurfaceEntry(kind="machine-local-key", key="repos.<derived-key>"),
            ),
        ),
    )
    resolutions = {
        0: ClauseResolution(
            entries=(
                WriteSurfaceEntry(kind="machine-local-key", key="repos.claude-klabauter"),
                WriteSurfaceEntry(kind="machine-local-key", key="repos.doe-claude"),
            ),
        ),
    }
    entries = derive_receipt_entries(decl, resolutions)
    assert entries == (
        ReceiptEntry(writer_id="register-discovered-repos", kind="machine-local-key", key="repos.claude-klabauter"),
        ReceiptEntry(writer_id="register-discovered-repos", kind="machine-local-key", key="repos.doe-claude"),
    )


def test_declared_empty_derives_to_empty_receipt() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="noop-writer",
        source_module="coordinator_core.install.noop",
        clauses=(),
    )
    assert derive_receipt_entries(decl) == ()


def test_mixed_static_and_shaped_clauses_one_writer() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="scaffold-structure",
        source_module="coordinator_core.install.scaffold_structure",
        clauses=(
            StaticClause(entries=(WriteSurfaceEntry(kind="file-path", path="state/.gitkeep"),)),
            ShapedClause(
                discovered_by="parse_manifest (template-backed file entries)",
                entry_template=WriteSurfaceEntry(kind="file-path", path="<manifest-declared-path>"),
            ),
        ),
    )
    resolutions = {
        1: ClauseResolution(entries=(WriteSurfaceEntry(kind="file-path", path="docs/wiki/foo.md"),)),
    }
    entries = derive_receipt_entries(decl, resolutions)
    assert entries == (
        ReceiptEntry(writer_id="scaffold-structure", kind="file-path", path="state/.gitkeep"),
        ReceiptEntry(writer_id="scaffold-structure", kind="file-path", path="docs/wiki/foo.md"),
    )


def test_resolution_supplied_for_a_static_clause_is_inert_not_an_error() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="configure-git",
        source_module="coordinator_core.ops.configure_git",
        clauses=(StaticClause(entries=(WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach"),)),),
    )
    resolutions = {0: ClauseResolution(entries=(WriteSurfaceEntry(kind="git-config-key", key="ignored"),))}
    entries = derive_receipt_entries(decl, resolutions)
    assert entries == (ReceiptEntry(writer_id="configure-git", kind="git-config-key", key="gc.autoDetach"),)


def test_delete_effect_propagates_from_entry_over_clause_default() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="appx-stub-remover",
        source_module="coordinator_core.install.substrate",
        clauses=(
            StaticClause(
                entries=(WriteSurfaceEntry(kind="file-path", path="appx-stub", effect="delete"),),
                effect="delete",
            ),
        ),
    )
    entries = derive_receipt_entries(decl)
    assert entries[0].effect == "delete"


def test_build_receipt_assembles_across_multiple_writers() -> None:
    decl_a = WriteSurfaceDeclaration(
        writer_id="configure-git",
        source_module="coordinator_core.ops.configure_git",
        clauses=(StaticClause(entries=(WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach"),)),),
    )
    decl_b = WriteSurfaceDeclaration(
        writer_id="register-discovered-repos",
        source_module="coordinator_core.install.substrate",
        clauses=(
            ShapedClause(
                discovered_by="discover_working_repos",
                entry_template=WriteSurfaceEntry(kind="machine-local-key", key="repos.<derived-key>"),
            ),
        ),
    )
    receipt = build_receipt(
        [
            (decl_a, None),
            (decl_b, {0: ClauseResolution(entries=(WriteSurfaceEntry(kind="machine-local-key", key="repos.foo"),))}),
        ]
    )
    assert isinstance(receipt, InstallReceipt)
    assert len(receipt.entries) == 2
    assert receipt.for_writer("configure-git") == (
        ReceiptEntry(writer_id="configure-git", kind="git-config-key", key="gc.autoDetach"),
    )
    assert receipt.for_kind("machine-local-key") == (
        ReceiptEntry(writer_id="register-discovered-repos", kind="machine-local-key", key="repos.foo"),
    )


def test_unrecognized_clause_type_raises_type_error() -> None:
    class _NotAClause:
        pass

    decl = WriteSurfaceDeclaration(
        writer_id="broken-writer",
        source_module="coordinator_core.install.broken",
        clauses=(_NotAClause(),),  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError):
        derive_receipt_entries(decl)


# ---------------------------------------------------------------------------
# Coverage — reported/unreported writer tracking (C2)
# ---------------------------------------------------------------------------


def test_build_receipt_records_unreported_writer_id_without_deriving() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="configure-git",
        source_module="coordinator_core.ops.configure_git",
        clauses=(StaticClause(entries=(WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach"),)),),
    )
    receipt = build_receipt(
        [(decl, None)],
        unreported_writer_ids=["ensure-venv", "wrapper-onto-path"],
    )
    assert receipt.reported("configure-git") is True
    assert receipt.reported("ensure-venv") is False
    assert receipt.reported("wrapper-onto-path") is False
    # A writer never mentioned at all is a distinct, honest "not asked" —
    # never conflated with "reported nothing" (declared-empty) or
    # "explicitly unreported". This is the negative spec's whole point.
    assert receipt.reported("register-discovered-repos") is None
    assert receipt.for_writer("ensure-venv") == ()


def test_unreported_writer_id_distinguishable_from_reported_empty_writer() -> None:
    empty_decl = WriteSurfaceDeclaration(
        writer_id="noop-writer",
        source_module="coordinator_core.install.noop",
        clauses=(),
    )
    receipt = build_receipt([(empty_decl, None)], unreported_writer_ids=["never-ran"])
    # Both writers have zero entries — but coverage tells them apart.
    assert receipt.for_writer("noop-writer") == ()
    assert receipt.for_writer("never-ran") == ()
    assert receipt.reported("noop-writer") is True
    assert receipt.reported("never-ran") is False


def test_build_receipt_rejects_writer_id_both_derived_and_marked_unreported() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="configure-git",
        source_module="coordinator_core.ops.configure_git",
        clauses=(StaticClause(entries=(WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach"),)),),
    )
    with pytest.raises(ValueError):
        build_receipt([(decl, None)], unreported_writer_ids=["configure-git"])


def test_unresolved_shaped_clause_still_raises_not_silently_marked_unreported() -> None:
    """The negative spec, restated as a test: `build_receipt` does NOT
    weaken `derive_receipt_entries`'s existing raise into a quiet
    unreported-marking fallback. A caller that hands in a declaration with
    an unresolved ShapedClause still gets the loud error — recording a
    writer as unreported is a SEPARATE call the caller must make itself
    via `unreported_writer_ids`, never an automatic rescue."""
    decl = WriteSurfaceDeclaration(
        writer_id="register-discovered-repos",
        source_module="coordinator_core.install.substrate",
        clauses=(
            ShapedClause(
                discovered_by="discover_working_repos",
                entry_template=WriteSurfaceEntry(kind="machine-local-key", key="repos.<derived-key>"),
            ),
        ),
    )
    with pytest.raises(UnresolvedShapedClauseError):
        build_receipt([(decl, None)])


def test_reported_writer_ids_default_empty_on_bare_construction() -> None:
    receipt = InstallReceipt()
    assert receipt.reported("anything") is None
    assert receipt.reported_writer_ids == frozenset()
    assert receipt.unreported_writer_ids == frozenset()


# ---------------------------------------------------------------------------
# Persistence — persist_receipt / load_receipt (C2)
# ---------------------------------------------------------------------------


def _sample_receipt() -> InstallReceipt:
    decl = WriteSurfaceDeclaration(
        writer_id="configure-git",
        source_module="coordinator_core.ops.configure_git",
        clauses=(
            StaticClause(
                entries=(
                    WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach"),
                    WriteSurfaceEntry(kind="file-path", path="state/.gitkeep", effect="delete"),
                ),
            ),
        ),
    )
    return build_receipt([(decl, None)], unreported_writer_ids=["never-ran"])


def test_persist_then_load_round_trips(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    receipt = _sample_receipt()

    written_path = persist_receipt(receipt, settings_home_override=tmp_path)
    assert written_path == tmp_path / "install-receipt.json"
    assert written_path.is_file()

    loaded = load_receipt(settings_home_override=tmp_path)
    assert loaded == receipt
    assert loaded.reported("configure-git") is True
    assert loaded.reported("never-ran") is False
    assert loaded.reported("some-other-writer") is None


def test_persist_writes_atomically_via_shared_primitive(tmp_path, monkeypatch) -> None:
    """Not a hand-rolled write-then-rename — no leftover tempfile after a
    successful persist (the `_shared.atomic_write_bytes` contract)."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    persist_receipt(_sample_receipt(), settings_home_override=tmp_path)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".atomic-write.")]
    assert leftovers == []


def test_persist_refused_when_machine_mutation_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    with pytest.raises(Exception):
        persist_receipt(_sample_receipt(), settings_home_override=tmp_path)
    assert not (tmp_path / "install-receipt.json").exists()


def test_load_receipt_returns_none_when_file_absent(tmp_path) -> None:
    assert load_receipt(settings_home_override=tmp_path) is None


def test_load_receipt_returns_none_on_malformed_json(tmp_path) -> None:
    (tmp_path / "install-receipt.json").write_text("{not valid json", encoding="utf-8")
    assert load_receipt(settings_home_override=tmp_path) is None


def test_load_receipt_returns_none_on_truncated_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    persist_receipt(_sample_receipt(), settings_home_override=tmp_path)
    target = tmp_path / "install-receipt.json"
    full = target.read_bytes()
    target.write_bytes(full[: len(full) // 2])
    assert load_receipt(settings_home_override=tmp_path) is None


def test_load_receipt_returns_none_on_unrecognized_schema_version(tmp_path) -> None:
    doc = {
        "schema_version": RECEIPT_SCHEMA_VERSION + 999,
        "entries": [],
        "reported_writer_ids": [],
        "unreported_writer_ids": [],
    }
    (tmp_path / "install-receipt.json").write_text(json.dumps(doc), encoding="utf-8")
    assert load_receipt(settings_home_override=tmp_path) is None


def test_load_receipt_returns_none_on_wrong_shape_that_does_not_round_trip(tmp_path) -> None:
    """Well-formed JSON, wrong document shape — a real corruption class
    distinct from "not JSON at all"."""
    doc = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "entries": "this should be a list, not a string",
        "reported_writer_ids": [],
        "unreported_writer_ids": [],
    }
    (tmp_path / "install-receipt.json").write_text(json.dumps(doc), encoding="utf-8")
    assert load_receipt(settings_home_override=tmp_path) is None


def test_load_receipt_returns_none_on_entry_missing_required_key(tmp_path) -> None:
    doc = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "entries": [{"kind": "git-config-key"}],  # missing writer_id
        "reported_writer_ids": [],
        "unreported_writer_ids": [],
    }
    (tmp_path / "install-receipt.json").write_text(json.dumps(doc), encoding="utf-8")
    assert load_receipt(settings_home_override=tmp_path) is None


def test_load_receipt_returns_none_when_settings_home_unresolvable(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    assert load_receipt() is None


def test_load_receipt_returns_none_when_writer_id_in_both_reported_and_unreported(tmp_path) -> None:
    """A malformed/tampered on-disk receipt with the same writer_id in both
    lists must degrade to None, not silently resolve reported() to True —
    build_receipt enforces this disjointness at construction, and the load
    path must enforce it too."""
    doc = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "entries": [],
        "reported_writer_ids": ["configure-git"],
        "unreported_writer_ids": ["configure-git"],
    }
    (tmp_path / "install-receipt.json").write_text(json.dumps(doc), encoding="utf-8")
    assert load_receipt(settings_home_override=tmp_path) is None


def test_load_receipt_returns_none_on_entry_with_non_string_path(tmp_path) -> None:
    """A corrupted receipt with e.g. an integer path must degrade to None
    (shape mismatch), not reconstruct a ReceiptEntry with a non-string
    path."""
    doc = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "entries": [
            {"writer_id": "configure-git", "kind": "file-path", "path": 12345}
        ],
        "reported_writer_ids": ["configure-git"],
        "unreported_writer_ids": [],
    }
    (tmp_path / "install-receipt.json").write_text(json.dumps(doc), encoding="utf-8")
    assert load_receipt(settings_home_override=tmp_path) is None


def test_persist_receipt_raises_receipt_persistence_error_when_settings_home_unresolvable(
    monkeypatch,
) -> None:
    """persist_receipt's own docstring and ReceiptPersistenceError's
    docstring both document an unresolvable settings-home as surfacing
    ReceiptPersistenceError — matching load_receipt's degrade contract on
    the write side. Mirrors
    test_load_receipt_returns_none_when_settings_home_unresolvable."""
    from coordinator_core.install.receipt import ReceiptPersistenceError

    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    with pytest.raises(ReceiptPersistenceError):
        persist_receipt(_sample_receipt())
