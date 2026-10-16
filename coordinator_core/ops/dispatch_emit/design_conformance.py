"""design_conformance: everything about a design-conformance ledger row except where it is called.

A registry hit (`design_registry.lookup`) becomes one open row on the sizing's requirement
register, anchored `design-registry:<entry id>`. The row is met only by a senior-front-end
review record naming its board, or by a PM ratification file cited for its entry. Works on
hit dicts (`id`, `board_url`, `artboards`, `status`, `harvest_path`?, `paths`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.ops import requirement_register as rr
from coordinator_core.reviewer_vocabulary import SENIOR_FRONT_END, bare_agent_type

GENERATES = []

ANCHOR_PREFIX = "design-registry:"
#: The gate-reply key `dispatch.ask_gate` fills and `ask_compose` reads.
DESIGN_BRIEF_KEY = "design_brief"
DESIGN_SURFACE = "visual"
#: Review verdicts that never meet a design row.
REFUSING_VERDICTS = frozenset({"REJECTED", "REQUIRES_CHANGES", "BLOCKED", "PIVOT"})

_BOARD_LINE = re.compile(r"(?m)^Board: (\S+)\s*$")
_DEVIATION_FENCE = re.compile(r"(?ms)^[ \t]*```ya?ml[ \t]+design-deviation[ \t]*\n(.*?)^[ \t]*```")
_KEY_LINE = re.compile(r"^(?:-\s+)?([A-Za-z_][\w-]*):[ \t]*(.*?)\s*$")


def entry_id(row: Mapping) -> str:
    """The registry entry id a design row's anchor names, else ''."""
    anchor = row.get(rr.ROW_ANCHOR)
    if isinstance(anchor, str) and anchor.startswith(ANCHOR_PREFIX):
        return anchor[len(ANCHOR_PREFIX):]
    return ""


def _source_text(hit: Mapping) -> str:
    lines = [
        f"Design board for registry entry {hit.get('id')} (status {hit.get('status')}).",
        f"Board: {hit.get('board_url')}",
        f"Artboards: {', '.join(str(a) for a in hit.get('artboards') or []) or '(none named)'}",
    ]
    if hit.get("harvest_path"):
        lines.append(f"Harvest: {hit['harvest_path']}")
    lines.append(f"Paths: {', '.join(str(p) for p in hit.get('paths') or [])}")
    return "\n".join(lines)


def brief_text(hits: Sequence[Mapping], registry_rel: Optional[str]) -> str:
    """Which board does the plan author owe? '' with no registry; a one-line registry pointer
    with a registry and no hits; else one block per hit."""
    if not registry_rel:
        return ""
    if not hits:
        return (
            f"Design registry: {registry_rel} is present. Check your writes against its `sources` "
            "globs before planning."
        )
    out = [
        f"Design registry ({registry_rel}): the writes in scope are drawn on these boards. "
        "Cite `<harvest>/harvest.json` for each entry or carry a ratified `design-deviation` block "
        "(`entry:`, `ratified_by:`), and declare `front-end` in review_signals."
    ]
    for hit in hits:
        out.append("")
        out.append(f"- Entry {hit.get('id')} (status {hit.get('status')})")
        out.append(f"  Board: {hit.get('board_url')}")
        out.append(f"  Artboards: {', '.join(str(a) for a in hit.get('artboards') or []) or '(none named)'}")
        if hit.get("harvest_path"):
            out.append(f"  Harvest: {hit['harvest_path']}")
        out.append(f"  Matched paths: {', '.join(str(p) for p in hit.get('paths') or [])}")
    return "\n".join(out)


def mint_rows(repo_root: Path, sizing_rel: str, hits: Sequence[Mapping]) -> list[str]:
    """Which ledger rows do these hits owe? Appends one open row per hit not already anchored,
    under the register's file lock, and returns the ids added."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    added: list[str] = []

    def edit(rows: list[dict]) -> list[dict]:
        added.clear()
        have = {str(r.get(rr.ROW_ANCHOR) or "") for r in rows}
        for hit in hits:
            anchor = f"{ANCHOR_PREFIX}{hit.get('id')}"
            if anchor in have:
                continue
            have.add(anchor)
            row_id = rl._next_id(rows)
            rows.append({
                rr.ROW_ID: row_id, rr.ROW_TEXT: _source_text(hit), rr.ROW_ANCHOR: anchor,
                rr.ROW_SURFACE: DESIGN_SURFACE, rr.ROW_STATUS: rr.STATUS_OPEN, rr.ROW_CLAIMED_BY: [],
            })
            added.append(row_id)
        return rows

    rl._mutate_register(Path(repo_root), sizing_rel, edit)
    return list(added)


def _unquote(raw: str) -> str:
    raw = raw.split(" #", 1)[0].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _deviation_ratifiers(plan_text: Optional[str], eid: str) -> list[str]:
    """`ratified_by` values of the design-deviation items naming entry `eid`."""
    out: list[str] = []
    for fence in _DEVIATION_FENCE.findall(plan_text or ""):
        item: dict[str, str] = {}
        items = [item]
        for line in fence.splitlines():
            body = line.strip()
            m = _KEY_LINE.match(body)
            if not m:
                continue
            if body.startswith("-"):
                item = {}
                items.append(item)
            item[m.group(1)] = _unquote(m.group(2))
        out.extend(i["ratified_by"] for i in items if i.get("entry") == eid and i.get("ratified_by"))
    return out


def _registry_ratification(repo_root: Path, eid: str) -> Optional[str]:
    """The registry entry's own `ratification` path, else None."""
    from coordinator_core import design_registry

    entries, _code = design_registry.load(Path(repo_root))
    for entry in entries or []:
        if entry.get("id") == eid and isinstance(entry.get("ratification"), str):
            return entry["ratification"]
    return None


def _contained_file(root: Path, rel: object) -> Optional[Path]:
    """The file `rel` names under `root`, else None (absent, a directory, or an escape)."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    try:
        root = root.resolve()
        found = (root / rel.strip().replace("\\", "/")).resolve()
        found.relative_to(root)
    except (OSError, ValueError):
        return None
    return found if found.is_file() else None


def _in_plans(root: Path, target: Path) -> bool:
    try:
        target.relative_to((root.resolve() / "docs" / "plans"))
    except ValueError:
        return False
    return True


def _frontmatter(text: str) -> dict[str, str]:
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    split = split_frontmatter(text)
    if split is None:
        return {}
    return {
        key: value
        for key in ("reviewer", "agent_type", "verdict")
        if (value := read_fm_field_unquoted(split.fm_text, key))
    }


def _record_refusal(target: Path, board: str) -> Optional[str]:
    """Why `target` is no senior-front-end review of `board`, or None when it is."""
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return "is unreadable"
    fm = _frontmatter(text)
    reviewers = {bare_agent_type(fm.get("reviewer")), bare_agent_type(fm.get("agent_type"))}
    if SENIOR_FRONT_END not in reviewers:
        return "names no senior-front-end reviewer"
    verdict = re.sub(r"[\s-]+", "_", fm.get("verdict", "").strip()).upper()
    if not verdict or verdict in REFUSING_VERDICTS:
        return f"carries verdict {verdict or '(none)'}"
    if board not in text:
        return "does not name the board"
    return None


def evidence_refusal(
    repo_root: Path, row: Mapping, verdict_item: Mapping, plan_text: Optional[str]
) -> Optional[str]:
    """Does this met claim carry senior-front-end or ratified-deviation evidence? None when it
    does; else one sentence naming what is missing. The registry entry's own status never meets
    a row."""
    root = Path(repo_root)
    eid = entry_id(row)
    m = _BOARD_LINE.search(str(row.get(rr.ROW_TEXT) or ""))
    board = m.group(1) if m else ""
    cited = [*_deviation_ratifiers(plan_text, eid)]
    registry = _registry_ratification(root, eid)
    if registry:
        cited.append(registry)
    cited_files = {t for t in (_contained_file(root, c) for c in cited) if t is not None}
    met_by = verdict_item.get("met_by")
    paths = [met_by] if isinstance(met_by, str) else [p for p in met_by or [] if isinstance(p, str)]
    prefix = f"design row {row.get(rr.ROW_ID)} ({eid})"
    if not paths:
        return f"{prefix}: met_by names no file. Name a senior-front-end review of the board or a ratification."
    first = ""
    for rel in paths:
        target = _contained_file(root, rel)
        if target is None:
            first = first or f"met_by {rel} is no file under the repo root"
            continue
        if target in cited_files and not _in_plans(root, target):
            return None
        if target in cited_files:
            first = first or f"met_by {rel} is a plan, which cannot ratify"
            continue
        why = _record_refusal(target, board) if board else "names no board URL on the row"
        if why is None:
            return None
        first = first or f"met_by {rel} {why}"
    return f"{prefix}: {first}. Name a senior-front-end review of the board or a cited ratification."
