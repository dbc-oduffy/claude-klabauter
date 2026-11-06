"""since_base_overlap: a landing that rewrites or extends a sibling's added line in place modified it, not dropped it.

Repos are written in-process with ``git_objects`` (no spawn).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from coordinator_core.git import since_base_overlap
from coordinator_core.git.tests.test_since_base_overlap import PATH, _join, _setup
from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP

_FOREIGN = "foreign_change = 1\n"
_INSERT_AT = 40


def _foreign_head(tmp_path: Path, added: List[str]):
    """A repo whose HEAD is the base plus ``added`` inserted by a foreign commit; returns (repo, base, head_lines)."""
    repo, base, base_lines = _setup(tmp_path)
    head_lines = list(base_lines)
    head_lines[_INSERT_AT:_INSERT_AT] = added
    repo.commit({PATH: _join(head_lines)}, "foreign terminal commit")
    return repo, base, head_lines


def _land_replacing(repo, head_lines: List[str], count: int, replacement: List[str]) -> None:
    landed = list(head_lines)
    landed[_INSERT_AT : _INSERT_AT + count] = replacement
    repo.land(_join(landed) + "own_edit = 2\n")


def _check(tmp_path: Path, base: str):
    return since_base_overlap.check(tmp_path, [PATH], base, run_id=None)


def test_rewriting_a_foreign_line_in_place_is_a_modification_not_a_drop(tmp_path: Path) -> None:
    repo, base, head_lines = _foreign_head(tmp_path, [_FOREIGN])
    _land_replacing(repo, head_lines, 1, ["foreign_change = 2\n"])

    assert _check(tmp_path, base) == ([], [])


def test_extending_a_foreign_line_into_more_lines_is_a_modification_not_a_drop(tmp_path: Path) -> None:
    repo, base, head_lines = _foreign_head(tmp_path, [_FOREIGN])
    _land_replacing(repo, head_lines, 1, ["foreign_change = 2\n", "foreign_change_more = 2\n"])

    assert _check(tmp_path, base) == ([], [])


def test_deleting_a_foreign_line_is_still_a_drop(tmp_path: Path) -> None:
    repo, base, head_lines = _foreign_head(tmp_path, [_FOREIGN])
    _land_replacing(repo, head_lines, 1, [])

    findings, notes = _check(tmp_path, base)

    assert notes == []
    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]


def test_replacing_two_foreign_lines_with_one_still_drops_the_surplus(tmp_path: Path) -> None:
    repo, base, head_lines = _foreign_head(tmp_path, ["foreign_a = 1\n", "foreign_b = 1\n"])
    _land_replacing(repo, head_lines, 2, ["foreign_a = 2\n"])

    findings, notes = _check(tmp_path, base)

    assert notes == []
    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]
