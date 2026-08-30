"""
Tests for coordinator_core.diff_scoped_tests -- see that module's own
docstring for the "changed test file" definition and the append-only
contract this exercises. Spec backlink: PM-ratified scope cut, sizing
record state/sizings/2026-07-30-diff-scoped-routine-ceremony-gates.yaml.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.diff_scoped_tests import (
    PYTEST_NO_TESTS_COLLECTED,
    append_test_paths,
    find_changed_test_files,
)
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_MARKER_CMD = (
    "python3 -m pytest -m 'not cadence and not pending_fix and not designed_red' "
    "-n auto --maxprocesses=12"
)


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path):
    """A minimal git repo with a pinned testpaths and one committed test
    file, matching this repo's own layout closely enough for the
    testpaths-membership filter to exercise realistically."""
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)

    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["pkg"]\n'
    )
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "test_existing.py").write_text("def test_a():\n    assert True\n")
    (pkg / "not_a_test.py").write_text("x = 1\n")

    outside = tmp_path / "scratch"
    outside.mkdir()
    (outside / "test_outside.py").write_text("def test_b():\n    assert True\n")

    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    return tmp_path


# --- find_changed_test_files -------------------------------------------------


def test_no_changed_test_files_returns_empty(tmp_path):
    repo = _init_repo(tmp_path)
    assert find_changed_test_files(str(repo)) == []


def test_modified_tracked_test_file_included(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "pkg" / "test_existing.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n"
    )
    assert find_changed_test_files(str(repo)) == ["pkg/test_existing.py"]


def test_staged_new_test_file_included(tmp_path):
    repo = _init_repo(tmp_path)
    new_file = repo / "pkg" / "test_new.py"
    new_file.write_text("def test_c():\n    assert True\n")
    _git(["add", str(new_file)], repo)
    assert find_changed_test_files(str(repo)) == ["pkg/test_new.py"]


def test_untracked_new_test_file_included(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "pkg" / "test_untracked.py").write_text(
        "def test_d():\n    assert True\n"
    )
    assert find_changed_test_files(str(repo)) == ["pkg/test_untracked.py"]


def test_deleted_test_file_excluded(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "pkg" / "test_existing.py").unlink()
    assert find_changed_test_files(str(repo)) == []


def test_non_test_file_change_excluded(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "pkg" / "not_a_test.py").write_text("x = 2\n")
    assert find_changed_test_files(str(repo)) == []


def test_test_file_outside_testpaths_excluded(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "scratch" / "test_outside.py").write_text(
        "def test_b():\n    assert False\n"
    )
    assert find_changed_test_files(str(repo)) == []


def test_multiple_changed_test_files_sorted(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "pkg" / "test_untracked_z.py").write_text("def test_z():\n    pass\n")
    (repo / "pkg" / "test_existing.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    pass\n"
    )
    assert find_changed_test_files(str(repo)) == [
        "pkg/test_existing.py",
        "pkg/test_untracked_z.py",
    ]


# --- append_test_paths --------------------------------------------------------


def test_append_test_paths_empty_list_returns_unchanged():
    assert append_test_paths(_MARKER_CMD, []) == _MARKER_CMD


def test_append_test_paths_appends_and_preserves_marker_expression():
    result = append_test_paths(_MARKER_CMD, ["pkg/test_existing.py"])
    assert result.startswith(_MARKER_CMD + " ")
    assert "-m 'not cadence and not pending_fix and not designed_red'" in result
    assert result.endswith("pkg/test_existing.py")


def test_append_test_paths_shell_quotes_each_path():
    result = append_test_paths(_MARKER_CMD, ["pkg/test_existing.py", "pkg/test_untracked.py"])
    assert result == _MARKER_CMD + " pkg/test_existing.py pkg/test_untracked.py"


def test_append_test_paths_quotes_path_with_space():
    result = append_test_paths(_MARKER_CMD, ["pkg/weird dir/test_x.py"])
    assert result == _MARKER_CMD + " 'pkg/weird dir/test_x.py'"


# --- rc=5 contract -------------------------------------------------------------


def test_pytest_no_tests_collected_constant_matches_pytest_contract():
    assert PYTEST_NO_TESTS_COLLECTED == 5


def test_rc5_reproduces_against_real_pytest(tmp_path):
    """Pin the sharp edge named in the brief: a real pytest invocation whose
    marker filter deselects the only named file exits 5, not 0 and not
    nonzero-for-a-different-reason."""
    pytest_bin = pytest.importorskip("pytest")  # ensure pytest importable
    del pytest_bin
    test_dir = tmp_path / "pkg"
    test_dir.mkdir()
    (test_dir / "test_deselected.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.designed_red\n"
        "def test_never_runs():\n"
        "    assert False\n"
    )
    (test_dir / "conftest.py").write_text(
        "def pytest_configure(config):\n"
        "    config.addinivalue_line('markers', 'designed_red: red by design')\n"
    )
    proc = subprocess.run(
        [
            "python3",
            "-m",
            "pytest",
            "-m",
            "not designed_red",
            str(test_dir / "test_deselected.py"),
        ],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert proc.returncode == PYTEST_NO_TESTS_COLLECTED
