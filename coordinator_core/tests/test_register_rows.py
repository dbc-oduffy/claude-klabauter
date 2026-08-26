"""Tests for the row-resolution helper (`coordinator_core/tests/register_rows.py`).

Covers: RegisterId keying (bare-name collisions do not conflate), the
index-first branch (path classes never touch AST, dotted classes never
touch the tracked-file spawn beyond the shared index), the two
separately-assertable readings (`rows_that_do_not_resolve` /
`assert_canary_present`), and the no-per-row-spawn constraint.
"""

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
        subject="coordinator_core.tests.totally_nonexistent_module_xyz.member",
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
    """Resolving many rows against a pre-built index must spawn no subprocess."""
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
