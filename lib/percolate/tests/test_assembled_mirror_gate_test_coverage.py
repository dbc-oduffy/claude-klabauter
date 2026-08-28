"""Pins for `coordinator/lib/percolate/assembled_mirror_gate.py`'s
`find_modules_missing_tests` / `format_test_coverage_warning` (C6) — the
inverse of klabauter#3: a module that shipped while its own test did not,
silently, because tests participate in no import closure and nothing else
checks for their absence.

WARN-shaped throughout: no assertion here checks a `passed`/refuse field,
because there is none. Every test instead pins the denominator contract —
`examined_count` and `missing` must never collapse into one number, per
the parent plan's "0 modules missing tests over 0 examined is the
abstention this plan exists to kill".

Spec: docs/plans/2026-08-28-a-dropped-module-must-not-leave-its-test-behind.md
chunk C6.
"""

import sys
from pathlib import Path

# Same sys.path convention as the sibling C2 test file in this directory.
_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.assembled_mirror_gate import (  # noqa: E402
    ModuleTestCoverageReport,
    find_modules_missing_tests,
    format_test_coverage_warning,
)


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_module_with_a_matching_test_is_not_reported_missing(tmp_path):
    tree = tmp_path / "tree"
    _touch(tree / "foo.py")
    _touch(tree / "test_foo.py")

    report = find_modules_missing_tests(tree)

    assert report.examined_count == 1
    assert report.missing == ()
    assert report.missing_count == 0


def test_module_with_no_test_anywhere_is_reported_missing(tmp_path):
    tree = tmp_path / "tree"
    _touch(tree / "orphan_subject.py")

    report = find_modules_missing_tests(tree)

    assert report.examined_count == 1
    assert report.missing == ("orphan_subject.py",)
    assert report.missing_count == 1


def test_matching_is_by_stem_not_directory_adjacency(tmp_path):
    """The assembled mirror routinely ships a module and its test at
    different depths (module at tree root, test under `tests/`) — a
    directory-adjacency requirement would misreport every one of those as
    missing."""
    tree = tmp_path / "tree"
    _touch(tree / "foo.py")
    _touch(tree / "tests" / "test_foo.py")

    report = find_modules_missing_tests(tree)

    assert report.examined_count == 1
    assert report.missing == ()


def test_foo_test_suffix_shape_also_matches(tmp_path):
    tree = tmp_path / "tree"
    _touch(tree / "bar.py")
    _touch(tree / "bar_test.py")

    report = find_modules_missing_tests(tree)

    assert report.missing == ()


def test_init_and_conftest_are_excluded_from_the_examined_population(tmp_path):
    tree = tmp_path / "tree"
    _touch(tree / "__init__.py")
    _touch(tree / "conftest.py")

    report = find_modules_missing_tests(tree)

    assert report.examined_count == 0
    assert report.missing == ()


def test_test_files_themselves_are_never_counted_as_subjects(tmp_path):
    """A test file with no corresponding "test of the test" must never be
    reported missing — only non-test modules are subjects."""
    tree = tmp_path / "tree"
    _touch(tree / "test_lonely.py")

    report = find_modules_missing_tests(tree)

    assert report.examined_count == 0
    assert report.missing == ()


def test_denominator_never_collapses_zero_missing_with_zero_examined(tmp_path):
    """Pins the parent plan's own abstention warning directly: an empty
    tree and a fully-covered tree both report `missing == ()`, but must
    stay distinguishable via `examined_count`."""
    empty_tree = tmp_path / "empty_tree"
    empty_tree.mkdir()
    covered_tree = tmp_path / "covered_tree"
    _touch(covered_tree / "foo.py")
    _touch(covered_tree / "test_foo.py")

    empty_report = find_modules_missing_tests(empty_tree)
    covered_report = find_modules_missing_tests(covered_tree)

    assert empty_report.missing == covered_report.missing == ()
    assert empty_report.examined_count == 0
    assert covered_report.examined_count == 1
    assert empty_report.examined_count != covered_report.examined_count


def test_multiple_missing_modules_are_all_reported_and_sorted(tmp_path):
    tree = tmp_path / "tree"
    _touch(tree / "zeta_subject.py")
    _touch(tree / "alpha_subject.py")
    _touch(tree / "covered.py")
    _touch(tree / "test_covered.py")

    report = find_modules_missing_tests(tree)

    assert report.examined_count == 3
    assert report.missing == ("alpha_subject.py", "zeta_subject.py")
    assert report.missing_count == 2


# --- format_test_coverage_warning: always states the denominator -----------


def test_format_warning_states_denominator_when_nothing_is_missing():
    report = ModuleTestCoverageReport(examined_count=5, missing=())
    msg = format_test_coverage_warning(report)
    assert "0 module(s) missing a test" in msg
    assert "over 5 module(s) examined" in msg


def test_format_warning_lists_each_missing_module():
    report = ModuleTestCoverageReport(examined_count=2, missing=("a/orphan.py", "b/orphan2.py"))
    msg = format_test_coverage_warning(report)
    assert "2 module(s) missing a test" in msg
    assert "over 2 module(s) examined" in msg
    assert "a/orphan.py" in msg
    assert "b/orphan2.py" in msg


def test_format_warning_zero_examined_is_distinguishable_from_zero_missing():
    """The abstention shape itself: "0 missing over 0 examined" must never
    read the same as a genuine clean pass."""
    abstained = format_test_coverage_warning(ModuleTestCoverageReport(examined_count=0, missing=()))
    clean = format_test_coverage_warning(ModuleTestCoverageReport(examined_count=5, missing=()))
    assert abstained != clean
    assert "over 0 module(s) examined" in abstained
    assert "over 5 module(s) examined" in clean
