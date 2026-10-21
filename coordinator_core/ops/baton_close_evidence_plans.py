"""coordinator_core.ops.baton_close_evidence_plans: plan evidence for the baton close backstop.

One header-regex pass over docs/plans/*.md joins plans to open batons (pm_brief.ref,
sizing_object, shared deliverable_id) and emits PLAN_IMPLEMENTED / PLAN_SPINE_DONE evidence
dicts in the shape pinned by docs/plans/2026-10-21-retroactive-baton-close-backstop.md (C1).

NEGATIVE-SPEC: read-only, no spawn, no write path. Imports nothing from baton_close_predicate
(kinds are literals) and nothing from archive_stamp (the spine is read with the primitives
`archive_stamp._open_spine_row_ids` composes).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

PLAN_IMPLEMENTED = "PLAN_IMPLEMENTED"
PLAN_SPINE_DONE = "PLAN_SPINE_DONE"

_FIELD_RE: Dict[str, "re.Pattern[str]"] = {
    "status": re.compile(r"^status:\s*(.*)$", re.M),
    "deliverable_id": re.compile(r"^deliverable_id:\s*(.*)$", re.M),
    "sizing_object": re.compile(r"^sizing_object:\s*(.*)$", re.M),
}
# `ref:` inside the pm_brief value: inline flow map, or an indented child line.
_PM_BRIEF_RE = re.compile(r"^pm_brief:[^\n]*(?:\n[ \t]+[^\n]*)*", re.M)
_REF_RE = re.compile(r"\bref:\s*[\"']?([^\"'\s,}]+)")


def _value(raw: str) -> str:
    value = raw.split(" #", 1)[0].strip().strip("\"'")
    return "" if value in ("null", "~") else value


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


def _read_header(path: Path) -> Dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    head = text.split("\n---", 1)[0]
    out: Dict[str, str] = {}
    for key, pattern in _FIELD_RE.items():
        match = pattern.search(head)
        if match:
            out[key] = _value(match.group(1))
    block = _PM_BRIEF_RE.search(head)
    if block:
        ref = _REF_RE.search(block.group(0))
        if ref:
            out["pm_brief_ref"] = _norm(ref.group(1))
    return out


def _spine_done(plan_path: Path) -> bool:
    """True for a LOCATED spine with at least one row and no open id left."""
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit.spine_read import drop_em_discharged
    from coordinator_core.ops.plan_tasks_render import load_rows, spine_projection

    try:
        source = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    result = load_rows(source)
    if result.status is not LocateStatus.LOCATED or not result.rows:
        return False
    open_rows = spine_projection(result.rows)["open"]
    if not open_rows:
        return True
    ids = [str(row.get("id", "?")) for row in open_rows]
    return not drop_em_discharged(plan_path, source, open_rows, ids)


def plan_evidence(worktree_root: Path, batons: Dict[str, dict]) -> Dict[str, List[dict]]:
    """Map each baton path (repo-relative posix) to its plan evidence; absent key = none."""
    plans_dir = Path(worktree_root) / "docs" / "plans"
    if not batons or not plans_dir.is_dir():
        return {}

    by_path = {_norm(rel): rel for rel in batons}
    by_sizing: Dict[str, List[str]] = {}
    by_deliv: Dict[str, List[str]] = {}
    for rel, fm in batons.items():
        sizing = fm.get("sizing_object")
        if isinstance(sizing, str) and sizing.strip():
            by_sizing.setdefault(_norm(sizing), []).append(rel)
        deliv = fm.get("deliverable_id")
        if isinstance(deliv, str) and deliv.strip():
            by_deliv.setdefault(deliv.strip(), []).append(rel)

    out: Dict[str, List[dict]] = {}
    for plan_path in sorted(plans_dir.glob("*.md")):
        if plan_path.name.endswith(".review.md"):
            continue
        header = _read_header(plan_path)
        if not header:
            continue
        matched: List[str] = []
        ref = header.get("pm_brief_ref", "")
        if ref and ref in by_path:
            matched.append(by_path[ref])
        sizing = _norm(header.get("sizing_object", ""))
        if sizing:
            matched.extend(by_sizing.get(sizing, ()))
        deliv = header.get("deliverable_id", "")
        if deliv:
            matched.extend(by_deliv.get(deliv, ()))
        if not matched:
            continue

        source = plan_path.relative_to(worktree_root).as_posix()
        if header.get("status") == "implemented":
            kind, detail = PLAN_IMPLEMENTED, "plan status implemented"
        elif _spine_done(plan_path):
            kind, detail = PLAN_SPINE_DONE, "spine complete, plan not stamped implemented"
        else:
            continue
        for rel in dict.fromkeys(matched):
            out.setdefault(rel, []).append(
                {
                    "kind": kind,
                    "reads": "terminal",
                    "strength": "strong" if kind == PLAN_IMPLEMENTED else "weak",
                    "proposes": "shipped",
                    "source": source,
                    "detail": detail,
                }
            )
    return out
