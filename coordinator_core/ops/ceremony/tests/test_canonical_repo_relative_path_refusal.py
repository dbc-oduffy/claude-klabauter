
from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import git_native

pytestmark = pytest.mark.cadence


@pytest.mark.parametrize(
    "path",
    [
        "a/../b",
        "./a",
        "a//b",
        "/abs",
        ".git/config",
        "a/.GIT/x",
    ],
)
def test_refuses_non_canonical_paths(path: str) -> None:
    refusal = git_native.canonical_repo_relative_path_refusal(path)
    assert refusal is not None, f"expected a refusal for {path!r}"
    assert path in refusal


def test_accepts_canonical_path() -> None:
    assert git_native.canonical_repo_relative_path_refusal("a/b.py") is None


def test_first_non_canonical_path_refusal_reports_the_offender() -> None:
    refusal = git_native.first_non_canonical_path_refusal(["a/b.py", "a/../b", "c/d.py"])
    assert refusal is not None
    assert "a/../b" in refusal


def test_first_non_canonical_path_refusal_none_when_all_canonical() -> None:
    assert git_native.first_non_canonical_path_refusal(["a/b.py", "c/d.py"]) is None
