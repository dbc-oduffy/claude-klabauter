"""Pins for the dispatch.land_run caller-facing contract."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run_contract as c

_HALTED = {"halted_by": {"halted": "commit"}}
_FANOUT = {"fanout": {"plans": ["docs/plans/a.md"]}}
_GRIND = {"grind": {"rows": []}}


@pytest.mark.parametrize(
    "params,mode",
    [
        ({}, "product"),
        ({"plan_path": "p.md"}, "product"),
        (_HALTED, "halted"),
        (_FANOUT, "fanout"),
        (_GRIND, "grind"),
        ({"halted_by": {"halted": ""}}, "product"),
        ({"fanout": {"plans": []}}, "product"),
        (None, "product"),
    ],
)
def test_mode_of_shapes(params, mode):
    assert c.mode_of(params) == mode


@pytest.mark.parametrize("params", [{**_HALTED, **_FANOUT}, {**_HALTED, **_GRIND}, {**_FANOUT, **_GRIND}])
def test_mode_of_raises_on_ambiguous_mix(params):
    with pytest.raises(c.ModeAmbiguous) as exc:
        c.mode_of(params)
    assert exc.value.refused == "mode-ambiguous"


def test_every_non_product_mode_has_a_module_and_product_does_not():
    assert set(c.MODE_MODULES) == {"halted", "grind", "fanout"}


def test_op_name_matches_registered_literal():
    assert c.OP_NAME == "dispatch.land_run"


def test_reply_keys_cover_ask_compose_commit_schema():
    from coordinator_core.ops.dispatch_emit import ask_compose

    assert set(ask_compose._COMMIT_SCHEMA["properties"]) <= c.REPLY_KEYS


def test_reply_keys_carry_what_callers_read():
    assert {"receipts", "review_stamp", "review_stamp_refusal", "plan_status", "plan_status_reason",
            "criterion_status", "stranded", "peer_held_left", "incomplete_reasons", "next"} <= c.REPLY_KEYS


def test_credited_chunks_reply_key_is_a_member():
    assert c.REPLY_CREDITED_CHUNKS == "credited_chunks"
    assert c.REPLY_CREDITED_CHUNKS in c.REPLY_KEYS


def test_clause_names_op_and_not_the_killed_op():
    assert c.OP_NAME in c.RECONCILE_CLAUSE
    assert "terminal_commit" not in c.RECONCILE_CLAUSE
    assert "-32004" in c.RECONCILE_CLAUSE and "already_landed" in c.RECONCILE_CLAUSE


def test_unlanded_row_trailer_name():
    assert c.UNLANDED_ROW_TRAILER == "Row-Unlanded"


def test_imports_only_stdlib():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "typing"}, imported
