"""Flat scalar-only ``key: value`` YAML loader shared by the initiative read/emit paths.

Public surface: ``simple_yaml_load`` (plus its helpers ``parse_goal_ids`` and ``unquote``).
Correct ONLY for flat files such as ``state/initiatives/*.yaml``; stub handoff frontmatter
carries ``blocks``/``blocked_by`` as YAML arrays this parser flattens into strings, so never
use it there. No PyYAML dependency.
"""

from __future__ import annotations

import re

_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)")
_BLOCK_LIST_ITEM_RE = re.compile(r"^-\s*(.*)$")


def unquote(val: str) -> str:
    """Strip a single layer of matching double- or single-quotes, then whitespace."""
    val = val.strip()
    if len(val) >= 2 and (
        (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
    ):
        val = val[1:-1]
    return val.strip()


def parse_goal_ids(raw_val: str, lines: list[str], line_idx: int) -> list[str]:
    """Parse the ``goals:`` value into a flat id-list (DR-207 ratified array field).

    Handles both YAML shapes the on-disk initiative schema permits:
      - flow syntax on the same line: ``goals: [id1, id2]`` (and empty ``goals: []``)
      - block syntax: ``goals:`` with no inline value, followed by ``  - id`` lines
        at greater indentation than the ``goals:`` line itself.

    Blank/whitespace-only ids are dropped; quotes are stripped per id. Absent/unparseable
    ``goals:`` (bare ``goals:`` with no following block-list items, or a value that is
    neither flow-list nor block-list) yields an empty list — never raises.

    Goal-ids are assumed slug-shaped and MUST NOT contain a literal comma — the flow-list
    split (``inner.split(",")``, below) is a naive comma split with no quote-awareness; a
    quoted id containing a comma would be corrupted (Review: code-reviewer, Finding 2).
    """
    val = raw_val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [unquote(part) for part in inner.split(",") if unquote(part)]

    if val:
        # Some other single-line scalar under `goals:` — not a recognised list shape.
        return []

    # Block-list form: consume subsequent `  - id` lines, but ONLY while they are
    # strictly MORE indented than the `goals:` key line itself (standard YAML
    # block-sequence-under-mapping-key rule). Stops the scan on a dedent to <= that
    # column rather than merely on regex-fail, so a sibling block-list array field
    # (should one ever be added after a bare `goals:` key) is never slurped in.
    # Indentation boundary was previously unchecked.
    goals_indent = len(lines[line_idx]) - len(lines[line_idx].lstrip())
    ids: list[str] = []
    for later in lines[line_idx + 1 :]:
        stripped = later.strip()
        if not stripped or stripped.startswith("#"):
            continue
        later_indent = len(later) - len(later.lstrip())
        if later_indent <= goals_indent:
            break
        m_item = _BLOCK_LIST_ITEM_RE.match(stripped)
        if not m_item:
            break
        item = unquote(m_item.group(1))
        if item:
            ids.append(item)
    return ids


def simple_yaml_load(content: str) -> dict:
    """Flat ``key: value`` YAML parser for initiative files (no PyYAML dependency).

    Handles single-line string values; unquotes double- and single-quoted strings. Lines
    starting with ``#`` or blank lines are ignored. Multi-line block scalars are NOT parsed;
    the field is simply absent if used. Mirrors the bash heredoc parser exactly (Port of:
    emit-cockpit-snapshot.sh, DoE 07eedcfb, 2026-07-19) — including the null-sentinel
    coercion of ``null``, ``~``, and the empty string to ``None``.

    Contract: returns a flat ``dict`` — every caller (``ops/emit/sections/initiatives.py``
    ``collect()``, ``ops/deliverable_rollup.py`` and ``ops/initiatives_serve.py``) treats the
    result as a mapping via ``.get``/``in``. Do NOT change this to a tuple return without
    updating all three call sites and the fixture-roundtrip test in ``test_initiatives_store.py``.

    Exception — the ``goals:`` key (DR-207 ratified array field, absent from the bash
    oracle which predates it): its value is parsed as a real id-list via
    ``parse_goal_ids`` (flow ``[id1, id2]`` or block ``- id`` syntax) rather than the
    flat single-line scalar rule, and staged under the ``_goal_ids`` dict key — it is NOT
    folded into the flat scalar fields (a raw string coercion there would corrupt it, e.g.
    ``"[g1, g2]"``). Absent ``goals:`` key -> ``_goal_ids=[]``. Callers that don't need
    goal ids (deliverable_rollup, initiatives_serve) simply ignore the extra key; ``collect()``
    pops it back out to build the ``_goal_ids`` staging field on the record.
    """
    result: dict = {}
    goal_ids: list[str] = []
    lines = content.splitlines()
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KEY_VALUE_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()
        if key == "goals":
            goal_ids = parse_goal_ids(val, lines, idx)
            continue
        if len(val) >= 2 and (
            (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
        ):
            val = val[1:-1]
        if val in ("null", "~", ""):
            val = None
        result[key] = val
    result["_goal_ids"] = goal_ids
    return result
