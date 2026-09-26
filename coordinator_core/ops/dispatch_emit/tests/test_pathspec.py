
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.pathspec import (
    DirectoryShapedWriteError,
    NoTestTargetError,
    _map_written_path_to_test_target,
    NoWritesDeclaredError,
    candidate_test_additions,
    commit_pathspec,
    commit_pathspec_or_none,
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
    # sibling row with UNDECLARED writes and a concrete surface still
    wave = [
        _wave_row("C1", ["a.py"]),
        _wave_row("C2", UNDECLARED, surface="coordinator_core/ops/dispatch_emit/pathspec.py"),
    ]
    assert commit_pathspec(wave) == [
        "a.py",
        "coordinator_core/ops/dispatch_emit/pathspec.py",
    ]


def test_commit_pathspec_ignores_non_concrete_surface_fallback():
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
    assert commit_pathspec(wave) == ["a.py"]


def test_commit_pathspec_warns_and_refuses_when_every_row_declares_empty_writes_naming_rows(caplog):
    # writes: [] is an explicit declaration, distinct from UNDECLARED -- the
    wave = [_wave_row("C1", []), _wave_row("C2", [])]
    with caplog.at_level("WARNING"):
        with pytest.raises(NoWritesDeclaredError) as excinfo:
            commit_pathspec(wave)
    assert "'C1'" in caplog.text
    assert "'C2'" in caplog.text
    assert "'C1'" in str(excinfo.value)
    assert "'C2'" in str(excinfo.value)


def test_commit_pathspec_warns_but_returns_real_paths_when_one_row_declares_empty_writes_sharing_a_wave(caplog):
    wave = [_wave_row("C1", ["a.py"]), _wave_row("C2", [])]
    with caplog.at_level("WARNING"):
        assert commit_pathspec(wave) == ["a.py"]
    assert "'C2'" in caplog.text
    assert "'C1'" not in caplog.text


def test_commit_pathspec_warns_but_returns_surface_fallback_when_empty_writes_row_shares_a_wave(caplog):
    wave = [
        _wave_row("C1", UNDECLARED, surface="coordinator_core/ops/dispatch_emit/pathspec.py"),
        _wave_row("C2", []),
    ]
    with caplog.at_level("WARNING"):
        assert commit_pathspec(wave) == ["coordinator_core/ops/dispatch_emit/pathspec.py"]
    assert "'C2'" in caplog.text


def test_commit_pathspec_refuses_directory_shaped_write_naming_row_and_path():
    wave = [_wave_row("C4", ["state/memo-outbox/sent/"])]
    with pytest.raises(DirectoryShapedWriteError) as excinfo:
        commit_pathspec(wave)
    assert "C4" in str(excinfo.value)
    assert "state/memo-outbox/sent/" in str(excinfo.value)


def test_commit_pathspec_normal_file_writes_are_unaffected():
    wave = [_wave_row("C1", ["a.py", "sub/b.py"])]
    assert commit_pathspec(wave) == ["a.py", "sub/b.py"]


def test_declared_paths_surface_fallback_still_behaves_as_before():
    # The surface:-fallback path (UNDECLARED writes) is untouched by the
    wave = [_wave_row("C1", UNDECLARED, surface="coordinator_core/ops/dispatch_emit/")]
    with pytest.raises(NoWritesDeclaredError, match="C1"):
        commit_pathspec(wave)


def test_commit_pathspec_ac4_refusal_still_fires_when_no_row_declares_writes_at_all():
    # writes: at all (every row UNDECLARED, no concrete surface fallback).
    wave = [_wave_row("C1", UNDECLARED), _wave_row("C2", UNDECLARED, surface="dispatch_emit")]
    with pytest.raises(NoWritesDeclaredError) as excinfo:
        commit_pathspec(wave)
    assert "'C1'" in str(excinfo.value)
    assert "'C2'" in str(excinfo.value)


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
    # This fixture previously paired CONTRACT.md with coordinator/bin/
    waves = [
        [_wave_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])],
        [_wave_row("C2", ["docs/wiki/dispatch-emit.md"])],
    ]
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
    waves = [[_wave_row("C1", [])], [_wave_row("C2", [])]]
    with pytest.raises(NoTestTargetError):
        terminal_test_scope(waves)


def test_terminal_test_scope_resolves_a_data_fixture_to_its_driver_test(tmp_path):
    driver = tmp_path / "coordinator_core/install/tests/test_engine_root_conformance.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    waves = [[_wave_row("C3", ["coordinator_core/install/engine-root-conformance.json"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator_core/install/tests/test_engine_root_conformance.py"]


def test_terminal_test_scope_resolves_a_hyphenated_stem_to_an_importable_test_name(tmp_path):
    driver = tmp_path / "coordinator/bin/tests/test_compose_review_wave.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    waves = [[_wave_row("C1", ["coordinator/bin/compose-review-wave.py"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator/bin/tests/test_compose_review_wave.py"]


def test_terminal_test_scope_resolves_non_code_by_stem_not_by_proximity(tmp_path):
    neighbour = tmp_path / "docs/wiki/tests/test_something_else.py"
    neighbour.parent.mkdir(parents=True)
    neighbour.write_text(
        'def test_x():\n    assert True, "docs/wiki/machine-load-norm.md"\n',
        encoding="utf-8",
    )
    waves = [[_wave_row("C1", ["docs/wiki/machine-load-norm.md"])]]
    assert terminal_test_scope(waves, repo_root=tmp_path) == []
    assert (
        _map_written_path_to_test_target(
            "docs/wiki/machine-load-norm.md", repo_root=tmp_path
        )
        is None
    )


def test_terminal_test_scope_resolves_a_flat_test_directory_at_an_ancestor(tmp_path):
    driver = tmp_path / "coordinator/tests/test_emit_dispatch_workflow.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    waves = [[_wave_row("C1", ["coordinator/bin/emit-dispatch-workflow.py"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator/tests/test_emit_dispatch_workflow.py"]


def test_terminal_test_scope_prefers_the_nearest_stem_named_test(tmp_path):
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
    waves = [[_wave_row("C1", ["coordinator_core/ops/tests/test_new_surface.py"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator_core/ops/tests/test_new_surface.py"]


def test_terminal_test_scope_resolves_a_test_this_spine_has_yet_to_write(tmp_path):
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
    waves = [
        [_wave_row("C1", ["coordinator_core/ops/brand_new.py"])],
        [_wave_row("C2", ["coordinator_core/ops/tests/test_brand_new.py"])],
    ]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["coordinator_core/ops/tests/test_brand_new.py"]


def test_declared_optimism_does_not_resolve_a_path_nobody_declares(tmp_path):
    waves = [[_wave_row("C1", ["docs/wiki/machine-load-norm.md"])]]
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


def test_terminal_test_scope_refuses_an_uncovered_python_path():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    with pytest.raises(NoTestTargetError) as excinfo:
        terminal_test_scope(waves)
    assert "nonexistent_module.py" in str(excinfo.value)
    assert "testable surfaces with no target" in str(excinfo.value)


def test_terminal_test_scope_refuses_a_wave_mixing_prose_with_an_uncovered_module():
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
    waves = [
        [_wave_row("C6", ["state/audits/ue-cpp-embedding-ab/RESULTS-samples.md"])],
        [_wave_row("C11", ["state/audits/ue-cpp-embedding-ab/RESULTS.md"])],
    ]
    assert terminal_test_scope(waves) == []


def test_a_prose_spine_does_not_swallow_the_zero_contribution_refusal():
    waves = [[_wave_row("C1", [])], [_wave_row("C2", [])]]
    with pytest.raises(NoTestTargetError):
        terminal_test_scope(waves)


def _write_local_md(root, suffixes):
    (root / "coordinator.local.md").write_text(
        "---\n"
        f"test_locator_suffixes: [{', '.join(suffixes)}]\n"
        "---\n",
        encoding="utf-8",
    )


def test_terminal_test_scope_resolves_a_configured_non_py_suffix(tmp_path):
    _write_local_md(tmp_path, ["*.test.ts"])
    driver = tmp_path / "src/widgets/foo.test.ts"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    waves = [[_wave_row("C1", ["src/widgets/foo.ts"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["src/widgets/foo.test.ts"]


def test_map_written_path_to_test_target_resolves_a_configured_non_py_suffix(tmp_path):
    _write_local_md(tmp_path, ["*.test.ts"])
    driver = tmp_path / "src/widgets/foo.test.ts"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    assert (
        _map_written_path_to_test_target("src/widgets/foo.ts", repo_root=tmp_path)
        == "src/widgets/foo.test.ts"
    )


def test_a_written_test_locator_file_is_its_own_target(tmp_path):
    _write_local_md(tmp_path, ["*.test.ts"])
    waves = [[_wave_row("C1", ["src/widgets/foo.test.ts"])]]
    scope = terminal_test_scope(waves, repo_root=tmp_path)
    assert scope == ["src/widgets/foo.test.ts"]


def test_terminal_test_scope_treats_an_unresolved_configured_suffix_as_an_omission(tmp_path):
    _write_local_md(tmp_path, ["*.test.ts"])
    waves = [[_wave_row("C1", ["src/widgets/bar.ts"])]]
    with pytest.raises(NoTestTargetError) as excinfo:
        terminal_test_scope(waves, repo_root=tmp_path)
    assert "bar.ts" in str(excinfo.value)


def test_terminal_test_scope_does_not_derive_an_unconfigured_suffix(tmp_path):
    driver = tmp_path / "src/widgets/foo.test.ts"
    driver.parent.mkdir(parents=True)
    driver.write_text("", encoding="utf-8")
    waves = [[_wave_row("C1", ["src/widgets/foo.ts"])]]
    assert terminal_test_scope(waves, repo_root=tmp_path) == []
    assert (
        _map_written_path_to_test_target("src/widgets/foo.ts", repo_root=tmp_path)
        is None
    )


def test_candidate_test_targets_matches_suffix_by_own_source_suffix_not_prefix(tmp_path):
    # (`*.test.ts` -> `.ts`), so a path with a DIFFERENT suffix never matches
    _write_local_md(tmp_path, ["*.test.ts"])
    waves = [[_wave_row("C1", ["src/widgets/foo.test.js"])]]
    assert terminal_test_scope(waves, repo_root=tmp_path) == []


def test_candidate_test_additions_derives_the_co_located_test_for_a_py_path():
    assert candidate_test_additions(
        ["coordinator_core/ops/dispatch_emit/brand_new.py"]
    ) == ["coordinator_core/ops/dispatch_emit/tests/test_brand_new.py"]


def test_candidate_test_additions_ignores_non_py_paths():
    assert candidate_test_additions(["docs/wiki/dispatch-emit.md"]) == []


def test_candidate_test_additions_ignores_a_path_that_is_already_a_test_file():
    assert candidate_test_additions(["coordinator_core/ops/tests/test_foo.py"]) == []


def test_candidate_test_additions_dedupes_and_preserves_order():
    assert candidate_test_additions(
        [
            "coordinator_core/ops/a.py",
            "coordinator_core/ops/b.py",
            "coordinator_core/ops/a.py",
        ]
    ) == [
        "coordinator_core/ops/tests/test_a.py",
        "coordinator_core/ops/tests/test_b.py",
    ]


def test_candidate_test_additions_empty_for_an_empty_pathspec():
    assert candidate_test_additions([]) == []


def test_commit_pathspec_or_none_returns_the_same_pathspec_when_writes_exist():
    wave = [_wave_row("C1", ["coordinator_core/ops/dispatch_emit/pathspec.py"])]

    assert commit_pathspec_or_none(wave) == commit_pathspec(wave)


def test_commit_pathspec_or_none_degrades_undeclared_solo_row_to_none():
    """Refusal shape 1: a single UNDECLARED, non-concrete-surface row. This is
    the shape that sank a whole plan's emission -- `_all_writes_declared_empty`
    cannot reach it, so `commit_pathspec` was called and raised."""
    wave = [_wave_row("C1", UNDECLARED, surface="an epistemic premise")]

    with pytest.raises(NoWritesDeclaredError):
        commit_pathspec(wave)
    assert commit_pathspec_or_none(wave) is None


def test_commit_pathspec_or_none_degrades_all_empty_writes_to_none():
    wave = [_wave_row("C1", []), _wave_row("C2", [])]

    with pytest.raises(NoWritesDeclaredError):
        commit_pathspec(wave)
    assert commit_pathspec_or_none(wave) is None


def test_commit_pathspec_or_none_still_raises_on_a_directory_shaped_write():
    wave = [_wave_row("C1", ["coordinator_core/ops/dispatch_emit/"])]

    with pytest.raises(DirectoryShapedWriteError):
        commit_pathspec_or_none(wave)


def test_commit_pathspec_or_none_never_returns_an_empty_list():
    wave = [_wave_row("C1", ["coordinator_core/ops/dispatch_emit/pathspec.py"])]

    result = commit_pathspec_or_none(wave)

    assert result is not None and result != []
