"""
coordinator_core.install.tests.test_write_surface

Behavioural tests for the write-surface declaration protocol
(coordinator_core.install.write_surface). Covers the four design
requirements from the spec as behaviour, not shape assertions.

Spec backlink: pln-writer-declared-write-surface-49d3bd,
chunk C1
"""

from __future__ import annotations

from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    ValidationError,
    WRITE_SURFACE_KINDS,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
    validate,
)
from coordinator_core.ops.write_surface_manifest import _flatten_declaration


def test_all_eight_kinds_present_and_exact() -> None:
    assert WRITE_SURFACE_KINDS == (
        "git-config-key",
        "machine-local-key",
        "os-env-var",
        "file-path",
        "rc-block",
        "hook-gate-region",
        "structured-file-key",
        "line-membership",
    )


def test_static_clause_round_trips_without_a_shaped_clause() -> None:
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
    assert validate(decl) == ()
    clause = decl.clauses[0]
    assert isinstance(clause, StaticClause)
    assert [e.key for e in clause.entries] == ["gc.autoDetach", "core.checkStat"]


def test_shaped_clause_round_trips_without_a_literal_key_list() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="register-discovered-repos",
        source_module="coordinator_core.install.substrate",
        clauses=(
            ShapedClause(
                discovered_by="discover_working_repos",
                entry_template=WriteSurfaceEntry(
                    kind="machine-local-key",
                    key="repos.<derived-key>",
                ),
            ),
        ),
    )
    assert validate(decl) == ()
    clause = decl.clauses[0]
    assert isinstance(clause, ShapedClause)
    assert clause.discovered_by == "discover_working_repos"
    assert clause.entry_template.key == "repos.<derived-key>"
    # A shaped clause never carries an entries list — this is the round
    # trip proof that it isn't secretly the static form in disguise.
    assert not hasattr(clause, "entries")


def test_declared_empty_and_undeclared_are_distinguishable() -> None:
    declared_empty = WriteSurfaceDeclaration(
        writer_id="noop-writer",
        source_module="coordinator_core.install.noop",
        clauses=(),
    )
    assert validate(declared_empty) == ()
    assert declared_empty.clauses == ()

    registry: dict[str, WriteSurfaceDeclaration] = {
        "noop-writer": declared_empty,
    }
    assert "noop-writer" in registry
    assert registry["noop-writer"].clauses == ()
    assert "some-other-writer" not in registry


def test_writer_may_carry_multiple_shape_clauses() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="install-substrate",
        source_module="coordinator_core.install.substrate",
        clauses=(
            ShapedClause(
                discovered_by="_percolation_and_path_steps",
                entry_template=WriteSurfaceEntry(kind="os-env-var", key="PATH"),
            ),
            ShapedClause(
                discovered_by="_windows_health_steps",
                entry_template=WriteSurfaceEntry(kind="os-env-var", key="PATH"),
            ),
            StaticClause(
                entries=(
                    WriteSurfaceEntry(
                        kind="file-path",
                        path="appx-stub",
                        effect="delete",
                        reason="consent-gated removal",
                    ),
                ),
                effect="delete",
            ),
            ShapedClause(
                discovered_by="_derive_agent_helper_target_map",
                entry_template=WriteSurfaceEntry(kind="file-path", path="<forwarder>.py"),
            ),
        ),
    )
    assert validate(decl) == ()
    assert len(decl.clauses) == 4


def test_absent_end_marker_validates_clean() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="legacy-marker-writer",
        source_module="coordinator_core.install.some_writer",
        clauses=(
            StaticClause(
                entries=(
                    WriteSurfaceEntry(
                        kind="hook-gate-region",
                        path="some/hook/file",
                        begin_marker="coordinator:some-gate:begin",
                        end_marker="absent-on-legacy-installs",
                    ),
                ),
            ),
        ),
    )
    assert validate(decl) == ()


def test_delete_effect_is_a_first_class_discriminator() -> None:
    entry = WriteSurfaceEntry(kind="file-path", path="appx-stub", effect="delete")
    assert entry.effect == "delete"
    decl = WriteSurfaceDeclaration(
        writer_id="appx-stub-remover",
        source_module="coordinator_core.install.substrate",
        clauses=(StaticClause(entries=(entry,), effect="delete"),),
    )
    assert validate(decl) == ()


def test_unrecognized_kind_produces_structured_error_not_a_raise() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="bad-writer",
        source_module="coordinator_core.install.bad",
        clauses=(
            StaticClause(
                entries=(WriteSurfaceEntry(kind="not-a-real-kind", key="x"),),
            ),
        ),
    )
    errors = validate(decl)
    assert len(errors) == 1
    assert isinstance(errors[0], ValidationError)
    assert "not-a-real-kind" in errors[0].message
    assert errors[0].writer_id == "bad-writer"


def test_unset_group_does_not_change_emitted_manifest_entry_keys() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="grouped-writer",
        source_module="coordinator_core.install.grouped",
        clauses=(
            StaticClause(
                entries=(
                    WriteSurfaceEntry(
                        kind="git-config-key",
                        key="gc.autoDetach",
                        unset_group="gc-group",
                    ),
                ),
            ),
        ),
    )
    flattened = _flatten_declaration(decl)
    assert len(flattened) == 1
    assert set(flattened[0].keys()) == {
        "status",
        "writer_id",
        "source_module",
        "form",
        "kind",
        "key",
        "path",
        "begin_marker",
        "end_marker",
        "effect",
        "reason",
        "discovered_by",
        "discovered_by_normalized",
        "validation_errors",
    }
    assert "unset_group" not in flattened[0]


def test_shaped_clause_missing_discovered_by_is_reported() -> None:
    decl = WriteSurfaceDeclaration(
        writer_id="broken-shaped-writer",
        source_module="coordinator_core.install.broken",
        clauses=(
            ShapedClause(
                discovered_by="",
                entry_template=WriteSurfaceEntry(kind="file-path", path="x"),
            ),
        ),
    )
    errors = validate(decl)
    assert any("discovered_by" in e.message for e in errors)
