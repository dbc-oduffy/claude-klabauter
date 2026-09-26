"""
test_coordinator_queue_append_enum_parity.py — help-string / schema enum-parity assertions.

Spec: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md (C23, Item 37)
Origin: state/cross-repo/archive/2026-09-24-doe-claude-em-queue-append-help-assertions-for-claude-klabauter.md

These two tests check that `coordinator-queue-append`'s argparse `--change-kind` and
`--scope` help strings (the hardcoded "Valid: ..." enum lists a human maintains by hand)
have not drifted from the DoE-resident schema enums they describe —
`coordinator/schemas/improvement-queue.schema.json`'s `change_kind` enum and
`coordinator/schemas/lesson-entry.schema.json`'s `scope` enum, respectively. They were
extracted from DoE-claude's `test_plan_tasks_schema_enum_parity.py` (AC4/AC5) because
`coordinator-queue-append` is claude-klabauter-resident — subject-first placement puts CLI-behavior
assertions here, not on the DoE-resident schema/wiki parity suite they used to live in.

The schemas themselves are DoE-resident (they do not exist on the claude-klabauter tree), so this
module resolves the sibling DoE-claude checkout via
`coordinator_core.testing.doe_root.resolve_doe_root()` — the pattern
`coordinator_core/install/test_gen_settings_hooks.py` already established for this same
cross-repo-fixture-dependency shape — and skips the whole module when that checkout (and
its schemas) is not present, rather than hard-failing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core.testing.doe_root import resolve_doe_root

QUEUE_APPEND_CLI = Path(__file__).parent / "coordinator-queue-append.py"

_DOE_ROOT = Path(resolve_doe_root() or "/doe-root-unresolved")
IMPROVEMENT_QUEUE_SCHEMA = _DOE_ROOT / "coordinator" / "schemas" / "improvement-queue.schema.json"
LESSON_ENTRY_SCHEMA = _DOE_ROOT / "coordinator" / "schemas" / "lesson-entry.schema.json"

pytestmark = pytest.mark.skipif(
    not (IMPROVEMENT_QUEUE_SCHEMA.is_file() and LESSON_ENTRY_SCHEMA.is_file()),
    reason=f"DoE-claude sibling checkout not found (expected schemas under {_DOE_ROOT})",
)


def _schema_enum(schema_path: Path, property_name: str) -> list[str]:
    schema = json.loads(schema_path.read_text())
    return sorted(schema["properties"][property_name]["enum"])


def _cli_help_enum(cli_source: str, flag: str) -> list[str]:
    """Extract the sorted 'Valid: a, b, c.' enum list from an argparse
    --<flag> argument's help string in coordinator-queue-append's source.

    Locates the `parser.add_argument("--<flag>", ...)` block (up to the
    next `parser.add_argument(` or end of source), then finds the
    `Valid: <a>, <b>, ...` clause within it. Tolerant of the help text
    being split across adjacent Python string-concatenation literals
    (the source writes multi-line help as several adjacent "..." fragments
    inside a parenthesised expression) and of a trailing period.
    """
    arg_marker = f'"--{flag}"'
    start = cli_source.index(arg_marker)
    next_arg = cli_source.find("parser.add_argument(", start + len(arg_marker))
    block = cli_source[start : next_arg if next_arg != -1 else len(cli_source)]

    # Collapse adjacent string-literal fragments into one contiguous string
    # by stripping the fragment quote/concatenation boilerplate: pull every
    # quoted literal out of the block and join them, so a `Valid: ...` clause
    # split across fragments reassembles correctly.
    fragments = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    joined = "".join(fragments)

    match = re.search(r"Valid:\s*(.+?)\.", joined)
    assert match, f"could not locate 'Valid: ...' clause for --{flag} help in {QUEUE_APPEND_CLI}"
    values = [v.strip() for v in match.group(1).split(",")]
    assert values, f"no enum values parsed from --{flag} help clause"
    return sorted(values)


def test_ac4_change_kind_help_matches_improvement_queue_enum() -> None:
    cli_source = QUEUE_APPEND_CLI.read_text()
    help_enum = _cli_help_enum(cli_source, "change-kind")
    schema_enum = _schema_enum(IMPROVEMENT_QUEUE_SCHEMA, "change_kind")
    assert help_enum == schema_enum, (
        f"coordinator-queue-append --change-kind help enum has drifted from "
        f"improvement-queue.schema.json's change_kind enum. "
        f"Help says: {help_enum}; schema says: {schema_enum}. "
        f"Fix whichever side is stale (update the CLI help string or the schema enum)."
    )


def test_ac5_scope_help_matches_lesson_entry_enum() -> None:
    cli_source = QUEUE_APPEND_CLI.read_text()
    help_enum = _cli_help_enum(cli_source, "scope")
    schema_enum = _schema_enum(LESSON_ENTRY_SCHEMA, "scope")
    assert help_enum == schema_enum, (
        f"coordinator-queue-append --scope help enum has drifted from "
        f"lesson-entry.schema.json's scope enum. "
        f"Help says: {help_enum}; schema says: {schema_enum}. "
        f"Fix whichever side is stale (update the CLI help string or the schema enum)."
    )
