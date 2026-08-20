"""
coordinator_core.plan_assemble.predicates.composition_lints — Layer 0
Branch C (a) of the `plan-assemble` contract: spine-row shape, AC
reject-list, deferral `case_against` evidence, hard-constraints-block
presence, sub-agent-spawn detection, shared-state candidate matching, and
chunk-index-sidecar presence.

Purpose: seven `gates.composition.*` contract rows
(`DoE-claude archive/specs/2026-08/2026-08-08-stop-the-rot-pickup-and-
workstream-complete.md:611-714`, as amended by
`cross-repo/inbox/2026-08-13-doe-claude-em-plan-assemble-wave1-accepted-and-
wave2-rulings.md`) — `:136`, `:137`, `:143`, `:150`, `:152`, `:153`,
`:172` — each a leaf reader taking a `PredicateContext` and returning
either the sub-tree to merge at its own `gates.composition.<name>` key, or
`predicates.undetermined(reason)` when the row's required input (a
missing `--plan`, an unparseable plan-tasks spine) is not available. A
scalar-valued contract row (`:152`) returns its bare value; a row with
multiple named sub-fields (`:137`, `:143`, `:150`, `:153`, `:172`) returns
a `dict` of those sub-fields. C13 (`residue.py`) merges these sub-trees
into the assembled `gates.composition` namespace — that wiring is
exclusively C13's write target and is untouched here.

Rows and their functions:
  :136   `spine_row_shape(ctx)` -> `{"valid": bool}`
  :137   `ac_reject_list(ctx)` -> `{"hits": [{"ac_id", "matched_pattern"}]}`
  :143   `deferral_case_against(ctx)` -> `{"entries": [{"id",
         "case_against_present", "case_against_text"}]}`
  :150   `hard_constraints_block(ctx)` -> `{"present": bool}`
  :152   `stub_spawns_subagents(ctx)` -> `bool`
  :153   `concurrency_shared_state(ctx)` -> `{"candidate": bool,
         "matched_paths": [str]}`
  :172   `chunk_index_sidecar(ctx)` -> `{"exists": bool, "path":
         Optional[str]}`

`:136`, `:143`, `:152`, `:153` all read the SAME plan-tasks spine (the
fenced ` ```yaml plan-tasks ` block under the plan's `## Tasks` heading);
`_load_spine_rows(ctx)` is the one internal disk-touching-adjacent parse
(it re-parses `ctx.plan_body`, already read once by `PredicateContext.
from_paths`, per that context's own contract that leaf readers touch disk
again only for a grep/git call this row itself names — locating and
YAML-parsing the fence is this row's own named read, not a re-read of the
plan file).

Negative-spec:
  - `:136` does NOT re-derive the plan-tasks spine-row shape. It reuses
    `frontmatter.schema_validate._cf_plan_tasks_disposition_shape`
    verbatim, called with `governed=True` on every row unconditionally —
    not to detect whether the plan IS governed (this module has no
    opinion on that), but because the row's own PM-approval arm is
    `U`-classified and excluded per the contract's negative-spec
    constraint; `governed=True` is the one existing lever that function
    exposes to suppress exactly that leg while leaving the `case_against`
    and `disposition_ref`-shape legs (both `C`) live. The vacuity line is
    drawn exactly where that function draws it: an absent plan-tasks
    fence (`LocateStatus.ABSENT`) is zero rows to check, so `valid: True`
    vacuously — the same convention `_cf_disposition_shape` uses when its
    own `items` argument is `None`.
  - `:137` does NOT judge whether an AC's phrasing is "genuinely binary
    pass/fail" — that call stays `G` per the contract. This function only
    regex-matches known vague/untestable phrasing markers
    (`_AC_REJECT_PATTERNS`) against each `AC-N` line's criterion text and
    reports the hits; it never infers testability from anything else.
  - `:143` does NOT resolve the deferral disposition itself (`backlogged`
    vs `wont_do` vs overturning either) — that stays the PM's, per the
    contract's negative-spec constraint. This function reports only
    `case_against` presence/non-vacuity for the plan-tasks rows whose
    `disposition` is one of the two PM-approval-gated (deferral)
    dispositions the schema already recognises
    (`schema_validate._PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS` —
    `backlogged`/`wont_do`; `spun_off` is excluded, matching that same
    constant's own DR-096 rationale that nothing leaves the corpus under
    `spun_off`, so there is no scope cut to argue against).
  - `:153` does NOT decide whether a matched path is ACTUALLY shared
    state — candidate evidence only, per the contract. `matched_paths` is
    a same-shape sibling of `:59`'s security/privacy-boundary proxy list,
    not a verdict.
  - `:172` does NOT invent a chunk-index-sidecar naming CONVENTION beyond
    the one heuristic this module implements (a `chunk-index sidecar`
    mention followed by a backtick-quoted path, case-insensitive). A plan
    that never names such a path (this module's own host plan does not)
    resolves to `undetermined`, not `exists: False` — there being nothing
    named is a different fact from a named path not existing on disk.
  - None of these functions resolve a `U`-classified row (AC4) or write
    to `residue.py` (C13's exclusive target).

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C5
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.schema_validate import (
    _cf_plan_tasks_disposition_shape,
    _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS,
)
from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

#: Vague/untestable phrasing markers `:137` rejects against each `AC-N`
#: line's criterion text. Deliberately conservative (common hedge words
#: and open-ended markers only) — "is this AC genuinely binary pass/fail"
#: stays a `G`, never-decided-here judgment call; this list is pattern
#: matching, not a testability verdict.
_AC_REJECT_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("vague_qualifier", re.compile(
        r"\b(properly|appropriately|reasonably|sufficiently|adequately)\b",
        re.IGNORECASE,
    )),
    ("open_ended_scope", re.compile(r"\b(?:etc\.?|and so on|and more)\b", re.IGNORECASE)),
    ("placeholder", re.compile(r"\bTBD\b|\bTODO\b", re.IGNORECASE)),
    ("hedge_should_work", re.compile(
        r"\bshould\s+(?:work|function|behave|be\s+fine)\b", re.IGNORECASE
    )),
    ("subjective_quality", re.compile(
        r"\b(good|clean|nice|reasonable|sensible)\s+(?:code|design|shape|quality)\b",
        re.IGNORECASE,
    )),
)

#: A markdown-table AC row: `| ACn | <criterion text> | <status> |`.
_AC_TABLE_ROW_RE = re.compile(r"^\|\s*(AC-?\d+)\s*\|\s*(.+?)\s*\|", re.MULTILINE)

#: An inline `AC-N: <criterion text>` line (non-table authoring style).
_AC_INLINE_RE = re.compile(r"(?:^|\s)(AC-?\d+):\s*(.+?)\s*$", re.MULTILINE)

#: `## <heading naming hard constraints>` — case-insensitive, matches this
#: very plan's own `## Executor hard constraints` heading.
_HARD_CONSTRAINTS_HEADING_RE = re.compile(
    r"^##\s+.*\bhard constraints\b", re.MULTILINE | re.IGNORECASE
)

#: Dispatch verbs `:152` greps a stub body for, per the contract's own
#: literal list (`dispatch`, `spawn`, `Task(`).
_DISPATCH_VERB_RE = re.compile(r"\b(dispatch|spawn)\b|Task\(", re.IGNORECASE)

#: Path-pattern candidates for `:153`'s shared-state proxy — same shape as
#: `:59`'s security/privacy-boundary candidate list, evidence only.
_SHARED_STATE_PATH_PATTERNS: tuple["re.Pattern[str]", ...] = (
    re.compile(r"(^|/)state/"),
    re.compile(r"schema\.json$"),
    re.compile(r"(^|/)registry(/|_map)?\.py$"),
    re.compile(r"\.lock$"),
    re.compile(r"(^|/)locked_write\.py$"),
)

#: A `chunk-index sidecar` mention followed by a backtick-quoted path.
_CHUNK_INDEX_SIDECAR_RE = re.compile(
    r"chunk[- ]index\s+sidecar[^`\n]*`([^`]+)`", re.IGNORECASE
)


def _load_spine_rows(ctx: PredicateContext) -> tuple[Optional[list], Optional[dict[str, Any]]]:
    """Locate and parse the plan's plan-tasks spine.

    Returns `(rows, sentinel)` — exactly one populated, except an absent
    fence, which returns `([], None)` (zero rows, not a sentinel — the
    vacuity case every caller in this module treats as "nothing to
    check", matching `_cf_plan_tasks_disposition_shape`'s own convention
    for an absent `items` argument).
    """
    if ctx.plan_body is None:
        return None, undetermined("no --plan supplied; cannot locate the plan-tasks spine")

    result = locate_fenced_block(ctx.plan_body)
    if result.status is LocateStatus.ABSENT:
        return [], None
    if result.status is LocateStatus.MALFORMED:
        return None, undetermined(
            "plan-tasks fence is malformed (duplicate fence, or no fence "
            "inside the ## Tasks heading's section)"
        )

    try:
        rows = yaml.safe_load(result.body) or []
    except yaml.YAMLError as exc:
        return None, undetermined(f"plan-tasks spine body is not parseable YAML: {exc}")
    if not isinstance(rows, list):
        return None, undetermined("plan-tasks spine body did not parse to a YAML list")
    return rows, None


def _extract_ac_lines(plan_body: str) -> list[tuple[str, str]]:
    """Extract `(ac_id, criterion_text)` pairs from `plan_body`, table
    style first (this repo's own `## Acceptance Criteria` convention),
    falling back to inline `AC-N: ...` lines. Table separator rows
    (`|---|---|`) are skipped; duplicate ids keep the first occurrence."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _AC_TABLE_ROW_RE.finditer(plan_body):
        ac_id, text = match.group(1), match.group(2)
        if ac_id in seen or set(text) <= {"-", " ", ":"}:
            continue
        rows.append((ac_id, text))
        seen.add(ac_id)
    if rows:
        return rows
    for match in _AC_INLINE_RE.finditer(plan_body):
        ac_id, text = match.group(1), match.group(2)
        if ac_id in seen:
            continue
        rows.append((ac_id, text))
        seen.add(ac_id)
    return rows


def spine_row_shape(ctx: PredicateContext) -> dict[str, Any]:
    """`:136` — whether every plan-tasks spine row's shape matches the
    closed-disposition schema, reusing `_cf_plan_tasks_disposition_shape`
    (`governed=True` unconditionally — the PM-approval arm is
    `U`-classified and excluded, not detected)."""
    rows, sentinel = _load_spine_rows(ctx)
    if sentinel is not None:
        return sentinel
    for row in rows:
        if not isinstance(row, dict):
            return {"valid": False}
        if _cf_plan_tasks_disposition_shape(row, governed=True) is not None:
            return {"valid": False}
    return {"valid": True}


def ac_reject_list(ctx: PredicateContext) -> dict[str, Any]:
    """`:137` — regex scan of each `AC-N` criterion line against
    `_AC_REJECT_PATTERNS`. "Binary pass/fail" semantics is never judged
    here."""
    if ctx.plan_body is None:
        return undetermined("no --plan supplied; cannot scan AC lines")
    hits: list[dict[str, str]] = []
    for ac_id, text in _extract_ac_lines(ctx.plan_body):
        for pattern_name, pattern in _AC_REJECT_PATTERNS:
            if pattern.search(text):
                hits.append({"ac_id": ac_id, "matched_pattern": pattern_name})
    return {"hits": hits}


def deferral_case_against(ctx: PredicateContext) -> dict[str, Any]:
    """`:143` — `case_against` presence/non-vacuity for every plan-tasks
    row whose `disposition` is one of the PM-approval-gated (deferral)
    dispositions. The disposition itself (whether the cut stands) is
    never resolved here."""
    rows, sentinel = _load_spine_rows(ctx)
    if sentinel is not None:
        return sentinel
    entries: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        disposition = row.get("disposition")
        if disposition not in _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS:
            continue
        case_against = row.get("case_against")
        present = bool(case_against) and bool(str(case_against).strip())
        entries.append({
            "id": row.get("id"),
            "case_against_present": present,
            "case_against_text": case_against if present else None,
        })
    return {"entries": entries}


def hard_constraints_block(ctx: PredicateContext) -> dict[str, Any]:
    """`:150` — presence of a hard-constraints heading among the plan's
    own fenced-section headers."""
    if ctx.plan_body is None:
        return undetermined("no --plan supplied; cannot lint for a hard-constraints block")
    return {"present": bool(_HARD_CONSTRAINTS_HEADING_RE.search(ctx.plan_body))}


def stub_spawns_subagents(ctx: PredicateContext) -> Any:
    """`:152` — whether any plan-tasks row's `body` greps for a dispatch
    verb (`dispatch`, `spawn`, `Task(`). Bare `bool` per the contract's
    scalar field."""
    rows, sentinel = _load_spine_rows(ctx)
    if sentinel is not None:
        return sentinel
    for row in rows:
        if not isinstance(row, dict):
            continue
        body = row.get("body")
        if body and _DISPATCH_VERB_RE.search(str(body)):
            return True
    return False


def concurrency_shared_state(ctx: PredicateContext) -> dict[str, Any]:
    """`:153` — candidate-only path-pattern match of each plan-tasks
    row's `surface` against known shared-state path shapes. "Is it
    actually shared" stays a `G` judgment call, never decided here."""
    rows, sentinel = _load_spine_rows(ctx)
    if sentinel is not None:
        return sentinel
    matched: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        surface = row.get("surface")
        if not surface:
            continue
        surface_str = str(surface)
        if any(pattern.search(surface_str) for pattern in _SHARED_STATE_PATH_PATTERNS):
            matched.append(surface_str)
    return {"candidate": bool(matched), "matched_paths": matched}


def chunk_index_sidecar(ctx: PredicateContext) -> dict[str, Any]:
    """`:172` — file-presence check for a chunk-index sidecar path the
    plan's own text names (a `chunk-index sidecar ... `<path>`` mention).
    A plan that names no such path is `undetermined`, not `exists:
    False` — there being nothing named is a different fact from a named
    path not existing on disk."""
    if ctx.plan_body is None:
        return undetermined("no --plan supplied; cannot locate a chunk-index sidecar reference")
    match = _CHUNK_INDEX_SIDECAR_RE.search(ctx.plan_body)
    if match is None:
        return undetermined("plan text does not name a chunk-index sidecar path")
    path_str = match.group(1).strip()
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = ctx.repo_root / candidate
    return {"exists": candidate.exists(), "path": path_str}


__all__ = [
    "spine_row_shape",
    "ac_reject_list",
    "deferral_case_against",
    "hard_constraints_block",
    "stub_spawns_subagents",
    "concurrency_shared_state",
    "chunk_index_sidecar",
]
