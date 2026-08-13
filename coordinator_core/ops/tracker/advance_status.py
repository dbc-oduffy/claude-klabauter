"""
coordinator_core.ops.tracker.advance_status — tracker.advance_status op.

Purpose: `coordinator/skills/enrich-and-review/SKILL.md` (Phase 2.5, 4.5, 6) names
this op — "the advance-tracker-status op — claude-klabauter's `tracker.advance_status`
(`coordinator_core/ops/tracker/advance_status.py`, `register_op
"tracker.advance_status"`)" — as the single call that flips a named set of stubs'
status cells in a plan chunk directory's "tracker README" (a markdown status table,
one row per stub — see `coordinator/docs/wiki/delegate-execution.md` § Phase 1.5 /
Re-dispatch budget for the one concrete on-disk row shape documented anywhere in
that repo: `| chunk-2A | Execution in progress (attempt 2/3) | ... |`).

Prior to this file: a grep of `coordinator_core` for `advance_tracker_status`,
`advance-tracker-status`, and a tracker-status `register_op` returned zero hits —
this op did not exist. This module is the first implementation. See "Prior-art
gap" and "Write-target classification" below before reusing this module as a
precedent for a second tracker.* op.

What "advance" means here (pinned against the SKILL.md call sites, all three of
which pass a `to_status` computed by the CALLER, never derived by this op):
  - Phase 2.5 (before enrichers launch):  to_status="Enrichment in progress"
  - Phase 4.5 (before reviewers launch):  to_status="Under review"
  - Phase 6    (after review completes):  to_status="Enriched and reviewed"
This op does NOT own the status-value vocabulary or validate to_status against an
enum — the three SKILL.md call sites are the only callers today and each supplies
its own literal string. Widening to a frozen enum is a future caller's decision,
not this op's.

Row-matching contract (the "conservative reading" — see Ambiguity note below):
  - Only markdown TABLE ROWS are recognized (a line whose stripped form contains
    at least two `|` characters). No other tracker shape (freeform bullet list,
    YAML front-matter, etc.) is supported — SKILL.md and delegate-execution.md
    never describe one, and guessing at an undocumented shape would silently
    mismatch a real tracker on first use.
  - A row's stub id is its FIRST cell (`| <id-cell> | <status-cell> | ... |`),
    normalized by stripping whitespace and one layer of surrounding `**`, `__`,
    or `` ` `` markdown emphasis/code markers. It matches `stub_id` if the
    normalized cell equals `stub_id`, `chunk-<stub_id>`, or `stub-<stub_id>`
    (case-insensitive) — covering both the bare-id and `chunk-`-prefixed forms
    SKILL.md's own text uses interchangeably ("stubs with status ...", "| chunk-2A |").
  - A row's status is its SECOND cell, replaced verbatim with `to_status`. Any
    trailing parenthetical (e.g. the `(attempt 2/3)` re-dispatch-budget annotation
    delegate-execution.md describes as "tracker README status column (coordinator-
    owned)") is DROPPED, not merged — this op sets the base status text only; a
    caller that needs to preserve an attempt-count annotation must fold it into
    the `to_status` string it passes. This is the conservative reading: SKILL.md's
    only three documented to_status values never include a parenthetical, and
    attempting to parse-and-preserve an undocumented annotation format would be
    guessing at a shape this op has no on-disk example of.

Ambiguity note (Bar: "implement the conservative reading and state the ambiguity"):
  - No tracker README on disk anywhere in coordinator-claude or claude-klabauter was found
    at authoring time (both repos greped clean for `Pending enrichment` /
    `Enrichment in progress` table rows) — the table shape above is reconstructed
    from doctrine prose plus the ONE concrete row delegate-execution.md quotes, not
    validated against a real fixture. If a real tracker README turns out to use a
    different column order or a non-table shape, this op will report every
    requested stub_id as "not found" (fail-loud, per the ALL-OR-NONE contract
    below) rather than silently mismatching.
  - Multiple rows matching the same stub_id in one tracker file is treated as
    ambiguous and fails the WHOLE call (see `raise_group` in `_resolve_targets`) —
    detect-then-fail-loud on ambiguity, not detect-then-silently-pick-first.

ALL-OR-NONE contract (crash-safety / idempotency, per the Bar's "correctness
matters more than completeness" — Idempotency and crash-safety are the properties
to get right):
  - Every stub_id in one call must resolve to EXACTLY ONE row before any byte of
    the file is rewritten. If any stub_id is not-found or ambiguous, the handler
    raises ValueError naming every offending id in one message and the file is
    NOT touched — no partial flip, no half-updated tracker.
  - Per-row idempotent: a row already at `to_status` is reported in `unchanged`,
    not rewritten. If EVERY requested row is already at `to_status`, the file is
    not opened for write at all (`changed: False`) — re-running the same call
    after a crash-and-retry is a safe no-op, never a spurious second write.
  - The write is a single atomic `tempfile.mkstemp(dir=parent) + os.replace` over
    the WHOLE file content (every unmutated line copied through byte-for-byte,
    including its original line-ending — `\n`/`\r\n`/`\r` are read per-line and
    reattached, never normalized) — a crash mid-write leaves either the old file
    or the new one, never a half-written table.

Write-target classification — ratified (write target) / still provisional
(self-commit) — READ BEFORE treating this as settled precedent:
  `coordinator_core/ipc.py`'s negative-spec is unconditional: "The core is
  read-mostly for coordinator substrate. Handlers MUST NOT write coordinator
  substrate ... Writers of substrate remain the EM/agent's job via bash/git
  surfaces" — and every existing exception to that rule (DR-211 fleet archival,
  DR-212 handoff frontmatter, DR-213 queue additive-create, DR-214 memo.send,
  DR-216 changelog/completion/review-trail, DR-218 review-trail reap, DR-228
  distill-disposal) is its OWN standalone, PM-ratified decision record with its
  own five-bound admission test, per DR-212's own framing ("a categorically
  stronger crossing ... warrants its own named, greppable decision record").

  RATIFIED — the write target itself. `tracker.advance_status`'s write target —
  a caller-supplied path to a plan chunk directory's tracker README, which is
  neither a fixed noun nor confined to a namespace any of the six prior DR
  carve-outs covers (DR-216's `docs/plans/*.md` carve-out is append-only
  session records via `plan.append_session`; a tracker README status-cell flip
  is an in-place REWRITE of existing content, the opposite semantic) — is now a
  PM-ratified, authorized mutating carve-out per `coordinator-claude
  docs/decisions/DR-094-tracker-advance-status-write-target-carveout.md`
  (PM-ratified 2026-07-25). DR-094 ratifies the write **as this module is
  currently built, no wider**: exactly one caller-supplied, path-contained
  tracker README per call, status-cell (second column) only, ALL-OR-NONE across
  every requested row, per-row idempotent, every other byte of the file passing
  through unchanged. It is explicitly NOT precedent for a different write
  target — a second `tracker.*` op, or a widening of this op's own confinement
  (non-table shape, `to_status` enum validation, attempt-count preservation),
  needs its own DR, per the same discipline DR-212 established. This module was
  already built to the SAME technical bar the existing carve-outs require
  (per-row idempotent, atomic write, content-scoped, path-contained), which is
  what let DR-094 ratify it as-is rather than requiring a rebuild.

  STILL PROVISIONAL — handler-issued commit. DR-094 explicitly does NOT ratify
  a handler-issued git commit for this write (see its "Bounds of the
  authorization" § Out of scope); the op's own self-imposed no-self-commit
  restraint stands unchanged and is not relaxed by DR-094. SKILL.md's own prose
  says this op "lands the tracker update in one scoped commit (SC-DR-008)" —
  i.e. it self-commits. This module deliberately does NOT self-commit: every
  existing carve-out except DR-211's archival-move family (whose atomicity
  genuinely requires bundling rename+commit) forbids a handler-issued git
  commit, and self-committing would be an independent boundary crossing that
  DR-094 does not settle — commit-issuance for this noun is a separate,
  not-yet-made decision. Commit timing for the tracker README write is the
  caller's (EM's) responsibility, same as
  `changelog.append_day`/`queue.append`/`handoff.transition` et al. — SKILL.md's
  "lands ... in one scoped commit" phrasing should be read as "the EM commits
  the tracker update immediately after this op returns," not as a property of
  the op itself, until/unless a future, separate DR sanctions handler-issued
  commits for this noun.

Spec backlink: coordinator-claude coordinator/skills/enrich-and-review/SKILL.md § Phase 2.5/4.5/6
Spec backlink: coordinator-claude coordinator/docs/wiki/delegate-execution.md § Phase 1.5, Re-dispatch budget
Spec backlink: coordinator-claude commit f83fbb52 (names this op; documents the prior-art gap)
Spec backlink: coordinator-claude docs/decisions/DR-094-tracker-advance-status-write-target-carveout.md
  (write-target ratification; see "Write-target classification" above for bounds)

Negative-spec:
  - Does NOT issue a git commit (see "Write-target classification" § STILL
    PROVISIONAL above — DR-094 does not ratify handler-issued commits).
  - Does NOT validate `to_status` against an enum — caller-supplied literal only.
  - Does NOT parse or preserve a trailing attempt-count parenthetical.
  - Does NOT support a non-table tracker shape.
  - Does NOT touch any line of the file other than the rows it resolves and
    changes — every other byte (including line endings) passes through unchanged.
  - Does NOT write rag's relational store (dual-write ban, DR-208 Invariant-1).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.wire_paths import rel_id

_LINE_ENDINGS = ("\r\n", "\n", "\r")

# Markdown emphasis/code wrappers stripped (one layer) from a table cell before
# comparing it against a caller-supplied stub id.
_CELL_WRAPPERS = ("**", "__", "`")


class TrackerRowError(ValueError):
    """Raised when one or more requested stub_ids do not resolve to exactly one
    tracker-README row (not-found or ambiguous) — see module docstring's
    ALL-OR-NONE contract. Carries every offending id in one message so a caller
    sees the full picture in one round trip, not one id at a time."""


def _split_line_ending(line: str) -> Tuple[str, str]:
    """Split a line into (content, ending). ending is one of "", "\\n", "\\r\\n",
    "\\r" — preserved verbatim so a rewritten line reattaches the SAME ending the
    original had (never normalized), per the module docstring's atomic-write note."""
    for ending in _LINE_ENDINGS:
        if line.endswith(ending):
            return line[: -len(ending)], ending
    return line, ""


def _normalize_cell(raw: str) -> str:
    """Strip whitespace and one layer of surrounding markdown emphasis/code
    markers (`**`, `__`, `` ` ``) from a table cell, for stub-id comparison."""
    s = raw.strip()
    for wrapper in _CELL_WRAPPERS:
        if len(s) >= 2 * len(wrapper) and s.startswith(wrapper) and s.endswith(wrapper):
            s = s[len(wrapper) : -len(wrapper)].strip()
    return s


def _cell_matches_stub_id(cell: str, stub_id: str) -> bool:
    normalized = _normalize_cell(cell).lower()
    target = stub_id.lower()
    return normalized in (target, f"chunk-{target}", f"stub-{target}")


def _is_table_row(content: str) -> bool:
    """A line "looks like" a markdown table row iff its stripped form contains
    at least two `|` characters (opening + one cell boundary) — matches the
    header, separator (`|---|---|`), and data-row shapes alike; separator/header
    rows never match a real stub_id in `_cell_matches_stub_id` so they self-
    exclude without special-casing."""
    return content.strip().count("|") >= 2


def _split_cells(content: str) -> List[str]:
    return content.split("|")


def validate_stub_ids(stub_ids) -> List[str]:
    if not isinstance(stub_ids, list) or not stub_ids:
        raise ValueError(
            f"tracker.advance_status: stub_ids must be a non-empty list, got {stub_ids!r}"
        )
    seen = []
    for sid in stub_ids:
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError(
                f"tracker.advance_status: every stub_id must be a non-empty string, got {sid!r}"
            )
        if sid not in seen:
            seen.append(sid)
    return seen


def validate_to_status(to_status) -> str:
    if not isinstance(to_status, str) or not to_status.strip():
        raise ValueError(
            f"tracker.advance_status: to_status must be a non-empty string, got {to_status!r}"
        )
    if "|" in to_status or "\n" in to_status or "\r" in to_status:
        raise ValueError(
            "tracker.advance_status: to_status must not contain '|' or a newline "
            f"(would corrupt the tracker table structure), got {to_status!r}"
        )
    return to_status


def _resolve_targets(lines: List[str], stub_ids: List[str]) -> Dict[str, int]:
    """Return {stub_id: line_index} for every stub_id, or raise TrackerRowError
    naming every id that resolved to zero or more-than-one row (ALL-OR-NONE —
    see module docstring)."""
    matches: Dict[str, List[int]] = {sid: [] for sid in stub_ids}
    for idx, raw_line in enumerate(lines):
        content, _ending = _split_line_ending(raw_line)
        if not _is_table_row(content):
            continue
        cells = _split_cells(content)
        if len(cells) < 3:
            continue
        id_cell = cells[1]
        for sid in stub_ids:
            if _cell_matches_stub_id(id_cell, sid):
                matches[sid].append(idx)

    not_found = [sid for sid, idxs in matches.items() if len(idxs) == 0]
    ambiguous = [sid for sid, idxs in matches.items() if len(idxs) > 1]
    if not_found or ambiguous:
        parts = []
        if not_found:
            parts.append(f"not found: {not_found}")
        if ambiguous:
            parts.append(f"ambiguous (multiple matching rows): {ambiguous}")
        raise TrackerRowError(
            "tracker.advance_status: could not resolve every stub_id to exactly "
            "one tracker-README row (" + "; ".join(parts) + ") — no row was "
            "modified (ALL-OR-NONE)"
        )
    return {sid: idxs[0] for sid, idxs in matches.items()}


def advance_status(tracker_file: Path, stub_ids: List[str], to_status: str) -> dict:
    """Pure, synchronous core: read `tracker_file`, flip the status cell of every
    row matching a stub_id in `stub_ids` to `to_status`, and atomically rewrite
    the file iff at least one row actually changed.

    Returns {updated: [stub_id, ...], unchanged: [stub_id, ...], changed: bool}
    (both id lists in the caller's original `stub_ids` order).

    Raises:
        ValueError — malformed stub_ids/to_status (see validate_stub_ids /
            validate_to_status) — validated here too so a direct caller of this
            pure function (not just the JSON-RPC handler) gets the same guard.
        TrackerRowError — a stub_id resolved to zero or >1 rows (ALL-OR-NONE;
            file is not touched).
        OSError — the file could not be read (caller pre-checks existence, but a
            TOCTOU race — e.g. the file removed between check and read — is not
            hidden here).
    """
    stub_ids = validate_stub_ids(stub_ids)
    to_status = validate_to_status(to_status)
    with open(tracker_file, "r", encoding="utf-8", newline="") as fh:
        content = fh.read()
    lines = content.splitlines(keepends=True)

    targets = _resolve_targets(lines, stub_ids)

    updated: List[str] = []
    unchanged: List[str] = []
    for sid in stub_ids:
        idx = targets[sid]
        line_content, ending = _split_line_ending(lines[idx])
        cells = _split_cells(line_content)
        current_status = cells[2].strip()
        if current_status == to_status:
            unchanged.append(sid)
            continue
        cells[2] = f" {to_status} "
        lines[idx] = "|".join(cells) + ending
        updated.append(sid)

    if not updated:
        return {"updated": [], "unchanged": unchanged, "changed": False}

    new_content = "".join(lines)
    parent = tracker_file.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_content)
        os.replace(tmp_path, tracker_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            print(
                f"skip: advance_status: os.unlink(tmp_path) failed: {sys.exc_info()[1]}",
                file=sys.stderr,
            )
        raise

    return {"updated": updated, "unchanged": unchanged, "changed": True}


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("tracker.advance_status")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """tracker.advance_status — flip a set of stubs' status cells in a plan
    chunk directory's tracker README, atomically, ALL-OR-NONE. See module
    docstring for the full contract and the provisional-classification note.

    Wire contract:
        params: {tracker_path: str, stub_ids: list[str], to_status: str}
        ->      {tracker_path: str (repo-relative), to_status: str,
                  updated: [str], unchanged: [str], changed: bool}

    `tracker_path` is resolved against the caller's worktree (`_OP_KEY_SCOPE:
    common_dir` — `repo_root` is the git common dir; `main_worktree_root`
    derives the worktree) and MUST resolve inside it (`_path_guard.contained_path`
    — blast-radius containment, not a privilege boundary; see that module's own
    threat-model note). An absolute `tracker_path` outside the worktree, or a
    relative one that escapes it via `..`, raises ValueError.

    Raises:
        RuntimeError — repo_root is None (engine misconfiguration; production
            always supplies it via `_OP_KEY_SCOPE='common_dir'`).
        ValueError — malformed params, tracker_path not contained in the
            worktree, tracker_path does not resolve to an existing file.
        TrackerRowError (ValueError subclass) — a stub_id resolved to zero or
            >1 tracker-README rows; no row was modified.
    """
    if repo_root is None:
        raise RuntimeError(
            "tracker.advance_status: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )
    worktree = main_worktree_root(Path(repo_root))

    tracker_path_param = params.get("tracker_path")
    if not isinstance(tracker_path_param, str) or not tracker_path_param.strip():
        raise ValueError(
            f"tracker.advance_status: tracker_path must be a non-empty string, "
            f"got {tracker_path_param!r}"
        )
    stub_ids = validate_stub_ids(params.get("stub_ids"))
    to_status = validate_to_status(params.get("to_status"))

    candidate = Path(tracker_path_param)
    if not candidate.is_absolute():
        candidate = worktree / candidate
    resolved = contained_path(candidate, [worktree])
    if resolved is None:
        raise ValueError(
            f"tracker.advance_status: tracker_path {tracker_path_param!r} is not "
            f"contained within the caller's worktree {worktree}"
        )
    if not resolved.is_file():
        raise ValueError(
            f"tracker.advance_status: tracker_path {tracker_path_param!r} does not "
            f"resolve to an existing file (resolved: {resolved})"
        )

    import asyncio

    result = await asyncio.to_thread(advance_status, resolved, stub_ids, to_status)
    result["tracker_path"] = rel_id(resolved, worktree)
    result["to_status"] = to_status
    return result
