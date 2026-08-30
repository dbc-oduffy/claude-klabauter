"""Pins for `coordinator/lib/percolate/assembled_mirror_gate.py`'s
`find_modules_missing_tests` / `format_test_coverage_warning` (C6) — the
inverse of klabauter#3: a module that shipped while its own test did not,
silently, because tests participate in no import closure and nothing else
checks for their absence.

`find_modules_missing_tests` compares the ASSEMBLED mirror against the
SOURCE tree it was built from: a subject is reported only when its test
exists in the source tree but did not ship with the mirror. A subject
that never had a test anywhere (source included) must never be reported —
comparing the mirror against itself, as an earlier version of this
function did, conflated "the test stayed home" with "this module never
had a test", and the second dominates by two orders of magnitude on the
real claude-klabauter<->klabauter pair.

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
    source = tmp_path / "source"
    _touch(tree / "foo.py")
    _touch(tree / "test_foo.py")
    _touch(source / "foo.py")
    _touch(source / "test_foo.py")

    report = find_modules_missing_tests(tree, source)

    assert report.examined_count == 1
    assert report.missing == ()
    assert report.missing_count == 0


def test_module_with_no_test_anywhere_is_not_reported_missing(tmp_path):
    """The whole point of comparing against the source tree: a module that
    never had a test anywhere (source included) must not be reported —
    only a test that exists in source but stayed home is actionable."""
    tree = tmp_path / "tree"
    source = tmp_path / "source"
    _touch(tree / "orphan_subject.py")
    _touch(source / "orphan_subject.py")

    report = find_modules_missing_tests(tree, source)

    assert report.examined_count == 1
    assert report.missing == ()
    assert report.missing_count == 0


def test_test_that_stayed_home_is_reported(tmp_path):
    """The one case this gate exists to catch: source carries a test for
    the shipped subject, but the mirror does not."""
    tree = tmp_path / "tree"
    source = tmp_path / "source"
    _touch(tree / "left_behind.py")
    _touch(source / "left_behind.py")
    _touch(source / "test_left_behind.py")

    report = find_modules_missing_tests(tree, source)

    assert report.examined_count == 1
    assert report.missing == ("left_behind.py",)
    assert report.missing_count == 1


def test_matching_is_by_stem_not_directory_adjacency(tmp_path):
    """The assembled mirror routinely ships a module and its test at
    different depths (module at tree root, test under `tests/`) — a
    directory-adjacency requirement would misreport every one of those as
    missing."""
    tree = tmp_path / "tree"
    source = tmp_path / "source"
    _touch(tree / "foo.py")
    _touch(tree / "tests" / "test_foo.py")
    _touch(source / "foo.py")
    _touch(source / "tests" / "test_foo.py")

    report = find_modules_missing_tests(tree, source)

    assert report.examined_count == 1
    assert report.missing == ()


def test_foo_test_suffix_shape_also_matches(tmp_path):
    tree = tmp_path / "tree"
    source = tmp_path / "source"
    _touch(tree / "bar.py")
    _touch(tree / "bar_test.py")
    _touch(source / "bar.py")
    _touch(source / "bar_test.py")

    report = find_modules_missing_tests(tree, source)

    assert report.missing == ()


def test_init_and_conftest_are_excluded_from_the_examined_population(tmp_path):
    tree = tmp_path / "tree"
    source = tmp_path / "source"
    _touch(tree / "__init__.py")
    _touch(tree / "conftest.py")

    report = find_modules_missing_tests(tree, source)

    assert report.examined_count == 0
    assert report.missing == ()


def test_test_files_themselves_are_never_counted_as_subjects(tmp_path):
    """A test file with no corresponding "test of the test" must never be
    reported missing — only non-test modules are subjects."""
    tree = tmp_path / "tree"
    source = tmp_path / "source"
    _touch(tree / "test_lonely.py")

    report = find_modules_missing_tests(tree, source)

    assert report.examined_count == 0
    assert report.missing == ()


def test_denominator_never_collapses_zero_missing_with_zero_examined(tmp_path):
    """Pins the parent plan's own abstention warning directly: an empty
    tree and a fully-covered tree both report `missing == ()`, but must
    stay distinguishable via `examined_count`."""
    empty_tree = tmp_path / "empty_tree"
    empty_tree.mkdir()
    empty_source = tmp_path / "empty_source"
    empty_source.mkdir()
    covered_tree = tmp_path / "covered_tree"
    covered_source = tmp_path / "covered_source"
    _touch(covered_tree / "foo.py")
    _touch(covered_tree / "test_foo.py")
    _touch(covered_source / "foo.py")
    _touch(covered_source / "test_foo.py")

    empty_report = find_modules_missing_tests(empty_tree, empty_source)
    covered_report = find_modules_missing_tests(covered_tree, covered_source)

    assert empty_report.missing == covered_report.missing == ()
    assert empty_report.examined_count == 0
    assert covered_report.examined_count == 1
    assert empty_report.examined_count != covered_report.examined_count


def test_multiple_missing_modules_are_all_reported_and_sorted(tmp_path):
    tree = tmp_path / "tree"
    source = tmp_path / "source"
    _touch(tree / "zeta_subject.py")
    _touch(tree / "alpha_subject.py")
    _touch(tree / "covered.py")
    _touch(tree / "test_covered.py")
    _touch(source / "zeta_subject.py")
    _touch(source / "test_zeta_subject.py")
    _touch(source / "alpha_subject.py")
    _touch(source / "test_alpha_subject.py")
    _touch(source / "covered.py")
    _touch(source / "test_covered.py")

    report = find_modules_missing_tests(tree, source)

    assert report.examined_count == 3
    assert report.missing == ("alpha_subject.py", "zeta_subject.py")
    assert report.missing_count == 2


def test_source_root_accepts_multiple_roots(tmp_path):
    """A destination repo root can be fed by more than one row, each with
    its own source_dir (publish.py's rows_by_repo_root) — the function
    must accept a sequence and union their test stems."""
    tree = tmp_path / "tree"
    source_a = tmp_path / "source_a"
    source_b = tmp_path / "source_b"
    _touch(tree / "from_a.py")
    _touch(tree / "from_b.py")
    _touch(source_a / "from_a.py")
    _touch(source_a / "test_from_a.py")
    _touch(source_b / "from_b.py")
    _touch(source_b / "test_from_b.py")

    report = find_modules_missing_tests(tree, [source_a, source_b])

    assert report.examined_count == 2
    assert report.missing == ("from_a.py", "from_b.py")


# --- format_test_coverage_warning: always states the denominator -----------


def test_format_warning_states_denominator_when_nothing_is_missing():
    report = ModuleTestCoverageReport(examined_count=5, missing=())
    msg = format_test_coverage_warning(report)
    assert "0 module(s) shipped without the test" in msg
    assert "over 5 module(s) examined" in msg


def test_format_warning_lists_each_missing_module():
    report = ModuleTestCoverageReport(examined_count=2, missing=("a/orphan.py", "b/orphan2.py"))
    msg = format_test_coverage_warning(report)
    assert "2 module(s) shipped without the test" in msg
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


def test_format_warning_caps_the_listed_paths_and_states_elided_count():
    missing = tuple(f"m{i}.py" for i in range(60))
    report = ModuleTestCoverageReport(examined_count=100, missing=missing)
    msg = format_test_coverage_warning(report)
    assert "m0.py" in msg
    assert "m49.py" in msg
    assert "m50.py" not in msg
    assert "10 more (elided)" in msg
