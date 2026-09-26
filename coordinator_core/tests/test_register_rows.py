
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.tests.register_rows import (
    RegisterId,
    Resolution,
    ResolutionKind,
    Row,
    SubjectClass,
    TrackedFileIndex,
    assert_canary_present,
    resolve_row,
    rows_that_do_not_resolve,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def index() -> TrackedFileIndex:
    return TrackedFileIndex.build(REPO_ROOT)


def test_register_id_is_path_and_name_tuple() -> None:
    a = RegisterId("coordinator_core/tests/foo.py", "_ALLOWLIST")
    b = RegisterId("coordinator_core/other/bar.py", "_ALLOWLIST")
    assert a != b
    assert a.constant_name == b.constant_name == "_ALLOWLIST"
    assert {a, b} == {a, b}


def test_repo_path_row_resolves_via_index_not_ast(index: TrackedFileIndex) -> None:
    row = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "SELF"),
        subject="coordinator_core/tests/__init__.py",
        declared_class=SubjectClass.REPO_PATH,
    )
    resolution = resolve_row(row, index, REPO_ROOT)
    assert resolution.kind is ResolutionKind.RESOLVED


def test_repo_path_row_absent_for_deleted_file(index: TrackedFileIndex) -> None:
    row = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "GONE"),
        subject="coordinator_core/tests/this_file_does_not_exist_anywhere.py",
        declared_class=SubjectClass.REPO_PATH,
    )
    resolution = resolve_row(row, index, REPO_ROOT)
    assert resolution.kind is ResolutionKind.ABSENT


def test_bare_filename_row_resolves_by_basename() -> None:
    synthetic_index = TrackedFileIndex(frozenset({"coordinator_core/tests/register_rows.py"}))
    row = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "SELF"),
        subject="register_rows.py",
        declared_class=SubjectClass.BARE_FILENAME,
    )
    resolution = resolve_row(row, synthetic_index, REPO_ROOT)
    assert resolution.kind is ResolutionKind.RESOLVED


def test_bare_filename_row_absent_for_unknown_basename(index: TrackedFileIndex) -> None:
    row = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "GONE"),
        subject="totally_nonexistent_basename_xyz.py",
        declared_class=SubjectClass.BARE_FILENAME,
    )
    resolution = resolve_row(row, index, REPO_ROOT)
    assert resolution.kind is ResolutionKind.ABSENT


def test_symbol_row_resolves_via_ast() -> None:
    synthetic_index = TrackedFileIndex(frozenset({"coordinator_core/tests/register_rows.py"}))
    row = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "SELF"),
        subject="coordinator_core.tests.register_rows.resolve_row",
        declared_class=SubjectClass.SYMBOL,
    )
    resolution = resolve_row(row, synthetic_index, REPO_ROOT)
    assert resolution.kind is ResolutionKind.RESOLVED


def test_symbol_row_absent_for_missing_member() -> None:
    synthetic_index = TrackedFileIndex(frozenset({"coordinator_core/tests/register_rows.py"}))
    row = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "GONE"),
        subject="coordinator_core.tests.register_rows.this_function_does_not_exist",
        declared_class=SubjectClass.SYMBOL,
    )
    resolution = resolve_row(row, synthetic_index, REPO_ROOT)
    assert resolution.kind is ResolutionKind.ABSENT


def test_module_row_absent_for_untracked_module(index: TrackedFileIndex) -> None:
    row = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "GONE"),
        subject="coordinator_core.tests.totally_nonexistent_module_xyz",
        declared_class=SubjectClass.MODULE,
    )
    resolution = resolve_row(row, index, REPO_ROOT)
    assert resolution.kind is ResolutionKind.ABSENT


def test_opaque_row_is_always_unadjudicable(index: TrackedFileIndex) -> None:
    row = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "MYSTERY"),
        subject="anything",
        declared_class=SubjectClass.OPAQUE,
    )
    resolution = resolve_row(row, index, REPO_ROOT)
    assert resolution.kind is ResolutionKind.UNADJUDICABLE


def test_rows_that_do_not_resolve_reports_only_absent_rows() -> None:
    synthetic_index = TrackedFileIndex(frozenset({"coordinator_core/tests/register_rows.py"}))
    live = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "LIVE"),
        subject="coordinator_core/tests/register_rows.py",
        declared_class=SubjectClass.REPO_PATH,
    )
    dead = Row(
        register=RegisterId("coordinator_core/tests/test_register_rows.py", "DEAD"),
        subject="coordinator_core/tests/this_file_does_not_exist_anywhere.py",
        declared_class=SubjectClass.REPO_PATH,
    )
    result = rows_that_do_not_resolve([live, dead], synthetic_index, REPO_ROOT)
    assert [row for row, _ in result] == [dead]


def test_assert_canary_present_passes_when_subset() -> None:
    assert_canary_present(frozenset({"a", "b", "c"}), frozenset({"a", "b"}))


def test_assert_canary_present_raises_when_canary_missing() -> None:
    with pytest.raises(AssertionError):
        assert_canary_present(frozenset({"a"}), frozenset({"a", "b"}))


def test_assert_canary_present_catches_empty_derived_set() -> None:
    with pytest.raises(AssertionError):
        assert_canary_present(frozenset(), frozenset({"a"}))


def test_resolution_predicates_are_mutually_exclusive() -> None:
    resolved = Resolution(ResolutionKind.RESOLVED)
    absent = Resolution(ResolutionKind.ABSENT)
    unadjudicable = Resolution(ResolutionKind.UNADJUDICABLE)
    assert resolved.resolved and not resolved.absent and not resolved.unadjudicable
    assert absent.absent and not absent.resolved and not absent.unadjudicable
    assert unadjudicable.unadjudicable and not unadjudicable.resolved and not unadjudicable.absent


def test_no_per_row_git_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic_index = TrackedFileIndex(frozenset({"coordinator_core/tests/register_rows.py"}))

    def _forbidden_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resolve_row must not spawn a subprocess per row")

    monkeypatch.setattr(subprocess, "run", _forbidden_run)

    rows = [
        Row(
            register=RegisterId("coordinator_core/tests/test_register_rows.py", f"ROW_{i}"),
            subject="coordinator_core/tests/register_rows.py",
            declared_class=SubjectClass.REPO_PATH,
        )
        for i in range(50)
    ]
    for row in rows:
        resolve_row(row, synthetic_index, REPO_ROOT)


def test_tracked_file_index_build_spawns_exactly_one_git_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real_run = subprocess.run

    def _counting_run(*args: Any, **kwargs: Any):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    TrackedFileIndex.build(REPO_ROOT)
    assert len(calls) == 1


def test_module_row_resolves_as_a_file_not_as_a_member_of_its_parent() -> None:
    index = TrackedFileIndex(frozenset({"pkg/sub/leaf.py", "pkg/sub.py"}))
    row = Row(
        register=RegisterId("probe.py", "_MODULES"),
        subject="pkg.sub.leaf",
        declared_class=SubjectClass.MODULE,
    )

    resolution = resolve_row(row, index, REPO_ROOT)

    assert resolution.resolved, resolution
    assert resolution.detail == "pkg/sub/leaf.py"


def test_module_row_resolves_a_package_via_its_dunder_init() -> None:
    index = TrackedFileIndex(frozenset({"pkg/sub/__init__.py"}))
    row = Row(
        register=RegisterId("probe.py", "_MODULES"),
        subject="pkg.sub",
        declared_class=SubjectClass.MODULE,
    )

    assert resolve_row(row, index, REPO_ROOT).resolved


def test_module_row_is_absent_only_when_no_file_backs_it() -> None:
    index = TrackedFileIndex(frozenset({"pkg/sub.py"}))
    row = Row(
        register=RegisterId("probe.py", "_MODULES"),
        subject="pkg.gone",
        declared_class=SubjectClass.MODULE,
    )

    resolution = resolve_row(row, index, REPO_ROOT)

    assert resolution.absent, resolution
    assert "pkg.gone" in (resolution.detail or "")


def test_module_row_resolution_never_parses_its_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ast as ast_module

    def _forbidden_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a MODULE row must resolve by index lookup, not by parsing")

    monkeypatch.setattr(ast_module, "parse", _forbidden_parse)

    index = TrackedFileIndex(frozenset({"pkg/sub/leaf.py"}))
    row = Row(
        register=RegisterId("probe.py", "_MODULES"),
        subject="pkg.sub.leaf",
        declared_class=SubjectClass.MODULE,
    )

    assert resolve_row(row, index, REPO_ROOT).resolved


def test_symbol_row_still_resolves_its_final_segment_as_a_member(tmp_path: Path) -> None:
    module = tmp_path / "holder.py"
    module.write_text("def the_member():\n    pass\n", encoding="utf-8")
    index = TrackedFileIndex(frozenset({"holder.py"}))

    present = Row(
        register=RegisterId("probe.py", "_SYMBOLS"),
        subject="holder.the_member",
        declared_class=SubjectClass.SYMBOL,
    )
    missing = Row(
        register=RegisterId("probe.py", "_SYMBOLS"),
        subject="holder.no_such_member",
        declared_class=SubjectClass.SYMBOL,
    )

    assert resolve_row(present, index, tmp_path).resolved
    assert resolve_row(missing, index, tmp_path).absent


def test_the_two_dotted_classes_take_different_routes_on_the_same_subject(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "leaf.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "sub.py").write_text("OTHER = 2\n", encoding="utf-8")
    index = TrackedFileIndex(frozenset({"pkg/sub/leaf.py", "pkg/sub.py"}))

    as_module = Row(
        register=RegisterId("probe.py", "_MODULES"),
        subject="pkg.sub.leaf",
        declared_class=SubjectClass.MODULE,
    )
    as_symbol = Row(
        register=RegisterId("probe.py", "_SYMBOLS"),
        subject="pkg.sub.leaf",
        declared_class=SubjectClass.SYMBOL,
    )

    assert resolve_row(as_module, index, tmp_path).resolved
    assert resolve_row(as_symbol, index, tmp_path).absent


def test_dotted_rows_locate_their_module_at_any_package_depth() -> None:
    index = TrackedFileIndex(frozenset({"deep/pkg/ipc.py", "other/thing.py"}))

    as_module = Row(
        register=RegisterId("probe.py", "_MODULES"),
        subject="ipc",
        declared_class=SubjectClass.MODULE,
    )
    resolution = resolve_row(as_module, index, REPO_ROOT)
    assert resolution.resolved, resolution
    assert resolution.detail == "deep/pkg/ipc.py"


def test_symbol_row_locates_a_relatively_named_module(tmp_path: Path) -> None:
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "pkg").mkdir()
    (tmp_path / "deep" / "pkg" / "ipc.py").write_text(
        "def _is_stamped():\n    pass\n", encoding="utf-8"
    )
    index = TrackedFileIndex(frozenset({"deep/pkg/ipc.py"}))

    row = Row(
        register=RegisterId("probe.py", "_SYMBOLS"),
        subject="ipc._is_stamped",
        declared_class=SubjectClass.SYMBOL,
    )

    assert resolve_row(row, index, tmp_path).resolved


def test_symbol_row_ignores_a_nested_scope_shadow_of_a_deleted_module_level_name(
    tmp_path: Path,
) -> None:
    """A SYMBOL row names a module-level definition, not any name in the file.

    Regression: `_resolve_symbol` originally walked the whole tree
    (`ast.walk`), so a deleted module-level `_is_stamped` read as RESOLVED
    whenever some unrelated nested scope happened to bind a local of the
    same name. That is the reads-as-CLOSURE failure in its silent
    direction -- a genuinely dead row reported live.
    """
    module = tmp_path / "holder.py"
    module.write_text(
        "def other_function():\n"
        "    def _is_stamped():\n"
        "        pass\n"
        "    return _is_stamped\n",
        encoding="utf-8",
    )
    index = TrackedFileIndex(frozenset({"holder.py"}))
    row = Row(
        register=RegisterId("probe.py", "_SYMBOLS"),
        subject="holder._is_stamped",
        declared_class=SubjectClass.SYMBOL,
    )

    assert resolve_row(row, index, tmp_path).absent


def test_op_name_row_resolves_on_exact_membership_hit(index: TrackedFileIndex) -> None:
    row = Row(
        register=RegisterId("probe.py", "_OPS"),
        subject="my.op.name",
        declared_class=SubjectClass.OP_NAME,
    )
    resolution = resolve_row(row, index, REPO_ROOT, op_names=frozenset({"my.op.name"}))
    assert resolution.resolved, resolution


def test_op_name_row_is_absent_not_unadjudicable_on_miss(index: TrackedFileIndex) -> None:
    row = Row(
        register=RegisterId("probe.py", "_OPS"),
        subject="never.registered",
        declared_class=SubjectClass.OP_NAME,
    )
    resolution = resolve_row(row, index, REPO_ROOT, op_names=frozenset({"my.op.name"}))
    assert resolution.absent, resolution


def test_op_name_row_raises_loudly_when_oracle_missing(index: TrackedFileIndex) -> None:
    row = Row(
        register=RegisterId("probe.py", "_OPS"),
        subject="my.op.name",
        declared_class=SubjectClass.OP_NAME,
    )
    with pytest.raises(ValueError):
        resolve_row(row, index, REPO_ROOT, op_names=None)


def test_subject_class_op_name_round_trips_from_declaration_string() -> None:
    """`__SUBJECT_CLASS = "op-name"` must map back to the enum member."""
    assert SubjectClass("op-name") is SubjectClass.OP_NAME


def test_an_ambiguous_dotted_module_is_unadjudicable_never_resolved() -> None:
    index = TrackedFileIndex(frozenset({"a/ipc.py", "b/ipc.py"}))

    as_module = Row(
        register=RegisterId("probe.py", "_MODULES"),
        subject="ipc",
        declared_class=SubjectClass.MODULE,
    )
    as_symbol = Row(
        register=RegisterId("probe.py", "_SYMBOLS"),
        subject="ipc.some_member",
        declared_class=SubjectClass.SYMBOL,
    )

    module_resolution = resolve_row(as_module, index, REPO_ROOT)
    symbol_resolution = resolve_row(as_symbol, index, REPO_ROOT)

    assert module_resolution.unadjudicable, module_resolution
    assert "ambiguous" in module_resolution.detail
    assert symbol_resolution.unadjudicable, symbol_resolution
    assert "ambiguous" in symbol_resolution.detail


def test_a_repo_rooted_dotted_module_still_wins_over_a_deeper_namesake() -> None:
    index = TrackedFileIndex(frozenset({"ipc.py", "deep/ipc.py"}))
    row = Row(
        register=RegisterId("probe.py", "_MODULES"),
        subject="ipc",
        declared_class=SubjectClass.MODULE,
    )

    assert resolve_row(row, index, REPO_ROOT).unadjudicable
