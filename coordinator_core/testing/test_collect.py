"""
coordinator_core.testing.test_collect — scoped tests for the collect.py engine.

Purpose: hermetic (no example-doctrine-repo dependency) proof that `discover()` classifies all four
families and excludes bundled-venv `test_*.py` decoys (DEC-2); plus a guarded
Example-doctrine-repo-present integration smoke that asserts classification integrity (non-empty
discovery, valid family, correct runner_kind) against the real sibling tree —
not that tree's content mix, which this repo does not own.

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: docs/plans/2026-07-19-claude-klabauter-doe-full-test-runner.md § C1 (AC1, AC9-partial)

Negative-spec: the committed `test_*.py` modules under `coordinator_core/testing/`
are the allow-listed set in `_REAL_TEST_MODULES` below (this module plus the runner
tests, `test_golden.py` — the parity-goldens helper's unit test — and
`test_suite_mutex.py`, the machine-wide test-suite mutex's own unit test) — every
fixture repo tree this module exercises is built via the shared `fixture_tree`
factory (conftest.py) under pytest's `tmp_path`, never committed to disk (Finding 9).
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from coordinator_core.testing.collect import (
    ALL_FAMILIES,
    FAMILY_RUNNER_KIND,
    discover,
)
from coordinator_core.testing.doe_root import doe_root_and_present

_TESTING_PKG_DIR = Path(__file__).resolve().parent
_REAL_TEST_MODULES = {
    "test_collect.py",
    "test_run.py",
    "test_full_runner.py",
    "test_home_sandbox.py",
    "test_golden.py",
    "test_suite_mutex.py",
    "test_symlink_capability.py",
}


def test_all_four_families_discovered_and_classified(fixture_tree) -> None:
    tree = fixture_tree()
    suites = discover(tree.repo_root)

    found_families = {s.family for s in suites}
    assert found_families == ALL_FAMILIES

    by_family = {s.family: s for s in suites}
    for family, expected_path in tree.family_files.items():
        suite = by_family[family]
        assert suite.path == expected_path
        assert suite.runner_kind == FAMILY_RUNNER_KIND[family]


def test_decoy_venv_test_file_is_excluded(fixture_tree) -> None:
    tree = fixture_tree(venv_dirname=".venv")
    suites = discover(tree.repo_root)

    discovered_paths = {s.path for s in suites}
    assert tree.decoy_venv_test not in discovered_paths
    # The real py-native fixture (outside the venv) must still be present.
    assert tree.family_files["py-native"] in discovered_paths


def test_decoy_site_packages_and_coordinator_venv_excluded(fixture_tree) -> None:
    for venv_dirname in ("site-packages", ".coordinator-venv"):
        tree = fixture_tree(root=None, venv_dirname=venv_dirname)
        suites = discover(tree.repo_root)
        discovered_paths = {s.path for s in suites}
        assert tree.decoy_venv_test not in discovered_paths


def test_no_stray_test_py_files_committed_under_testing_package() -> None:
    # Finding 9 belt check: nothing under coordinator_core/testing/ matches
    # pytest's own collection glob (test_*.py) beyond the real, intentionally
    # committed test modules for this package.
    stray = []
    for path_str in glob.glob(str(_TESTING_PKG_DIR / "**" / "test_*.py"), recursive=True):
        path = Path(path_str)
        if path.name not in _REAL_TEST_MODULES:
            stray.append(path)
    assert stray == [], f"stray test_*.py fixture(s) committed under {_TESTING_PKG_DIR}: {stray}"


_DOE_ROOT, _DOE_PRESENT = doe_root_and_present()


@pytest.mark.skipif(not _DOE_PRESENT, reason="example-doctrine-repo repo root not resolvable on this machine")
def test_doe_integration_discovers_and_classifies_real_tree() -> None:
    # Negative-spec (2026-07-25): this test formerly asserted lower-bound
    # COUNTS per family (js-prefix >= 13, js-suffix >= 30, py-native >= 40,
    # py-nonnative >= 15) against the sibling example-doctrine-repo clone. Those floors
    # asserted the SIBLING REPO'S CONTENT — a tree this repo neither owns nor
    # controls — not this repo's own logic, and drifted twice as example-doctrine-repo mutated
    # its own suite mix (most recently example-doctrine-repo deliberately retiring its JS/TS
    # corpus in commits 1750bb79 / b069e8f2, driving js-prefix to 0 here). A
    # same-day guard (commit 0a940fce) papered over the js-prefix collapse
    # with a `pytest.skip()`, which also silently hid that js-suffix (17/30)
    # and py-nonnative (1/15) were already under floor — the test was
    # skipping on this machine and telling nobody anything. The classification
    # contract this test exists to protect — every family found, every suite
    # tagged with the right runner_kind — is already covered hermetically by
    # `test_all_four_families_discovered_and_classified` above against a
    # `fixture_tree`, which is unaffected by what example-doctrine-repo's tree happens to
    # contain on any given day. This test now asserts only that `discover()`
    # runs cleanly against a REAL tree and classifies whatever it finds
    # correctly — not what that tree's suite mix happens to be.
    suites = discover(_DOE_ROOT)

    assert suites, f"discover() found no suites at all under {_DOE_ROOT}"
    for suite in suites:
        assert suite.family in ALL_FAMILIES
        assert suite.runner_kind == FAMILY_RUNNER_KIND[suite.family]
