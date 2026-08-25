"""Regression pin for `_parent_dir`'s cross-platform normalization.

`_parent_dir` operates on a recorded frontmatter `path` field, not a live
filesystem path on this host -- the field may have been authored on either
platform. Pins that a backslash-separated (Windows-authored) value resolves
correctly on a POSIX host, guarding against a regression to
`os.path.dirname()` on the raw string (which silently drops the parent
directory for a backslash-separated value on POSIX -- see
`docs/reference/posix-portability-fix-vs-carveout.md`, C7 disposition for
this file).

Spec: docs/plans/2026-08-13-grind-the-posix-exec-baseline-to-zero.md § C7
"""
from __future__ import annotations

from coordinator_core.clustering.candidates import _parent_dir


def test_parent_dir_forward_slash() -> None:
    assert _parent_dir("state/handoffs/2026-08-13-foo.md") == "state/handoffs"


def test_parent_dir_backslash_authored() -> None:
    assert _parent_dir("state\\handoffs\\2026-08-13-foo.md") == "state/handoffs"


def test_parent_dir_mixed_separators() -> None:
    assert _parent_dir("state/handoffs\\2026-08-13-foo.md") == "state/handoffs"


def test_parent_dir_no_separator() -> None:
    assert _parent_dir("README.md") == ""


def test_parent_dir_empty_string() -> None:
    assert _parent_dir("") == ""
