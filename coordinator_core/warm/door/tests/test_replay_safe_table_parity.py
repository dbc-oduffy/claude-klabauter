"""Parity between door_core.c's `door_replay_safe_invocations` table and the
`REPLAY_SAFE_SUBCOMMANDS` declarations in `coordinator/bin/*.py`, both ways."""
from __future__ import annotations

import ast
import re
from pathlib import Path

_DOOR_DIR = Path(__file__).resolve().parents[1]
_DOOR_CORE_C_PATH = _DOOR_DIR / "door_core.c"
_BIN_DIR = Path(__file__).resolve().parents[4] / "coordinator" / "bin"

_TABLE_DECL_RE = re.compile(
    r"door_replay_safe_invocations\[\]\s*=\s*\{(?P<body>.*?)\};",
    re.DOTALL,
)
_STRING_LITERAL_RE = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')


def parse_door_core_table(source: str) -> "frozenset[str]":
    """Entries of the C table, parsed textually; raises when the table is not
    found rather than returning an empty set."""
    match = _TABLE_DECL_RE.search(source)
    assert match is not None, (
        "could not find 'door_replay_safe_invocations[] = { ... };' in "
        f"{_DOOR_CORE_C_PATH}"
    )
    return frozenset(_STRING_LITERAL_RE.findall(match.group("body")))


def _declared_subcommands(path: Path) -> "frozenset[str] | None":
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "REPLAY_SAFE_SUBCOMMANDS"
                   for t in node.targets):
            continue
        value = node.value
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset" and value.args):
            value = value.args[0]
        return frozenset(ast.literal_eval(value))
    return None


def derive_roster() -> "frozenset[str]":
    entries: set[str] = set()
    for p in sorted(_BIN_DIR.glob("*.py")):
        subs = _declared_subcommands(p)
        if subs:
            entries.update(f"{p.stem} {s}" for s in subs)
    return frozenset(entries)


def test_door_core_table_matches_declared_replay_safe_exactly():
    derived = derive_roster()
    table = parse_door_core_table(_DOOR_CORE_C_PATH.read_text(encoding="utf-8"))
    assert not (derived - table), (
        f"declared REPLAY_SAFE_SUBCOMMANDS absent from door_core.c: {sorted(derived - table)}"
    )
    assert not (table - derived), (
        f"door_core.c entries with no REPLAY_SAFE_SUBCOMMANDS declaration: {sorted(table - derived)}"
    )


def test_client_replay_safe_entrypoints_equal_the_c_table():
    from coordinator_core.warm import client

    table = parse_door_core_table(_DOOR_CORE_C_PATH.read_text(encoding="utf-8"))
    assert client._REPLAY_SAFE_ENTRYPOINTS == frozenset(
        tuple(entry.split(" ", 1)) for entry in table
    )


def test_roster_is_non_empty_and_names_brief():
    assert "consolidate-assemble brief" in derive_roster()


def test_parser_rejects_missing_table():
    try:
        parse_door_core_table("int x;")
    except AssertionError:
        return
    raise AssertionError("parser accepted a source without the table")
