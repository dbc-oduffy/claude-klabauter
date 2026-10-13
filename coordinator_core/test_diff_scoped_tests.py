
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.diff_scoped_tests import (
    PYTEST_NO_TESTS_COLLECTED,
    STANDING_GUARDS,
    append_test_paths,
    compute_diff_scoped_paths,
    find_changed_source_files,
    find_changed_test_files,
)
from coordinator_core.win_portability import no_console_creationflags

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


def test_pytest_no_tests_collected_constant_matches_pytest_contract():
    assert PYTEST_NO_TESTS_COLLECTED == 5


def test_rc5_reproduces_against_real_pytest(tmp_path):
    pytest_bin = pytest.importorskip("pytest")
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


def _init_repo_with_mappable_source(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)

    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["pkg"]\n'
    )
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("x = 1\n")
    (pkg / "test_mod.py").write_text("def test_a():\n    assert True\n")
    (pkg / "unmappable.py").write_text("y = 2\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)
    return tmp_path


def test_find_changed_source_files_excludes_test_files(tmp_path):
    root = _init_repo_with_mappable_source(tmp_path)
    (root / "pkg" / "mod.py").write_text("x = 2\n")

    changed = find_changed_source_files(str(root))
    assert changed == ["pkg/mod.py"]


def test_compute_diff_scoped_paths_source_only_diff_maps_to_covering_test(tmp_path):
    root = _init_repo_with_mappable_source(tmp_path)
    (root / "pkg" / "mod.py").write_text("x = 2\n")

    paths, fully_mapped = compute_diff_scoped_paths(str(root))
    assert paths == ["pkg/test_mod.py"]
    assert fully_mapped is True


def test_compute_diff_scoped_paths_unions_changed_test_and_mapped_source(tmp_path):
    root = _init_repo_with_mappable_source(tmp_path)
    (root / "pkg" / "mod.py").write_text("x = 2\n")
    (root / "pkg" / "test_existing_direct.py").write_text(
        "def test_b():\n    assert True\n"
    )
    _git(["add", "-A"], root)

    paths, fully_mapped = compute_diff_scoped_paths(str(root))
    assert paths == ["pkg/test_existing_direct.py", "pkg/test_mod.py"]
    assert fully_mapped is True


def test_compute_diff_scoped_paths_unmapped_source_forces_full_tier_signal(tmp_path):
    root = _init_repo_with_mappable_source(tmp_path)
    (root / "pkg" / "unmappable.py").write_text("y = 3\n")

    paths, fully_mapped = compute_diff_scoped_paths(str(root))
    assert fully_mapped is False


def test_compute_diff_scoped_paths_no_changes_is_fully_mapped_empty(tmp_path):
    root = _init_repo_with_mappable_source(tmp_path)

    paths, fully_mapped = compute_diff_scoped_paths(str(root))
    assert paths == []
    assert fully_mapped is True


_GUARD = STANDING_GUARDS[0][1]


def _init_repo_with_guard_prefix(tmp_path, with_guard):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["coordinator_core", "other"]\n'
    )
    (tmp_path / "coordinator_core").mkdir()
    (tmp_path / "coordinator_core" / "mod.py").write_text("x = 1\n")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "mod.py").write_text("x = 1\n")
    if with_guard:
        guard = tmp_path / _GUARD
        guard.parent.mkdir(parents=True)
        guard.write_text("def test_g():\n    assert True\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    return tmp_path


def test_source_edit_under_guard_prefix_selects_guard(tmp_path):
    root = _init_repo_with_guard_prefix(tmp_path, with_guard=True)
    for _prefix, test_path in STANDING_GUARDS:
        guard = root / test_path
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_text("def test_g():\n    assert True\n")
    (root / "coordinator_core" / "mod.py").write_text("x = 2\n")

    paths, _ = compute_diff_scoped_paths(str(root))
    for _prefix, test_path in STANDING_GUARDS:
        assert test_path in paths


def test_absent_guard_file_is_never_selected(tmp_path):
    root = _init_repo_with_guard_prefix(tmp_path, with_guard=False)
    (root / "coordinator_core" / "mod.py").write_text("x = 2\n")

    paths, _ = compute_diff_scoped_paths(str(root))
    assert _GUARD not in paths


def test_source_edit_under_other_root_does_not_select_guard(tmp_path):
    root = _init_repo_with_guard_prefix(tmp_path, with_guard=True)
    (root / "other" / "mod.py").write_text("x = 2\n")

    paths, _ = compute_diff_scoped_paths(str(root))
    assert _GUARD not in paths


def test_standing_guard_leaves_fully_mapped_unchanged(tmp_path):
    from coordinator_core.source_test_map import map_changed_sources

    root = _init_repo_with_guard_prefix(tmp_path, with_guard=True)
    (root / "coordinator_core" / "mod.py").write_text("x = 2\n")

    _, fully_mapped = compute_diff_scoped_paths(str(root))
    _, expected = map_changed_sources(
        find_changed_source_files(str(root)), str(root)
    )
    assert fully_mapped is expected


def test_every_standing_guard_test_path_is_a_repo_file():
    repo = Path(__file__).resolve().parent.parent
    for _prefix, test_path in STANDING_GUARDS:
        assert (repo / test_path).is_file(), test_path
