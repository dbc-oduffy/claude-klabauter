"""Leaf: plan frontmatter ``row_build_gate`` parse, glob and row-match.

Imports stdlib, ``frontmatter.primitives`` and ``spine_read.load_frontmatter_doc`` only,
never ``emit`` (its import cost is not payable by a spine check).
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.dispatch_emit.spine_read import load_frontmatter_doc


def parse_row_build_gates(plan_text: str) -> tuple:
    """Frontmatter ``row_build_gate`` as ``((change_kind, surface_glob, command), ...)``.

    Tolerant: absent key, bad YAML, or a malformed entry (non-mapping, empty
    or non-string ``command``) yields nothing for that entry; never raises.
    """
    split = split_frontmatter(plan_text)
    if split is None or "row_build_gate" not in split.fm_text:
        return ()
    try:
        doc = load_frontmatter_doc(split.fm_text)
    except yaml.YAMLError:
        return ()
    raw = doc.get("row_build_gate") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return ()
    gates = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        when = entry.get("when")
        when = when if isinstance(when, dict) else {}
        kind = when.get("change_kind")
        glob = when.get("surface_glob")
        gates.append((
            kind if isinstance(kind, str) and kind else None,
            glob if isinstance(glob, str) and glob else None,
            command.strip(),
        ))
    return tuple(gates)


def glob_regex(glob: str) -> "re.Pattern[str]":
    """Compile ``**/`` (any leading dirs), ``**``, ``*`` (no ``/``) and ``?`` to a full-match regex."""
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def gate_commands_for(
    writes: Sequence[str], change_kind: Optional[str], gates: tuple
) -> list:
    """Commands of every gate whose ``when`` matches: ``change_kind`` equal when
    given; ``surface_glob`` matching any write (backslashes normalised)."""
    if not gates:
        return []
    norm = [str(w).replace("\\", "/") for w in writes]
    matched = []
    for kind, glob, command in gates:
        if kind is not None and change_kind != kind:
            continue
        if glob is not None:
            rx = glob_regex(glob)
            if not any(rx.match(w) for w in norm):
                continue
        if command not in matched:
            matched.append(command)
    return matched
