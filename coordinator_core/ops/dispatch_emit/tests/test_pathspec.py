"""
Tests for coordinator_core.ops.dispatch_emit.pathspec.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C3.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.pathspec import (
    NoTestTargetError,
    NoWritesDeclaredError,
    commit_pathspec,
    is_concrete_surface,
    terminal_test_scope,
)
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _wave_row(id_, writes, surface="dispatch_emit"):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface=surface,
        writes=writes,
        reads=[],
        depends_on=[],
    )


# ---------------------------------------------------------------------------
# commit_pathspec — AC3, AC4
# ---------------------------------------------------------------------------


def test_commit_pathspec_derives_from_declared_writes():
    wave = [
        _wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
        _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
    ]
    assert commit_pathspec(wave) == [
        "coordinator_core/ops/dispatch_emit/spine_read.py",
        "coordinator_core/ops/dispatch_emit/wave_map.py",
    ]


def test_commit_pathspec_dedupes_overlapping_declared_writes():
    wave = [
        _wave_row("C1", ["a.py", "shared.py"]),
        _wave_row("C2", ["shared.py", "b.py"]),
    ]
    assert commit_pathspec(wave) == ["a.py", "shared.py", "b.py"]


def test_commit_pathspec_falls_back_to_concrete_surface():
    # A wave where at least one row declares writes: is not refused; a
    # sibling row with UNDECLARED writes and a concrete surface still
    # contributes via the surface fallback.
    wave = [
        _wave_row("C1", ["a.py"]),
        _wave_row("C2", UNDECLARED, surface="coordinator_core/ops/dispatch_emit/pathspec.py"),
    ]
    assert commit_pathspec(wave) == [
        "a.py",
        "coordinator_core/ops/dispatch_emit/pathspec.py",
    ]


def test_commit_pathspec_ignores_non_concrete_surface_fallback():
    # No row declares writes:, so this hits the AC4 refusal rather than
    # silently falling back to the subsystem-named surface.
    wave = [_wave_row("C1", UNDECLARED, surface="dispatch_emit")]
    with pytest.raises(NoWritesDeclaredError, match="C1"):
        commit_pathspec(wave)


def test_commit_pathspec_refuses_when_no_row_declares_writes_naming_rows():
    wave = [_wave_row("C1", UNDECLARED), _wave_row("C2", UNDECLARED)]
    with pytest.raises(NoWritesDeclaredError) as excinfo:
        commit_pathspec(wave)
    assert "'C1'" in str(excinfo.value)
    assert "'C2'" in str(excinfo.value)


def test_commit_pathspec_does_not_refuse_when_at_least_one_row_declares_writes():
    wave = [_wave_row("C1", ["a.py"]), _wave_row("C2", UNDECLARED, surface="dispatch_emit")]
    # C2 contributes nothing (undeclared writes, non-concrete surface), but
    # the wave as a whole is not refused since C1 declared writes:.
    assert commit_pathspec(wave) == ["a.py"]


def test_commit_pathspec_refuses_when_every_row_declares_empty_writes_naming_rows():
    # writes: [] is an explicit declaration, distinct from UNDECLARED --
    # the AC4 "no row declares writes:" check does not fire. But zero
    # declared paths must still refuse rather than silently emit an empty
    # commit-phase pathspec (Review: code-reviewer wsc-B finding).
    wave = [_wave_row("C1", []), _wave_row("C2", [])]
    with pytest.raises(NoWritesDeclaredError) as excinfo:
        commit_pathspec(wave)
    assert "'C1'" in str(excinfo.value)
    assert "'C2'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# is_concrete_surface — the concreteness predicate
# ---------------------------------------------------------------------------


def test_is_concrete_surface_true_for_suffixed_path():
    assert is_concrete_surface("coordinator_core/ops/dispatch_emit/pathspec.py") is True


def test_is_concrete_surface_false_for_trailing_slash():
    assert is_concrete_surface("coordinator_core/ops/dispatch_emit/") is False


def test_is_concrete_surface_false_for_bare_package_name():
    assert is_concrete_surface("dispatch_emit") is False


def test_is_concrete_surface_false_for_no_suffix_no_matching_file():
    assert is_concrete_surface("coordinator_core/ops/dispatch_emit/nonexistent_module") is False


def test_is_concrete_surface_false_for_empty_string():
    assert is_concrete_surface("") is False


# ---------------------------------------------------------------------------
# terminal_test_scope — AC9, AC10, AC16
# ---------------------------------------------------------------------------


def test_terminal_test_scope_maps_written_paths_to_test_targets():
    waves = [
        [_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])],
        [_wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"])],
    ]
    scope = terminal_test_scope(waves)
    assert scope == [
        "coordinator_core/ops/dispatch_emit/tests/test_spine_read.py",
        "coordinator_core/ops/dispatch_emit/tests/test_wave_map.py",
    ]


def test_terminal_test_scope_refuses_whole_spine_no_writes_declared_naming_rows():
    waves = [[_wave_row("C1", UNDECLARED)], [_wave_row("C2", UNDECLARED)]]
    with pytest.raises(NoWritesDeclaredError) as excinfo:
        terminal_test_scope(waves)
    assert "'C1'" in str(excinfo.value)
    assert "'C2'" in str(excinfo.value)


def test_terminal_test_scope_refuses_when_every_written_path_is_doc_only():
    # Declares writes: (passes AC10's literal wording) but every path is a
    # doc, so no test target exists -- must refuse (AC16), not report an
    # empty scope as green.
    waves = [
        [_wave_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])],
        [_wave_row("C2", ["coordinator/bin/coordinator-doc-new"])],
    ]
    with pytest.raises(NoTestTargetError) as excinfo:
        terminal_test_scope(waves)
    assert "CONTRACT.md" in str(excinfo.value)
    assert "coordinator-doc-new" in str(excinfo.value)


def test_terminal_test_scope_drops_doc_paths_but_keeps_mapped_ones():
    waves = [
        [
            _wave_row(
                "C1",
                [
                    "coordinator_core/ops/dispatch_emit/spine_read.py",
                    "coordinator_core/subagent_sandbox/CONTRACT.md",
                ],
            )
        ],
    ]
    scope = terminal_test_scope(waves)
    assert scope == ["coordinator_core/ops/dispatch_emit/tests/test_spine_read.py"]


def test_terminal_test_scope_refuses_when_every_row_declares_empty_writes():
    # Analogous to the commit_pathspec case above: writes: [] declared on
    # every row across the whole spine still yields zero written paths,
    # which must refuse rather than emit an empty terminal test scope.
    waves = [[_wave_row("C1", [])], [_wave_row("C2", [])]]
    with pytest.raises(NoTestTargetError):
        terminal_test_scope(waves)


def test_terminal_test_scope_is_not_the_same_union_as_commit_pathspec():
    wave = [
        _wave_row(
            "C1",
            [
                "coordinator_core/ops/dispatch_emit/spine_read.py",
                "coordinator_core/subagent_sandbox/CONTRACT.md",
            ],
        )
    ]
    pathspec = commit_pathspec(wave)
    scope = terminal_test_scope([wave])
    assert "coordinator_core/subagent_sandbox/CONTRACT.md" in pathspec
    assert "coordinator_core/subagent_sandbox/CONTRACT.md" not in scope


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
