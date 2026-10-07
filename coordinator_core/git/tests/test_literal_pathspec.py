"""git_pathspec keeps a bracket-bearing literal path literal and leaves globs and magic alone."""

import pytest

from coordinator_core.git.literal_pathspec import git_pathspec, is_magic_pathspec


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("src/app/[entityId]/route.ts", ":(literal)src/app/[entityId]/route.ts"),
        ("a/[[...slug]]/page.tsx", ":(literal)a/[[...slug]]/page.tsx"),
        ("src/*.py", "src/*.py"),
        ("src/[id]/*.ts", "src/[id]/*.ts"),
        (":(glob)src/**", ":(glob)src/**"),
        ("plain/path.py", "plain/path.py"),
    ],
)
def test_git_pathspec(entry, expected):
    assert git_pathspec(entry) == expected


def test_bracket_alone_is_not_magic():
    assert not is_magic_pathspec("a/[id]/x.ts")
    assert is_magic_pathspec("a/*.ts")
