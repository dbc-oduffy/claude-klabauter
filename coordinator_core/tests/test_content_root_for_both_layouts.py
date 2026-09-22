"""The shared `content_root_for` primitive's own contract.

See `coordinator_core._content_root_primitive.content_root_for`'s docstring
for the systemic defect these arms pin against (overengineering-reviewer
finding 7 — one owning passage, cited here). Twin parity with the bin/ side
is owned by `coordinator/bin/tests/test_claude_doe_content_root_parity.py`
(finding 5).
"""
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
    # The arm that was missing everywhere. A cloud container registers exactly
    # this shape and sets `repos.doe_claude` to it.
    root = _flat_mirror(tmp_path / "coordinator-claude")
    assert content_root_for(str(root)) == root


def test_the_private_layout_wins_when_a_root_somehow_carries_both(tmp_path):
    # Order is load-bearing: probing private first is what makes this widen
    # nothing for the callers that already worked.
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


# A degenerate all-slash root used to collapse
# via rstrip("/\\") to "", and Path("") resolves to the process cwd, so this
# silently probed cwd instead of failing closed on "/" or "//". Pinned here
# so the fix (fall back to the un-stripped string when stripping empties it)
# stays load-bearing.
@pytest.mark.parametrize("degenerate", ["/", "//"])
def test_a_degenerate_all_slash_root_fails_closed_not_cwd(degenerate):
    assert content_root_for(degenerate) is None


def test_a_symlinked_content_root_still_resolves(tmp_path):
    real = _private_tree(tmp_path / "real-DoE-claude")
    link = tmp_path / "linked-DoE-claude"
    link.symlink_to(real)
    assert content_root_for(str(link)) == link / "coordinator"


# Twin parity (bin/ twin, and the third claude-doe.py inline copy) is owned
# entirely by `coordinator/bin/tests/test_claude_doe_content_root_parity.py`
# (overengineering-reviewer finding 5): its three-way property is a strict
# superset of what used to be asserted here two-way, and collapsing to one
# owner also removes the reason this coordinator_core test reached sideways
# into `coordinator/bin/lib` via a `sys.path` insert.
