"""Footprint leg for an existing XS sizing: when the leg is owed, its return schema and its prompt.

Pure: no I/O, no spawn. `ask_compose.compose_ask_script` wires the leg ahead of the gate agent.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Sequence

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import _clean
from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_XS

#: Same shape `ask_compose._obj` emits.
FOOTPRINT_SCHEMA = {
    "type": "object",
    "required": ["writes"],
    "properties": {"writes": {"type": "array", "items": {"type": "string"}}},
}


#: One lexical rule for an admitted footprint write, written in the subset Python `re` and JS `RegExp`
#: share so the composed script and the unit tests apply the same definition. Rejects: empty or
#: blank, POSIX-absolute, `~`, Windows drive, any backslash, glob characters, a `..` segment, a
#: bare `.`, a trailing slash (directory), leading or trailing whitespace, and any CR or LF.
ADMIT_WRITE_PATTERN = (
    r"^(?!/|~|[A-Za-z]:|\.$)(?!.*[\\*?\[\]{}\n\r])(?!(?:.*/)?\.\.(?:/|$))[^\s/](?:.*[^/\s])?$"
)

_ADMIT_RE = re.compile(ADMIT_WRITE_PATTERN)


def is_admitted_write(path: object) -> bool:
    """True when `path` is a repo-relative, in-tree, glob-free file path (lexical; no I/O)."""
    return isinstance(path, str) and _ADMIT_RE.fullmatch(path) is not None


def admit_writes_js() -> str:
    """JS predicate source `(w) => ...` equal to `is_admitted_write`, for the composed script's filter."""
    return f"((w) => typeof w === 'string' && new RegExp({json.dumps(ADMIT_WRITE_PATTERN)}).test(w))"


_BATON_SECTIONS = frozenset({"what this covers", "specification", "acceptance criteria"})
_HEADING = re.compile(r"^##\s+(.*?)\s*$")
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def declared_footprint(baton_text: str) -> tuple[str, ...]:
    """Repo-relative file paths the baton's covers, specification and acceptance sections name in backticks.

    Token normalisation is `ledger_remaining_paths._clean`; absolute, drive-lettered, `..` and
    scratch/ tokens are refused there. Pure and lexical: no existence check, first-seen order, deduplicated.
    """
    found: dict[str, None] = {}
    in_section = False
    for line in baton_text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            in_section = heading.group(1).strip().lower() in _BATON_SECTIONS
            continue
        if not in_section:
            continue
        for token in _BACKTICKED.findall(line):
            cleaned = _clean(token)
            if cleaned is not None:
                found.setdefault(cleaned, None)
    return tuple(found)


def footprint_owed(*, arm: Optional[str], writes: Sequence[str], sized_in_run: bool) -> bool:
    """True when an existing XS sizing reaches the gate with no file footprint.

    A raw or intake ask is sized in run and its scout already authors `writes`, so it is never owed.
    """
    return arm == ARM_XS and not writes and not sized_in_run


def footprint_prompt(*, sizing_rel: str, baton_path: Optional[str]) -> str:
    """The prompt asking an agent for the repo-relative files the ask writes."""
    parts = [
        f"Read the XS sizing at {sizing_rel} (intent, premise.evidence, exit_criterion)."
    ]
    if baton_path:
        parts.append(
            f"Also read the baton record at {baton_path} (its ## Specification and ## Acceptance criteria)."
        )
    parts.append(
        "Return `writes`: the repo-relative FILE paths this ask will create or modify, tests included. "
        "List files only: no globs and no directories. Every path is inside this repo. "
        "Write nothing yourself and run no command."
    )
    return " ".join(parts)
