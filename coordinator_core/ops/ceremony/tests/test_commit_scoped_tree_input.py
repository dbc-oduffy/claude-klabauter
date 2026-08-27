"""
coordinator_core.ops.ceremony.tests.test_commit_scoped_tree_input

Tests for `git_native._assemble_commit_tree_input()` -- the tree-input
assembler for the multi-path, in-process (spine-rewrite) commit arm.
C8a, docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md.

Table-driven over the source-resolution matrix (`_SOURCE_STAGED` /
`_SOURCE_WORKTREE` / `_SOURCE_SUPPLIED`), mode-precedence ladder
(index -> HEAD spine -> `_SUPPLIED_BLOB_MODE`), the staged-deletion ABSENT
trap, and case-divergence refusal. Needs no git and no repo at all -- every
input is a plain in-memory parameter, which is the entire point of the
split (see the module docstring on `_assemble_commit_tree_input` for why).
"""

from __future__ import annotations

import pytest

from coordinator_core.git.git_state import IndexEntry
from coordinator_core.ops.ceremony.git_native import (
    _SOURCE_STAGED,
    _SOURCE_SUPPLIED,
    _SOURCE_WORKTREE,
    _SUPPLIED_BLOB_MODE,
    _assemble_commit_tree_input,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


def test_staged_path_resolves_verbatim_from_index_snapshot():
    resolution = {"foo.txt": _SOURCE_STAGED}
    index_snapshot = {"foo.txt": IndexEntry(mode=0o100644, sha=_SHA_A, stage=0)}

    tree_input, absent = _assemble_commit_tree_input(
        resolution, index_snapshot=index_snapshot, head_spine=None
    )

    assert tree_input == {"foo.txt": ("100644", _SHA_A)}
    assert absent == set()


def test_staged_deletion_lands_in_absent_set_not_tree_input():
    # Path resolves _SOURCE_STAGED (diverged) but has no index entry --
    # staged for deletion. Must be explicit ABSENT, never silently dropped
    # and never resurrected via a HEAD spine fallback.
    resolution = {"gone.txt": _SOURCE_STAGED}
    index_snapshot: dict = {}
    head_spine = {"": {"gone.txt": (0o100644, _SHA_A)}}

    tree_input, absent = _assemble_commit_tree_input(
        resolution, index_snapshot=index_snapshot, head_spine=head_spine
    )

    assert tree_input == {}
    assert absent == {"gone.txt"}


def test_worktree_deleted_path_is_absent_even_with_a_live_index_entry():
    # The sibling above infers the deletion from a MISSING index entry, which
    # is right when the index is the authority. It is wrong for a path deleted
    # on disk whose index entry still stands: that entry gets written back and
    # the caller's requested removal silently does not happen. `worktree_deleted`
    # is the caller stating the deletion rather than leaving it to be inferred.
    resolution = {"gone.txt": _SOURCE_STAGED}
    index_snapshot = {"gone.txt": IndexEntry(mode=0o100644, sha=_SHA_A, stage=0)}
    head_spine = {"": {"gone.txt": (0o100644, _SHA_A)}}

    tree_input, absent = _assemble_commit_tree_input(
        resolution,
        index_snapshot=index_snapshot,
        head_spine=head_spine,
        worktree_deleted={"gone.txt"},
    )

    assert tree_input == {}
    assert absent == {"gone.txt"}


def test_worktree_deleted_default_leaves_the_index_entry_authoritative():
    # Same inputs, no `worktree_deleted` -- prior behaviour exactly, so the
    # parameter is additive and no existing caller changes shape.
    resolution = {"gone.txt": _SOURCE_STAGED}
    index_snapshot = {"gone.txt": IndexEntry(mode=0o100644, sha=_SHA_A, stage=0)}

    tree_input, absent = _assemble_commit_tree_input(
        resolution, index_snapshot=index_snapshot, head_spine=None
    )

    assert tree_input == {"gone.txt": ("100644", _SHA_A)}
    assert absent == set()


def test_worktree_path_mode_prefers_index_over_head_spine():
    resolution = {"foo.txt": _SOURCE_WORKTREE}
    index_snapshot = {"foo.txt": IndexEntry(mode=0o100755, sha=_SHA_A, stage=0)}
    head_spine = {"": {"foo.txt": (0o100644, _SHA_B)}}

    tree_input, absent = _assemble_commit_tree_input(
        resolution,
        index_snapshot=index_snapshot,
        head_spine=head_spine,
        worktree_blobs={"foo.txt": _SHA_C},
    )

    # sha comes from the caller-supplied worktree blob, mode from the index
    # entry (higher precedence than the HEAD spine).
    assert tree_input == {"foo.txt": ("100755", _SHA_C)}
    assert absent == set()


def test_worktree_path_mode_falls_back_to_head_spine_when_absent_from_index():
    resolution = {"new.txt": _SOURCE_WORKTREE}
    index_snapshot: dict = {}
    head_spine = {"": {"new.txt": (0o100755, _SHA_A)}}

    tree_input, _ = _assemble_commit_tree_input(
        resolution,
        index_snapshot=index_snapshot,
        head_spine=head_spine,
        worktree_blobs={"new.txt": _SHA_B},
    )

    assert tree_input == {"new.txt": ("100755", _SHA_B)}


def test_worktree_path_mode_falls_back_to_supplied_blob_mode_when_nowhere_else():
    resolution = {"brand_new.txt": _SOURCE_WORKTREE}

    tree_input, _ = _assemble_commit_tree_input(
        resolution,
        index_snapshot={},
        head_spine=None,
        worktree_blobs={"brand_new.txt": _SHA_A},
    )

    assert tree_input == {"brand_new.txt": (_SUPPLIED_BLOB_MODE, _SHA_A)}


def test_worktree_path_missing_from_worktree_blobs_raises():
    resolution = {"foo.txt": _SOURCE_WORKTREE}

    with pytest.raises(ValueError, match="worktree_blobs"):
        _assemble_commit_tree_input(
            resolution, index_snapshot={}, head_spine=None, worktree_blobs={}
        )


def test_supplied_path_uses_supplied_blob_and_resolved_mode():
    resolution = {"patched.txt": _SOURCE_SUPPLIED}
    index_snapshot = {"patched.txt": IndexEntry(mode=0o100755, sha=_SHA_A, stage=0)}

    tree_input, _ = _assemble_commit_tree_input(
        resolution,
        index_snapshot=index_snapshot,
        head_spine=None,
        supplied_blobs={"patched.txt": _SHA_B},
    )

    assert tree_input == {"patched.txt": ("100755", _SHA_B)}


def test_supplied_path_missing_from_supplied_blobs_raises():
    resolution = {"patched.txt": _SOURCE_SUPPLIED}

    with pytest.raises(ValueError, match="supplied_blobs"):
        _assemble_commit_tree_input(
            resolution, index_snapshot={}, head_spine=None, supplied_blobs={}
        )


def test_nested_path_mode_falls_back_to_head_spine_directory_entry():
    resolution = {"dir/sub/new.txt": _SOURCE_WORKTREE}
    head_spine = {"dir/sub": {"new.txt": (0o100755, _SHA_A)}}

    tree_input, _ = _assemble_commit_tree_input(
        resolution,
        index_snapshot={},
        head_spine=head_spine,
        worktree_blobs={"dir/sub/new.txt": _SHA_B},
    )

    assert tree_input == {"dir/sub/new.txt": ("100755", _SHA_B)}


def test_case_divergent_index_key_refuses_rather_than_guesses():
    resolution = {"File.txt": _SOURCE_STAGED}
    index_snapshot = {"file.txt": IndexEntry(mode=0o100644, sha=_SHA_A, stage=0)}

    with pytest.raises(ValueError, match="case-divergent"):
        _assemble_commit_tree_input(
            resolution, index_snapshot=index_snapshot, head_spine=None
        )


def test_case_divergent_head_spine_key_refuses_rather_than_guesses():
    resolution = {"File.txt": _SOURCE_WORKTREE}
    head_spine = {"": {"file.txt": (0o100644, _SHA_A)}}

    with pytest.raises(ValueError, match="case-divergent"):
        _assemble_commit_tree_input(
            resolution,
            index_snapshot={},
            head_spine=head_spine,
            worktree_blobs={"File.txt": _SHA_B},
        )


def test_unknown_source_value_raises():
    resolution = {"foo.txt": "not-a-real-source"}

    with pytest.raises(ValueError, match="unresolved/unknown source"):
        _assemble_commit_tree_input(resolution, index_snapshot={}, head_spine=None)


def test_mixed_multi_path_resolution_end_to_end():
    resolution = {
        "staged.txt": _SOURCE_STAGED,
        "deleted.txt": _SOURCE_STAGED,
        "worktree.txt": _SOURCE_WORKTREE,
        "patched.txt": _SOURCE_SUPPLIED,
    }
    index_snapshot = {
        "staged.txt": IndexEntry(mode=0o100644, sha=_SHA_A, stage=0),
        "worktree.txt": IndexEntry(mode=0o100755, sha=_SHA_B, stage=0),
    }

    tree_input, absent = _assemble_commit_tree_input(
        resolution,
        index_snapshot=index_snapshot,
        head_spine=None,
        worktree_blobs={"worktree.txt": _SHA_C},
        supplied_blobs={"patched.txt": _SHA_A},
    )

    assert tree_input == {
        "staged.txt": ("100644", _SHA_A),
        "worktree.txt": ("100755", _SHA_C),
        "patched.txt": (_SUPPLIED_BLOB_MODE, _SHA_A),
    }
    assert absent == {"deleted.txt"}
