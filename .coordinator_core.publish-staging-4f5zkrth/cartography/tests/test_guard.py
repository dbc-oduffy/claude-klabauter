"""
coordinator_core.cartography.tests.test_guard

Unit tests for coordinator_core.cartography._guard.path_guard — the shared
target_root containment helper every cartography primitive (tree,
file_index, churn, symbols, edges) imports before touching a
caller-supplied path.

Coverage:
  (a) accepts a relative path that resolves inside target_root
  (b) accepts an absolute path that resolves inside target_root
  (c) rejects a relative '../' escape outside target_root
  (d) rejects an absolute path outside target_root
  (e) symlink escape is caught (resolves outside target_root -> raises)
  (f) nested subdirectory candidate is accepted

Spec backlink: pln-makima-cartography-substrate-a-26eb2e
§ chunk C2 (package foundation + containment helper).
"""

from __future__ import annotations

import pytest

from coordinator_core.cartography._guard import PathEscapeError, path_guard


def test_path_guard_accepts_relative_path_in_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file.py").write_text("x", encoding="utf-8")

    result = path_guard(root, "file.py")
    assert result == (root / "file.py").resolve()


def test_path_guard_accepts_absolute_path_in_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    candidate = root / "file.py"
    candidate.write_text("x", encoding="utf-8")

    result = path_guard(root, candidate)
    assert result == candidate.resolve()


def test_path_guard_rejects_relative_traversal_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(PathEscapeError):
        path_guard(root, "../secret.py")


def test_path_guard_rejects_absolute_path_outside_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "secret.py"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(PathEscapeError):
        path_guard(root, outside)


def test_path_guard_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside_target = tmp_path / "elsewhere" / "secret.py"
    outside_target.parent.mkdir()
    outside_target.write_text("secret", encoding="utf-8")

    symlink_path = root / "escape.py"
    try:
        symlink_path.symlink_to(outside_target)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(PathEscapeError):
        path_guard(root, "escape.py")


def test_path_guard_accepts_nested_subdirectory_path(tmp_path):
    root = tmp_path / "repo"
    nested = root / "pkg" / "sub"
    nested.mkdir(parents=True)
    candidate = nested / "mod.py"
    candidate.write_text("x", encoding="utf-8")

    result = path_guard(root, "pkg/sub/mod.py")
    assert result == candidate.resolve()
