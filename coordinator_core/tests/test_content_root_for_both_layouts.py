from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.data_root import content_root_for


def _private_tree(root: Path) -> Path:
    (root / "coordinator").mkdir(parents=True)
    return root


def _flat_mirror(root: Path) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}")
    return root


def test_the_private_authoring_tree_resolves_to_its_coordinator_subdir(tmp_path):
    root = _private_tree(tmp_path / "DoE-claude")
    assert content_root_for(str(root)) == root / "coordinator"


def test_the_published_flat_mirror_resolves_to_its_own_root(tmp_path):
    root = _flat_mirror(tmp_path / "coordinator-claude")
    assert content_root_for(str(root)) == root


def test_the_private_layout_wins_when_a_root_somehow_carries_both(tmp_path):
    root = _flat_mirror(_private_tree(tmp_path / "both"))
    assert content_root_for(str(root)) == root / "coordinator"


def test_a_bare_directory_is_not_a_content_root(tmp_path):
    (tmp_path / "bare").mkdir()
    assert content_root_for(str(tmp_path / "bare")) is None


def test_a_flat_directory_without_its_plugin_manifest_is_not_a_content_root(tmp_path):
    root = tmp_path / "unmarked"
    (root / ".claude-plugin").mkdir(parents=True)
    assert content_root_for(str(root)) is None


@pytest.mark.parametrize("empty", ["", None])
def test_an_unresolved_doe_root_is_none_never_a_raise(empty):
    assert content_root_for(empty) is None


def test_a_path_argument_is_accepted_as_well_as_a_string(tmp_path):
    root = _private_tree(tmp_path / "DoE-claude")
    assert content_root_for(root) == root / "coordinator"


def test_a_trailing_separator_does_not_defeat_the_probe(tmp_path):
    root = _private_tree(tmp_path / "DoE-claude")
    assert content_root_for(str(root) + os.sep) == root / "coordinator"


@pytest.mark.parametrize("degenerate", ["/", "//"])
def test_a_degenerate_all_slash_root_fails_closed_not_cwd(degenerate):
    assert content_root_for(degenerate) is None


def test_a_symlinked_content_root_still_resolves(tmp_path):
    real = _private_tree(tmp_path / "real-DoE-claude")
    link = tmp_path / "linked-DoE-claude"
    link.symlink_to(real)
    assert content_root_for(str(link)) == link / "coordinator"

