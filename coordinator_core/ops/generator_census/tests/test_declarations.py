"""Tests for coordinator_core.ops.generator_census.declarations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.generator_census.declarations import (
    DECLARATION_NAMES,
    MALFORMED,
    extract,
    owned_constants,
)

_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("name", DECLARATION_NAMES)
def test_each_name(name):
    assert extract(f'{name} = ["a/b.md"]\n') == {name: ["a/b.md"]}


def test_annotated_form():
    assert extract('MUTATES: list[str] = ["x/*.md"]\n') == {"MUTATES": ["x/*.md"]}


def test_multiline_list():
    src = 'MUTATES = [\n    "a/*.md",\n    "b/*.md",\n]\nimport os\n'
    assert extract(src) == {"MUTATES": ["a/*.md", "b/*.md"]}


def test_crlf_equals_lf():
    src = 'GENERATES = [\n    "a",\n]\nMUTATES = ["b/*.md"]\n'
    assert extract(src.replace("\n", "\r\n")) == extract(src) == {
        "GENERATES": ["a"],
        "MUTATES": ["b/*.md"],
    }


def test_indented_assignment_ignored():
    assert extract('def f():\n    MUTATES = ["a"]\n') == {}


@pytest.mark.parametrize("q", ['"""', "'''"])
def test_column0_inside_triple_quoted_string_ignored(q):
    src = f'FIXTURE = {q}\nMUTATES = ["a/*.md"]\n{q}\n'
    assert extract(src) == {}


def test_real_declaration_after_string_still_found():
    src = 'X = """\nMUTATES = ["no"]\n"""\nMUTATES = ["yes"]\n'
    assert extract(src) == {"MUTATES": ["yes"]}


def test_non_literal_call_is_malformed():
    assert extract("MUTATES = compute()\n") == {"MUTATES": MALFORMED}


def test_unparseable_statement_is_malformed():
    assert extract("MUTATES = [\n") == {"MUTATES": MALFORMED}


def test_name_and_fstring_folding_over_own_constants():
    src = 'BASE = "state/x"\nMUTATES = [f"{BASE}/*.md", BASE + "/y"]\n'
    assert extract(src) == {"MUTATES": ["state/x/*.md", "state/x/y"]}


def test_memo_modules_fold_over_machinery_paths():
    oracle = json.loads(
        (_ROOT / "coordinator_core/ops/generator_census/tests/fixtures/census_oracle.json").read_text(
            encoding="utf-8"
        )
    )
    owned = owned_constants(
        (_ROOT / "coordinator_core/session/machinery_paths.py").read_text(encoding="utf-8")
    )
    expected = {
        r["generator"]: r
        for r in oracle
        if r["generator"].startswith("coordinator_core/ops/fleet/memo_")
        and r["generator"].rsplit("/", 1)[1]
        in ("memo_compose.py", "memo_draft.py", "memo_heal.py", "memo_reconcile_outbox.py")
    }
    assert len(expected) == 4
    for path, rec in expected.items():
        got = extract((_ROOT / path).read_text(encoding="utf-8"), owned)
        assert got["MUTATES"] == rec["mutates"], path
