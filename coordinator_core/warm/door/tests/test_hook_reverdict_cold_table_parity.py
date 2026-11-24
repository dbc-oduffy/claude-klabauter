"""door_core.c's `door_hook_reverdict_cold_ops` table: exact membership, registered ops only."""
from __future__ import annotations

import re
from pathlib import Path

_DOOR_CORE_C_PATH = Path(__file__).resolve().parents[1] / "door_core.c"

_TABLE_DECL_RE = re.compile(
    r"door_hook_reverdict_cold_ops\[\]\s*=\s*\{(?P<body>.*?)\};",
    re.DOTALL,
)
_STRING_LITERAL_RE = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')


def parse_table(source: str) -> "frozenset[str]":
    match = _TABLE_DECL_RE.search(source)
    assert match is not None, (
        f"could not find 'door_hook_reverdict_cold_ops[] = {{ ... }};' in {_DOOR_CORE_C_PATH}"
    )
    return frozenset(_STRING_LITERAL_RE.findall(match.group("body")))


def _table() -> "frozenset[str]":
    return parse_table(_DOOR_CORE_C_PATH.read_text(encoding="utf-8"))


def test_table_is_exactly_the_bash_dispatch_op():
    assert _table() == frozenset({"hooks.preuse_bash_dispatch"})


def test_every_entry_is_a_registered_op():
    from coordinator_core.authz.classification import OP_CLASSIFICATION as ops
    for entry in _table():
        assert entry in ops, f"{entry} is not a registered op"


def test_side_effect_ops_are_absent():
    table = _table()
    for op in table:
        assert "commit" not in op and "submit" not in op, op


def test_parser_rejects_missing_table():
    try:
        parse_table("int x;")
    except AssertionError:
        return
    raise AssertionError("parser accepted a source without the table")
