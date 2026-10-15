"""Tests for the shared repo-relativizer."""

from __future__ import annotations

import os

import pytest

from coordinator_core.repo_relative_text import relativize_text, to_repo_relative


def test_to_repo_relative_inside_and_outside(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    assert to_repo_relative(str(root / "docs" / "a.md"), str(root)) == "docs/a.md"
    assert to_repo_relative(str(tmp_path / "other" / "a.md"), str(root)) is None
    assert to_repo_relative(str(tmp_path / "repo2" / "a.md"), str(root)) is None


def test_to_repo_relative_backslash_windows_root():
    # abs-path-ok: Windows-shaped fixture
    assert to_repo_relative("C:\\Work\\repo\\docs\\a.md", "c:/work/repo") == "docs/a.md"
    # abs-path-ok: Windows-shaped fixture
    assert to_repo_relative("D:\\x\\a.md", "C:\\Work\\repo") is None


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_to_repo_relative_symlinked_root(tmp_path):
    real = tmp_path / "real"
    (real / "docs").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert to_repo_relative(str(real / "docs" / "a.md"), str(link)) == "docs/a.md"


def test_relativize_text_repo_root_and_replacement():
    text = "see /w/repo/docs/y.md and /w/repo/scratch/x.md"
    out = relativize_text(text, [("/w/repo", ""), ("/w/repo/scratch", "docs/research/run")])
    assert out == "see docs/y.md and docs/research/run/x.md"


def test_relativize_text_longest_root_wins_regardless_of_order():
    roots = [("/w/repo/scratch", "arch"), ("/w/repo", "")]
    assert relativize_text("/w/repo/scratch/x /w/repo/y", roots) == "arch/x y"
    assert relativize_text("/w/repo/scratch/x /w/repo/y", roots[::-1]) == "arch/x y"


def test_relativize_text_matches_both_separators_and_windows_case():
    # abs-path-ok: Windows-shaped fixture
    out = relativize_text("C:\\Work\\Repo\\docs\\a.md and c:/work/repo/b.md", [("C:/work/repo", "")])
    assert out == "docs\\a.md and b.md"


def test_relativize_text_leaves_outside_paths_byte_identical():
    text = "/elsewhere/repo/x.md /w/repo2/x.md /w/repo and /prefix/w/repo/x.md"
    assert relativize_text(text, [("/w/repo", "")]) == text


def test_relativize_text_trailing_slash_root_and_empty_inputs():
    assert relativize_text("/w/repo/a", [("/w/repo/", "")]) == "a"
    assert relativize_text("", [("/w/repo", "")]) == ""
    assert relativize_text("/w/repo/a", []) == "/w/repo/a"
    assert relativize_text("/a", [("", "")]) == "/a"


def test_relativize_text_regex_metacharacters_in_root():
    assert relativize_text("/w/re+po (1)/a", [("/w/re+po (1)", "")]) == "a"
