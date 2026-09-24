"""Pure classifier: which of a commit's published top-level names does the
landing allowlist (field 7 of its `publish-targets.portable` row) leave
unclassified?

Every function here is pure -- string/dict logic only, never a spawned
process, and no `coordinator_core.ops` import. `coordinator/bin/publish-allowlist-generate.py`
forbids the `coordinator_core.ops` edge on its side, and loading a
`coordinator/bin` script into the commit route from here would be the same
layering inversion the other direction. `_ROWS` and `_ALLOWLIST_FIELD`
duplicate the generator's constants on purpose; `test_published_tree_
classification.py`'s parity test pins the duplicate so it cannot drift
silently.

Callers do their own I/O: `touched_published_names` is a pure prefix test on
the pathspec's index keys, and `unclassified` takes an injected
`read_landing` so this module never opens a file itself.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

#: `(row_name, source_subdir)` -- duplicates the generator's `_ROWS`.
_ROWS: Tuple[Tuple[str, str], ...] = (
    ("claude-klabauter", "coordinator_core"),
    ("claude-klabauter-coordinator-bin", "coordinator/bin"),
)

#: Field index of the allowlist CSV in a `publish-targets.portable` row --
#: duplicates the generator's `_ALLOWLIST_FIELD`.
_ALLOWLIST_FIELD = 6

#: Repo-relative paths this module's callers resolve `read_landing` against.
PORTABLE_PATH = "setup/publish-targets.portable"
DECLARATIONS_PATH = "setup/publish-allowlist-declarations.yaml"


def touched_published_names(paths: Sequence[str]) -> Dict[str, FrozenSet[str]]:
    """The top-level name directly under each `_ROWS` subdir that `paths`
    (index keys, forward-slash) touches, keyed by row name. Empty when
    nothing in `paths` falls under either subdir, so the caller does no I/O."""
    result: Dict[str, set] = {}
    for row_name, source_subdir in _ROWS:
        prefix_parts = PurePosixPath(source_subdir).parts
        depth = len(prefix_parts)
        names = set()
        for p in paths:
            parts = PurePosixPath(p).parts
            if len(parts) <= depth:
                continue
            if parts[:depth] != prefix_parts:
                continue
            names.add(parts[depth])
        if names:
            result[row_name] = frozenset(names)
    return {k: v for k, v in result.items()}


def field7_inclusions(portable_text: str, row_name: str) -> FrozenSet[str]:
    """The landing field-7 CSV entries for `row_name`, minus empty and
    `!`-prefixed (exclusion) entries. Empty if the row is absent."""
    for line in portable_text.splitlines():
        if not line.startswith(f"{row_name}|"):
            continue
        fields = line.split("|")
        if len(fields) <= _ALLOWLIST_FIELD:
            return frozenset()
        entries = [e for e in fields[_ALLOWLIST_FIELD].split(",") if e]
        return frozenset(e for e in entries if not e.startswith("!"))
    return frozenset()


def deny_names(declarations_text: str, row_name: str) -> FrozenSet[str]:
    """The row's `deny` list from the declarations yaml. Entries are a bare
    str or a `{name: ...}` mapping, matching the generator's
    `_row_declarations`."""
    import yaml

    data = yaml.safe_load(declarations_text)
    if not isinstance(data, dict):
        return frozenset()
    rows = data.get("rows")
    if not isinstance(rows, dict):
        return frozenset()
    row = rows.get(row_name)
    if not isinstance(row, dict):
        return frozenset()
    deny_entries = row.get("deny") or []
    return frozenset(
        entry["name"] if isinstance(entry, dict) else entry for entry in deny_entries
    )


def unclassified(
    touched: Dict[str, FrozenSet[str]],
    read_landing: Callable[[str], Optional[str]],
) -> List[Tuple[str, str]]:
    """`(row_name, name)` pairs `touched` names that the landing field 7
    does not include and the landing `deny` does not withhold.

    A missing landing file (portable or, when needed, declarations) means
    the check cannot answer -- returns no refusal for that row's names, so a
    partial checkout is never refused and the publish gate stays the
    backstop."""
    refused: List[Tuple[str, str]] = []
    portable_text = read_landing(PORTABLE_PATH)
    if portable_text is None:
        return refused
    for row_name, names in touched.items():
        inclusions = field7_inclusions(portable_text, row_name)
        missing = sorted(names - inclusions)
        if not missing:
            continue
        declarations_text = read_landing(DECLARATIONS_PATH)
        if declarations_text is None:
            continue
        denied = deny_names(declarations_text, row_name)
        for name in missing:
            if name not in denied:
                refused.append((row_name, name))
    return refused
