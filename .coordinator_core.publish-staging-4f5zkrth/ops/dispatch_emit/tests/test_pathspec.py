"""
Tests for coordinator_core.ops.dispatch_emit.pathspec.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C3.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.pathspec import (
    DirectoryShapedWriteError,
    NoTestTargetError,
    _map_written_path_to_test_target,
    NoWritesDeclaredError,
    commit_pathspec,
    is_concrete_surface,
    terminal_test_scope,
)
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
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


def test_commit_pathspec_warns_and_refuses_when_every_row_declares_empty_writes_naming_rows(caplog):
    # writes: [] is an explicit declaration, distinct from UNDECLARED -- the
    # AC4 "no row declares writes:" check does not fire (every row DID
    # declare). But every row is also zero-contributing, so the union is
    # empty -- an empty pathspec is never a legal return regardless of
    # which spelling produced it (module docstring's negative spec: "name
    # the rows, never emit an empty result"). This is refusal 2 in
    # commit_pathspec's docstring, distinct from the per-row warn-and-
    # continue case (staff review finding P0-1) which only applies when a
    # real-contributing sibling is present to carry the wave. Both the
    # warning AND the raise fire here.
    wave = [_wave_row("C1", []), _wave_row("C2", [])]
    with caplog.at_level("WARNING"):
        with pytest.raises(NoWritesDeclaredError) as excinfo:
            commit_pathspec(wave)
    assert "'C1'" in caplog.text
    assert "'C2'" in caplog.text
    assert "'C1'" in str(excinfo.value)
    assert "'C2'" in str(excinfo.value)


def test_commit_pathspec_warns_but_returns_real_paths_when_one_row_declares_empty_writes_sharing_a_wave(caplog):
    # Finding A4's original shape: C2 declares writes: [] (zero paths) but
    # shares its wave with C1, which contributes real paths. Pre-A4,
    # neither the declares_writes check (C2 IS declared, just empty) nor
    # the old whole-wave "if not paths" check (C1's path makes the total
    # non-empty) fired -- C2 vanished with NO signal at all, its dispatched
    # executor's real writes uncarried by this or any later wave's
    # pathspec. Per P0-1, the fix is a named warning that excludes ONLY the
    # zero-contributing row -- NOT a refusal that fails the whole wave for
    # C1's sake too. This is the regression P0-1 exists to prevent.
    wave = [_wave_row("C1", ["a.py"]), _wave_row("C2", [])]
    with caplog.at_level("WARNING"):
        assert commit_pathspec(wave) == ["a.py"]
    assert "'C2'" in caplog.text
    assert "'C1'" not in caplog.text


def test_commit_pathspec_warns_but_returns_surface_fallback_when_empty_writes_row_shares_a_wave(caplog):
    # Same A4 shape, but the OTHER row contributes via the surface fallback
    # rather than a declared writes: list -- confirms the per-row warning
    # fires regardless of how the sibling row contributes, and the sibling's
    # path still comes back.
    wave = [
        _wave_row("C1", UNDECLARED, surface="coordinator_core/ops/dispatch_emit/pathspec.py"),
        _wave_row("C2", []),
    ]
    with caplog.at_level("WARNING"):
        assert commit_pathspec(wave) == ["coordinator_core/ops/dispatch_emit/pathspec.py"]
    assert "'C2'" in caplog.text


def test_commit_pathspec_refuses_directory_shaped_write_naming_row_and_path():
    # A spine row's declared writes: is uncommittable if it names a
    # directory -- scoped-git-commit refuses directory pathspecs by
    # design. Left uncaught this surfaces only at runtime, after a
    # dispatched committer refuses and strands the wave's work; the row id
    # and offending path must both be named so the EM can see which row is
    # at fault, not just that the wave's union failed.
    wave = [_wave_row("C4", ["state/memo-outbox/sent/"])]
    with pytest.raises(DirectoryShapedWriteError) as excinfo:
        commit_pathspec(wave)
    assert "C4" in str(excinfo.value)
    assert "state/memo-outbox/sent/" in str(excinfo.value)


def test_commit_pathspec_normal_file_writes_are_unaffected():
    # Regression guard: a normal, non-directory-shaped writes: list is not
    # touched by the new directory-shape check.
    wave = [_wave_row("C1", ["a.py", "sub/b.py"])]
    assert commit_pathspec(wave) == ["a.py", "sub/b.py"]


def test_declared_paths_surface_fallback_still_behaves_as_before():
    # The surface:-fallback path (UNDECLARED writes) is untouched by the
    # new writes:-primary-path check -- is_concrete_surface's own
    # trailing-slash check already governed this path before this fix and
    # continues to.
    wave = [_wave_row("C1", UNDECLARED, surface="coordinator_core/ops/dispatch_emit/")]
    with pytest.raises(NoWritesDeclaredError, match="C1"):
        commit_pathspec(wave)


def test_commit_pathspec_ac4_refusal_still_fires_when_no_row_declares_writes_at_all():
    # Guard against over-correction: downgrading the per-row zero-
    # contribution case to a warning must not have touched the wave-level
    # AC4 refusal, which fires when NOT ONE row in the wave declares
    # writes: at all (every row UNDECLARED, no concrete surface fallback).
    wave = [_wave_row("C1", UNDECLARED), _wave_row("C2", UNDECLARED, surface="dispatch_emit")]
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
    #
    # This fixture previously paired CONTRACT.md with coordinator/bin/
    # coordinator-doc-new.py. That script is NOT uncovered -- coordinator/
    # tests/test_coordinator_doc_new.py is named for it -- and it only sat in
    # a doc-only fixture because the mapper probed the immediate parent alone
    # and could not reach this repo's own flat test directory. The fixture
    # encoded a limitation of the derivation rather than a property of the
    # paths; once the ancestor walk landed, it asserted a refusal that had
    # stopped being correct. Both paths named here are genuinely uncovered.
    waves = [
        [_wave_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])],
        [_wave_row("C2", ["docs/wiki/dispatch-emit.md"])],
    ]
    # Both paths are prose. There is no edit the plan author could make to
    # satisfy a test-target check on them, so this is a legitimately empty
    # scope rather than an authoring omission -- emit.compose_script omits
    # the terminal phase and narrates the omission.
    assert terminal_test_scope(waves) == []


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


def test_terminal_test_scope_resolves_a_data_fixture_to_its_driver_test(tmp_path):
    # A config-only row alone in its wave used to be unemittable: the mapper
    # returned None for every non-.py path, so terminal_test_scope refused a
    # surface that IS covered by a driver test named for the fixture.
    driver = tmp_path / "coordinator_core/install/tests/test_engine_root_conformance.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    waves = [[_wave_row("C3", ["coordinator_core/install/engine-root-conformance.json"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator_core/install/tests/test_engine_root_conformance.py"]


def test_terminal_test_scope_resolves_a_hyphenated_stem_to_an_importable_test_name(tmp_path):
    # A hyphen cannot appear in an importable test module name, so a verbatim
    # f"test_{stem}.py" derives a candidate that can never exist -- which would
    # leave the non-.py rungs inert for exactly the paths they exist to resolve.
    driver = tmp_path / "coordinator/bin/tests/test_compose_review_wave.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    waves = [[_wave_row("C1", ["coordinator/bin/compose-review-wave.py"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator/bin/tests/test_compose_review_wave.py"]


def test_terminal_test_scope_resolves_non_code_by_stem_not_by_proximity(tmp_path):
    # Negative spec: an uncovered non-.py path resolves through the stem
    # derivation ONLY. Neither a sibling test in the same tests/ directory nor
    # a test that cites the written path in an assertion message makes it
    # resolvable -- that looser cut resolved a wiki doc to an unrelated test.
    neighbour = tmp_path / "docs/wiki/tests/test_something_else.py"
    neighbour.parent.mkdir(parents=True)
    neighbour.write_text(
        'def test_x():\n    assert True, "docs/wiki/machine-load-norm.md"\n',
        encoding="utf-8",
    )
    waves = [[_wave_row("C1", ["docs/wiki/machine-load-norm.md"])]]
    # The point of these cases is that nothing resolves. That now reads as an
    # empty scope rather than a refusal (the doc is prose), which is a weaker
    # signal, so assert the mapper directly too.
    assert terminal_test_scope(waves, repo_root=tmp_path) == []
    assert (
        _map_written_path_to_test_target(
            "docs/wiki/machine-load-norm.md", repo_root=tmp_path
        )
        is None
    )


def test_terminal_test_scope_resolves_a_flat_test_directory_at_an_ancestor(tmp_path):
    # A repo keeping ONE flat test directory has no tests/ beside the written
    # path. Probing only the immediate parent resolved nothing for the whole
    # layout, which is what forced an outside caller to substitute this
    # module's private bindings rather than configure it.
    driver = tmp_path / "coordinator/tests/test_emit_dispatch_workflow.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    waves = [[_wave_row("C1", ["coordinator/bin/emit-dispatch-workflow.py"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator/tests/test_emit_dispatch_workflow.py"]


def test_terminal_test_scope_prefers_the_nearest_stem_named_test(tmp_path):
    # The ancestor walk is nearest-first: a co-located test wins over a
    # same-stem test further out, so widening the ladder cannot silently
    # redirect a repo that already resolved co-located.
    for rel in (
        "coordinator/bin/tests/test_emit_dispatch_workflow.py",
        "coordinator/tests/test_emit_dispatch_workflow.py",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    waves = [[_wave_row("C1", ["coordinator/bin/emit-dispatch-workflow.py"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator/bin/tests/test_emit_dispatch_workflow.py"]


def test_terminal_test_scope_maps_a_written_test_file_to_itself(tmp_path):
    # A row whose deliverable IS a new test knows its own target with
    # certainty. Deriving instead asks for test_test_<stem>.py, which can
    # never exist, and refuses a test-only row on the one surface it
    # definitionally covers. The file is deliberately NOT created on disk:
    # the row is writing it.
    waves = [[_wave_row("C1", ["coordinator_core/ops/tests/test_new_surface.py"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator_core/ops/tests/test_new_surface.py"]


def test_terminal_test_scope_resolves_a_test_this_spine_has_yet_to_write(tmp_path):
    # The ordinary shape of new work: a row writing a module together with the
    # test covering it. Judged against the tree alone that test does not exist
    # at emission time, so the module mapped to nothing and emission refused
    # precisely on work that carries its own coverage. The spine, not the
    # worktree, is authoritative about what will exist by the terminal phase.
    waves = [
        [
            _wave_row(
                "C1",
                [
                    "coordinator_core/ops/brand_new.py",
                    "coordinator_core/ops/tests/test_brand_new.py",
                ],
            )
        ]
    ]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator_core/ops/tests/test_brand_new.py"]


def test_terminal_test_scope_resolves_a_declared_test_written_by_a_later_row(tmp_path):
    # The declared union is whole-spine, not per-row: a module and the test
    # covering it are one deliverable even when split across two rows, and a
    # per-row union would resolve it only when one row declared both.
    waves = [
        [_wave_row("C1", ["coordinator_core/ops/brand_new.py"])],
        [_wave_row("C2", ["coordinator_core/ops/tests/test_brand_new.py"])],
    ]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator_core/ops/tests/test_brand_new.py"]


def test_declared_optimism_does_not_resolve_a_path_nobody_declares(tmp_path):
    # Optimism is bounded by the spine's own declaration. An undeclared,
    # non-existent candidate stays unresolved, so AC16's refusal survives the
    # widening rather than being quietly traded away for it.
    waves = [[_wave_row("C1", ["docs/wiki/machine-load-norm.md"])]]
    # The point of these cases is that nothing resolves. That now reads as an
    # empty scope rather than a refusal (the doc is prose), which is a weaker
    # signal, so assert the mapper directly too.
    assert terminal_test_scope(waves, repo_root=tmp_path) == []
    assert (
        _map_written_path_to_test_target(
            "docs/wiki/machine-load-norm.md", repo_root=tmp_path
        )
        is None
    )


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


# --- The AC16 discriminator: an authoring omission vs a fact about the surface ---
# Two repos hit the original all-or-nothing refusal independently (doe-claude-em
# 2026-08-18, project-rag-ue-addon-em 2026-08-20). These four pin the split that
# replaced it, including the mixed case that must still refuse.


def test_terminal_test_scope_refuses_an_uncovered_python_path():
    # The omission AC16 exists for: a row writes code and names no test.
    # The author CAN fix this, and a wave that proves nothing about the code
    # it wrote is exactly what must not emit.
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    with pytest.raises(NoTestTargetError) as excinfo:
        terminal_test_scope(waves)
    assert "nonexistent_module.py" in str(excinfo.value)
    assert "testable surfaces with no target" in str(excinfo.value)


def test_terminal_test_scope_refuses_a_wave_mixing_prose_with_an_uncovered_module():
    # Prose wave-mates do not excuse an uncovered .py. A wave is not "doc-only"
    # because most of it is docs -- one testable surface with no target is
    # still an omission, and the whole wave refuses.
    waves = [
        [
            _wave_row(
                "C1",
                [
                    "docs/wiki/dispatch-emit.md",
                    "coordinator_core/ops/dispatch_emit/nonexistent_module.py",
                ],
            )
        ]
    ]
    with pytest.raises(NoTestTargetError) as excinfo:
        terminal_test_scope(waves)
    assert "nonexistent_module.py" in str(excinfo.value)


def test_terminal_test_scope_is_empty_for_a_spine_that_writes_only_prose():
    # The project-rag-ue-addon repro: the plan's stated product deliverable
    # was a write-up chunk, which the original cut made unexecutable.
    waves = [
        [_wave_row("C6", ["state/audits/ue-cpp-embedding-ab/RESULTS-samples.md"])],
        [_wave_row("C11", ["state/audits/ue-cpp-embedding-ab/RESULTS.md"])],
    ]
    assert terminal_test_scope(waves) == []


def test_a_prose_spine_does_not_swallow_the_zero_contribution_refusal():
    # Regression: the widening keys on "every unmapped path is non-testable".
    # A spine whose rows all declare `writes: []` has NO unmapped path, which
    # vacuously satisfies that -- it must still refuse, never read as prose.
    waves = [[_wave_row("C1", [])], [_wave_row("C2", [])]]
    with pytest.raises(NoTestTargetError):
        terminal_test_scope(waves)
