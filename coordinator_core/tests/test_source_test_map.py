"""coordinator_core/tests/test_source_test_map.py — C2 coverage.

Spec backlink: docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md
(C2). Covers AC2 (worked examples across five of the eight measured layouts
-- <mod>/tests/, flat-inside-module, flat-top, coordinator/bin/tests/, and
coordinator/tests/ flat), AC3 (empty map -> full tier is NOT this module's
call, but an unmapped source contributes zero candidates), and AC9
(conjunctive fail-safe: one un-mappable file in the diff forces
fully_mapped=False for the whole set).
"""

from __future__ import annotations

import textwrap

from coordinator_core.source_test_map import map_changed_sources, map_source_to_tests


def _pyproject(testpaths):
    roots = ", ".join(f'"{r}"' for r in testpaths)
    return textwrap.dedent(
        f"""
        [tool.pytest.ini_options]
        testpaths = [{roots}]
        """
    )


def _make_repo(tmp_path, testpaths):
    (tmp_path / "pyproject.toml").write_text(_pyproject(testpaths), encoding="utf-8")
    for root in testpaths:
        (tmp_path / root).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _touch(root, rel):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


# --------------------------------------------------------------------------
# AC2 — five layouts
# --------------------------------------------------------------------------


def test_layout_mod_tests_subdir(tmp_path):
    """coordinator_core/<mod>/tests/test_*.py"""
    root = _make_repo(tmp_path, ["coordinator_core"])
    _touch(root, "coordinator_core/session/foo.py")
    _touch(root, "coordinator_core/session/tests/test_foo.py")

    matches = map_source_to_tests("coordinator_core/session/foo.py", str(root))
    assert matches == ["coordinator_core/session/tests/test_foo.py"]


def test_layout_flat_inside_module(tmp_path):
    """coordinator_core/<mod>/test_*.py (flat, inside the module)"""
    root = _make_repo(tmp_path, ["coordinator_core"])
    _touch(root, "coordinator_core/session/bar.py")
    _touch(root, "coordinator_core/session/test_bar.py")

    matches = map_source_to_tests("coordinator_core/session/bar.py", str(root))
    assert matches == ["coordinator_core/session/test_bar.py"]


def test_layout_flat_top(tmp_path):
    """coordinator_core/test_*.py (flat top)"""
    root = _make_repo(tmp_path, ["coordinator_core"])
    _touch(root, "coordinator_core/baz.py")
    _touch(root, "coordinator_core/test_baz.py")

    matches = map_source_to_tests("coordinator_core/baz.py", str(root))
    assert matches == ["coordinator_core/test_baz.py"]


def test_layout_bin_tests_subdir_with_dash_normalization(tmp_path):
    """coordinator/bin/tests/test_*.py — source is a hyphenated CLI."""
    root = _make_repo(tmp_path, ["coordinator/bin"])
    _touch(root, "coordinator/bin/some-cli.py")
    _touch(root, "coordinator/bin/tests/test_some_cli.py")

    matches = map_source_to_tests("coordinator/bin/some-cli.py", str(root))
    assert matches == ["coordinator/bin/tests/test_some_cli.py"]


def test_layout_coordinator_tests_flat(tmp_path):
    """coordinator/tests/test_*.py (flat) — a distinct top-level root from
    the source file's own location (coordinator_core)."""
    root = _make_repo(tmp_path, ["coordinator_core", "coordinator/tests"])
    _touch(root, "coordinator_core/quux.py")
    _touch(root, "coordinator/tests/test_quux.py")

    matches = map_source_to_tests("coordinator_core/quux.py", str(root))
    assert matches == ["coordinator/tests/test_quux.py"]


def test_two_root_case_returns_every_candidate(tmp_path):
    """The canonical multi-root case: a source file with covering tests in
    TWO testpaths roots must return BOTH, never pick one (workday-complete-
    step1-validate.py's real shape)."""
    root = _make_repo(tmp_path, ["coordinator/tests", "coordinator/bin/tests"])
    _touch(root, "coordinator/bin/workday-complete-step1-validate.py")
    _touch(root, "coordinator/tests/test_workday_complete_step1_validate.py")
    _touch(root, "coordinator/bin/tests/test_workday_complete_step1_validate.py")

    matches = map_source_to_tests(
        "coordinator/bin/workday-complete-step1-validate.py", str(root)
    )
    assert matches == [
        "coordinator/bin/tests/test_workday_complete_step1_validate.py",
        "coordinator/tests/test_workday_complete_step1_validate.py",
    ]


# --------------------------------------------------------------------------
# AC3 / empty-input semantics
# --------------------------------------------------------------------------


def test_unmappable_source_returns_empty_candidates(tmp_path):
    root = _make_repo(tmp_path, ["coordinator_core"])
    matches = map_source_to_tests("coordinator_core/nope.py", str(root))
    assert matches == []


def test_map_changed_sources_empty_input_is_fully_mapped_trivially(tmp_path):
    root = _make_repo(tmp_path, ["coordinator_core"])
    candidates, fully_mapped = map_changed_sources([], str(root))
    assert candidates == []
    assert fully_mapped is True


def test_map_changed_sources_all_mapped(tmp_path):
    root = _make_repo(tmp_path, ["coordinator_core"])
    _touch(root, "coordinator_core/a.py")
    _touch(root, "coordinator_core/test_a.py")
    _touch(root, "coordinator_core/b.py")
    _touch(root, "coordinator_core/test_b.py")

    candidates, fully_mapped = map_changed_sources(
        ["coordinator_core/a.py", "coordinator_core/b.py"], str(root)
    )
    assert candidates == ["coordinator_core/test_a.py", "coordinator_core/test_b.py"]
    assert fully_mapped is True


# --------------------------------------------------------------------------
# AC9 — conjunctive fail-safe
# --------------------------------------------------------------------------


def test_one_unmappable_file_forces_fully_mapped_false_for_whole_set(tmp_path):
    root = _make_repo(tmp_path, ["coordinator_core"])
    _touch(root, "coordinator_core/a.py")
    _touch(root, "coordinator_core/test_a.py")
    _touch(root, "coordinator_core/unmapped.py")  # no covering test

    candidates, fully_mapped = map_changed_sources(
        ["coordinator_core/a.py", "coordinator_core/unmapped.py"], str(root)
    )
    # Additive: the mappable file's candidate is still returned...
    assert candidates == ["coordinator_core/test_a.py"]
    # ...but the set as a whole is NOT fully mapped, forcing the caller to
    # the full tier rather than trusting a partial narrowing.
    assert fully_mapped is False


def test_all_unmappable_yields_no_candidates_and_not_fully_mapped(tmp_path):
    root = _make_repo(tmp_path, ["coordinator_core"])
    candidates, fully_mapped = map_changed_sources(
        ["coordinator_core/x.py", "coordinator_core/y.py"], str(root)
    )
    assert candidates == []
    assert fully_mapped is False
