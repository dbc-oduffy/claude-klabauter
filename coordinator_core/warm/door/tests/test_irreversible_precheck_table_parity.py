"""The door's `door_irreversible_command_shapes` table matches the Python matcher and the registry.

`door_core.c :: door_irreversible_command_shapes` is the warm-timeout leg of
`hooks/preuse_bash_dispatch.py :: _IRREVERSIBLE` (the import-failure / engine-error
leg). Both legs share one outcome, so the table's names equal `_IRREVERSIBLE`'s
names exactly, and its verbs cover every `IRREVERSIBLE_EXTERNAL` guard in
`deny_registry.GUARD_DENY_CLASS`.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core import deny_registry
from coordinator_core.hooks import preuse_bash_dispatch

_CORE_C = Path(__file__).resolve().parents[1] / "door_core.c"
_TABLE_RE = re.compile(r"door_irreversible_command_shapes\[\]\s*=\s*\{(?P<body>.*?)\n\};", re.DOTALL)
_ROW_RE = re.compile(r'\{\s*"(?P<name>[^"]+)",\s*"(?P<tool>[^"]*)",\s*"(?P<verb>[^"]*)",\s*"(?P<sub>[^"]*)"\s*\}')

#: Each IRREVERSIBLE_EXTERNAL guard -> the table names it needs. A new guard of that class
#: fails the coverage test until it is mapped here and given a table row.
_GUARD_TO_NAMES = {
    "block-push-pr-non-owned-remote": {"git push", "gh pr merge", "gh release create"},
    "block-perforce-submit": {"p4 submit"},
    "p4-verb-fence": {"p4 shelve"},
}


def _table_rows() -> list[dict[str, str]]:
    match = _TABLE_RE.search(_CORE_C.read_text(encoding="utf-8"))
    assert match, "door_irreversible_command_shapes[] not found in door_core.c"
    rows = [m.groupdict() for m in _ROW_RE.finditer(match.group("body"))]
    assert rows, "door_irreversible_command_shapes parsed to no rows"
    return rows


def test_the_table_names_equal_the_python_irreversible_names_exactly():
    table = [row["name"] for row in _table_rows()]
    python = [name for _, name in preuse_bash_dispatch._IRREVERSIBLE]
    assert sorted(table) == sorted(python)
    assert len(table) == len(set(table))


def test_the_table_covers_every_irreversible_external_guard():
    registry = {
        guard for guard, cls in deny_registry.GUARD_DENY_CLASS.items()
        if cls == deny_registry.IRREVERSIBLE_EXTERNAL
    }
    assert registry == set(_GUARD_TO_NAMES), "map the new IRREVERSIBLE_EXTERNAL guard to its table verbs"
    names = {row["name"] for row in _table_rows()}
    for guard, wanted in _GUARD_TO_NAMES.items():
        assert wanted <= names, guard


def test_each_row_names_its_own_verbs():
    for row in _table_rows():
        assert row["name"].startswith(row["tool"] + " "), row
        words = row["name"].split()
        assert words[1] == row["verb"], row
        if row["sub"]:
            assert words[-1] == row["sub"], row
