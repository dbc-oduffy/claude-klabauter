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
#: Prompt clause for the ledger check: a design row's default `met_by` is the wave's
#: senior-front-end review sidecar, never the harvest output.
DEFAULT_MET_BY_CLAUSE = (
    " Default met_by for a design-registry item judged 'met' with no ratification file of its own: "
    "the senior-front-end review sidecar of the review wave named here (it must name the item's board); "
    "never the harvest output or its directory. Wave senior-front-end review sidecars: "
)
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
        "Build from `<harvest>/harvest.json`, but never cite it or its directory as met_by evidence: "
        "it is the source the row is judged against. Meet each entry with a senior-front-end review "
        "or carry a ratified `design-deviation` block "
        "(`entry:`, `ratified_by:`), and declare `front-end` in review_signals. A senior-front-end "
        "reviewer writes one `Board: <url>` line in its sidecar for each board it checked."
    ]
    for hit in hits:
        out.append("")
        out.append(f"- Entry {hit.get('id')} (status {hit.get('status')})")
        out.append(f"  Board: {hit.get('board_url')}")
        out.append(f"  Artboards: {', '.join(str(a) for a in hit.get('artboards') or []) or '(none named)'}")
        if hit.get("harvest_path"):
            out.append(f"  Harvest: {hit['harvest_path']}")
        elif hit.get("harvest_expected_path"):
            out.append(
                f"  Harvest: absent (expected {hit['harvest_expected_path']}); not a blocking gate. "
                "Mint it with the canvas.harvest op."
            )
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


_HARVEST_LINE = re.compile(r"(?m)^Harvest: (\S+)\s*$")
HARVEST_RECORD_NAME = "harvest.json"


def _judged_source(root: Path, row_text: str) -> Optional[Path]:
    """The directory holding the row's `Harvest:` record, else None."""
    m = _HARVEST_LINE.search(row_text)
    if not m:
        return None
    try:
        root = root.resolve()
        parent = (root / m.group(1).replace("\\", "/")).resolve().parent
        parent.relative_to(root)
    except (OSError, ValueError):
        return None
    # A root-level harvest record must not condemn the whole repo; the name check still applies.
    return None if parent == root else parent


def _is_judged_source(target: Path, harvest_dir: Optional[Path]) -> bool:
    """Is `target` a harvest record, or inside the row's harvest directory?"""
    if target.name == HARVEST_RECORD_NAME:
        return True
    if harvest_dir is None:
        return False
    try:
        target.relative_to(harvest_dir)
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


_BODY_VERDICT = re.compile(r"^#{1,6}[ \t]+Verdict[ \t]*:?[ \t]*\n+[ \t>*_`]*([A-Za-z]+(?:[ \t_-]+[A-Za-z]+)*)", re.M | re.I)


def _body_verdict(text: str) -> str:
    """Normalised leading words under a body `## Verdict` heading, or "" when there is none."""
    m = _BODY_VERDICT.search(text)
    return re.sub(r"[\s-]+", "_", m.group(1).strip()).upper() if m else ""


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
    verdict = re.sub(r"[\s-]+", "_", fm.get("verdict", "").strip()).upper() or _body_verdict(text)
    if not verdict or verdict in REFUSING_VERDICTS or any(verdict.startswith(r + "_") for r in REFUSING_VERDICTS):
        return f"carries verdict {verdict or '(none)'}"
    if board not in text:
        return "does not name the board"
    return None


def _wave_agent_type(root: Path, entry: Mapping) -> str:
    """The wave return's reviewer type. The slice-review-result schema carries no `agent_type`,
    so the spawn-provisioned sidecar frontmatter is the fallback; the verdict still comes from the return."""
    declared = bare_agent_type(entry.get("agent_type"))
    if declared:
        return declared
    target = _contained_file(root, entry.get("sidecar_path"))
    if target is None:
        return ""
    try:
        return bare_agent_type(_frontmatter(target.read_text(encoding="utf-8-sig")).get("agent_type"))
    except (OSError, UnicodeDecodeError):
        return ""


def _wave_refusal(root: Path, entry: Mapping, board: str) -> Optional[str]:
    """Why this wave return is no senior-front-end review of `board`, or None when it is.
    The verdict comes from the schema-validated return, never from the sidecar."""
    if _wave_agent_type(root, entry) != SENIOR_FRONT_END:
        return "names no senior-front-end reviewer"
    verdict = re.sub(r"[\s-]+", "_", str(entry.get("verdict") or "").strip()).upper()
    if not verdict or verdict in REFUSING_VERDICTS:
        return f"carries verdict {verdict or '(none)'}"
    rel = entry.get("sidecar_path")
    target = _contained_file(root, rel)
    if target is None or _in_plans(root, target):
        return "is no review file outside docs/plans"
    if not board:
        return "names no board URL on the row"
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return "is unreadable"
    return None if board in text else "does not name the board"


def evidence_refusal(
    repo_root: Path, row: Mapping, verdict_item: Mapping, plan_text: Optional[str],
    *, wave_reviews: Sequence[Mapping] = (), noted_paths: Sequence[str] = (),
) -> Optional[str]:
    """Does this met claim carry senior-front-end or ratified-deviation evidence? None when it
    does; else one sentence naming what is missing. Candidates, in order: the verdict item's
    `met_by`, each revise-noted path (same checks), each senior-front-end wave return whose
    sidecar names the board. The registry entry's own status never meets a row."""
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
    first = ""

    judged = _judged_source(root, str(row.get(rr.ROW_TEXT) or ""))

    def passes(rel: str, label: str) -> bool:
        nonlocal first
        target = _contained_file(root, rel)
        if target is None:
            first = first or f"{label} {rel} is no file under the repo root"
            return False
        if _is_judged_source(target, judged):
            first = first or (
                f"{label} {rel} is the harvest/design source the row is judged against, "
                "not conformance evidence"
            )
            return False
        if target in cited_files and not _in_plans(root, target):
            return True
        if target in cited_files:
            first = first or f"{label} {rel} is a plan, which cannot ratify"
            return False
        why = _record_refusal(target, board) if board else "names no board URL on the row"
        if why is None:
            return True
        first = first or f"{label} {rel} {why}"
        return False

    if any(passes(rel, "met_by") for rel in paths):
        return None
    if any(passes(rel, "revise-noted file") for rel in noted_paths if isinstance(rel, str)):
        return None
    for entry in wave_reviews:
        if not isinstance(entry, Mapping) or _wave_agent_type(root, entry) != SENIOR_FRONT_END:
            continue
        why = _wave_refusal(root, entry, board)
        if why is None:
            return None
        first = first or f"wave review {entry.get('sidecar_path')} {why}"
    lead = first or "met_by names no file"
    if first and not paths:
        lead = f"met_by names no file; {first}"
    return (
        f"{prefix}: {lead}. No senior-front-end wave review and no revise-noted review of the board "
        "met the row. Name a senior-front-end review of the board or a cited ratification."
    )
