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
