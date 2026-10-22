"""census_operands: the moved operand parsers give the operands the revalidator keyed by."""

from __future__ import annotations

import pytest

from coordinator_core.roadmap import census_operands as ops


@pytest.mark.parametrize(
    "command, expected",
    [
        ("grep -c foo docs/a.md", ["docs/a.md"]),
        ("grep -rn foo docs | wc -l", ["docs"]),
        ("grep -e foo -e bar a.md b.md", None),
        ("find docs -name '*.md' | wc -l", ["docs"]),
        ("ls -1 state", ["state"]),
        ("cat ./x/y.md", ["x/y.md"]),
        ("grep foo /abs/path", []),
        ("grep foo ../up", []),
        ("grep foo a.md 2>/dev/null", ["a.md"]),
        ("git log --oneline | wc -l", []),
    ],
)
def test_command_operands(command, expected):
    got = ops.command_operands(command)
    if expected is None:
        assert got in ([], None)
    else:
        assert got == expected


def test_stage_operands_unbounded_and_bounded():
    assert ops.stage_operands("grep", ["-r", "foo"], True) is None
    assert ops.stage_operands("grep", ["foo", "a", "b"], True) == ["a", "b"]
    assert ops.stage_operands("find", ["-name", "x"], True) is None
    assert ops.stage_operands("ls", ["-l", "d"], True) is None
    assert ops.stage_operands("ls", ["-1", "d"], True) == ["d"]


def test_operand_rel_shapes():
    assert ops.operand_rel("./a//b") == "a/b"
    assert ops.operand_rel(".") is None
    assert ops.operand_rel("~/x") is None
    assert ops.operand_rel("a/.git/b") is None


def test_live_state_roots_pinned():
    assert ops.LIVE_STATE_ROOTS == (".coordinator-local", "scratch", "state/mise-workflows")
