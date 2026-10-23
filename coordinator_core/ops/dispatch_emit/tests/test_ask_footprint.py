"""ask_footprint: the footprint-owed truth table and the leg's prompt and schema."""

import json

import pytest

from coordinator_core.ops.dispatch_emit.ask_footprint import (
    ADMIT_WRITE_PATTERN,
    FOOTPRINT_SCHEMA,
    admit_writes_js,
    declared_footprint,
    footprint_owed,
    footprint_prompt,
    is_admitted_write,
)
from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_M_PLUS, ARM_S, ARM_XS


def test_owed_for_xs_without_writes_not_sized_in_run():
    assert footprint_owed(arm=ARM_XS, writes=[], sized_in_run=False) is True


@pytest.mark.parametrize(
    "arm,writes,sized",
    [
        (ARM_XS, ["a.py"], False),
        (ARM_XS, [], True),
        (ARM_S, [], False),
        (ARM_M_PLUS, [], False),
        (None, [], False),
    ],
)
def test_not_owed_otherwise(arm, writes, sized):
    assert footprint_owed(arm=arm, writes=writes, sized_in_run=sized) is False


def test_prompt_names_sizing_and_baton():
    p = footprint_prompt(sizing_rel="state/sizings/s.yaml", baton_path="state/handoffs/b.md")
    assert "state/sizings/s.yaml" in p and "state/handoffs/b.md" in p


@pytest.mark.parametrize(
    "path",
    ["a.py", "coordinator_core/ops/x.py", "docs/plans/a-b.md", ".coordinator-local/x/C1.md", "a..b.py", "dir/..hidden"],
)
def test_admits_repo_relative_files(path):
    assert is_admitted_write(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "", " ", "/etc/passwd", "\\srv\\x", "C" + ":\\x\\y.py", "c" + ":/x/y.py", "~/x.py", "a\\b.py",
        "../x.py", "a/../b.py", "a/..", "..", "src/*.py", "src/**/x.py", "a?.py", "a[1].py", "{a,b}.py",
        "src/", "a/b/", "a\nb.py", "a.py\n", "a.py\r", "a.py ", ".", None, 3, ["a.py"],
    ],
)
def test_rejects_unsafe_or_non_file_writes(path):
    assert is_admitted_write(path) is False


def test_js_predicate_embeds_the_same_pattern():
    js = admit_writes_js()
    assert js.startswith("((w) => typeof w === 'string'")
    assert json.dumps(ADMIT_WRITE_PATTERN) in js


def test_prompt_omits_baton_clause_when_none():
    p = footprint_prompt(sizing_rel="state/sizings/s.yaml", baton_path=None)
    assert "baton" not in p.lower()


_BATON = """# Baton

## What this covers
Edits `coordinator_core/a.py` and `tests/test_a.py::test_x`.

## Notes
Mentions `coordinator_core/ignored.py` outside the named sections.

## Specification
Again `coordinator_core/a.py:12-20`, plus `ledger_remaining_paths` (no extension).
"""


def test_declared_footprint_returns_both_paths_once():
    assert declared_footprint(_BATON) == ("coordinator_core/a.py", "tests/test_a.py")


def test_declared_footprint_empty_when_none_named():
    assert declared_footprint("## Specification\nNo paths, only `a_symbol`.\n") == ()
    assert declared_footprint("") == ()


@pytest.mark.parametrize("bad", ["../x.py", "a/../x.py", "/etc/x.py", "C:/x.py", "scratch/x.py"])  # abs-path-ok: refusal fixture
def test_declared_footprint_refuses_unsafe_paths(bad):
    assert declared_footprint(f"## Acceptance criteria\n`{bad}` and `ok/y.py`\n") == ("ok/y.py",)


def test_schema_requires_writes():
    assert FOOTPRINT_SCHEMA["required"] == ["writes"]
    assert "writes" in FOOTPRINT_SCHEMA["properties"]
