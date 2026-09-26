from __future__ import annotations

import os

import pytest

_REPO_ROOT_MARKERS = ("pyproject.toml",)


def _find_repo_root(start: str) -> "str | None":
    current = start
    while True:
        if any(os.path.isfile(os.path.join(current, marker)) for marker in _REPO_ROOT_MARKERS):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


@pytest.fixture(autouse=True)
def _no_new_repo_root_entries():
    repo_root = _find_repo_root(os.path.dirname(os.path.abspath(__file__)))
    if repo_root is None:
        yield
        return
    try:
        before = set(os.listdir(repo_root))
    except OSError:
        yield
        return
    yield
    try:
        after = set(os.listdir(repo_root))
    except OSError:
        return
    new_entries = after - before
    assert not new_entries, (
        "test left new untracked entries at the repo root: "
        f"{sorted(new_entries)!r} — a test flipping os.name process-wide "
        "(or any other cause) can make a relative-path write land here "
        "instead of a tmp_path. See this file's module docstring."
    )
