"""
coordinator_core.ops.cascade_retract — JSON-RPC "deliverable.cascade_retract" operation.

Purpose: AC6f — "a decision can be revised in one place and the cascade re-derives,
including retraction." `deliverable_cascade.py` (C6/C6g) is a one-way door on its own:
once it flips a handoff (and, per AC6g, the rows inside a roadmap-baton handoff's own
`## Tasks` spine) it has no undo. Per R1a, the EM's stamp anchoring the whole cascade
design is trusted precisely because the EM CAN be wrong — which only holds in practice
if being wrong is recoverable in one move. This module is that move.
Spec backlink: pln-terminal-state-propagation-giv-c85539 § C6d (AC6f).
See also docs/decisions/DR-263-terminal-cascade-join-closure-fanout-and-retraction-
provenance.md § 3.4, which this module implements.

THE MECHANISM (per DR-263 § 3.4 and this chunk's dispatch): retraction reads the
per-artifact provenance markers `advanced_by`/`advanced_at` (AC6e) as THE INDEX —
never a re-query of live work-state (it never re-runs `deliverable_cascade`'s own
AC6h predicate; that predicate answers "is it safe to ADVANCE", a different question
than "is it safe to UNDO an advance"). Every live `state/handoffs/*.md` record whose
own frontmatter carries `advanced_by == deliverable_id` is a retraction candidate.

BYTE-ATTRIBUTABLE, not blanket undo: for each candidate, this module diffs the file's
CURRENT on-disk text against its own last-committed (`git show HEAD:<path>`) text.
`deliverable_cascade`'s forward write (plus AC6g's row-depth write) touches ONLY a
fixed, named field set — `deployment_state`, `shipped_in`, `shipped_in_kind`, `advanced_by`,
`advanced_at`
at frontmatter depth, and `disposition`/`disposition_ref`/`disposition_detail`/
`advanced_by`/`advanced_at` at row depth (same field names AC6e/AC6g already
established, see `cascade_baton_rows._ROW_STAMP_LINE_RE`). If EVERY line the diff
touches falls inside that field set, the candidate's current state is byte-attributable
to the cascade and is safe to REVERT — by restoring the file to its own HEAD content
verbatim (never re-serialized/re-derived, so no indent-reconstruction risk: this
module only ever copies bytes it already has, it never re-emits YAML). If ANY touched
line falls outside that set — a human hand-edited the body, a ceremony touched another
field, or the sibling trigger (C6b) advanced the SAME file for a DIFFERENT deliverable_id
since — the candidate is REFUSED and named, never reverted (AC6f, DR-263 § 3.4).
Reverting a file that diverges outside the cascade's own field set would un-ship
whatever that divergence represents, which is exactly the hazard AC6f exists to avoid.

Field-NAME matching alone is not enough at row depth: `advanced_by`/`advanced_at`
are written on EVERY advanced row, so a human editing a DIFFERENT, cascade-untouched
row's `disposition:` (or any row sharing this field vocabulary for any reason)
produces a line that lexically matches the field set without being this cascade's
own write. A row-depth field line is therefore additionally position-correlated
against this candidate's own row provenance (`_attributable_row_spans`): the line
must fall inside a `## Tasks` row whose OWN on-disk `advanced_by` currently names
this run's `deliverable_id`, not merely share a field name with one that does. A
row failing that correlation is refused and named, same as any other out-of-set
divergence — never silently reverted with it.

Row depth reverts for free: AC6g's row stamps live inside the SAME handoff file this
module already restores to HEAD content, so reverting the frontmatter flip and
reverting the row-level `disposition`/`advanced_by`/`advanced_at` stamps are the SAME
write, not two — "the cascade re-deriving every projection, including the AC6g depth"
(C6d body) falls out of construction rather than needing a second row-aware pass.

Scoping call (named explicitly per this chunk's own instruction to say so precisely
rather than diverge silently): this module's byte-attributable check is a git-HEAD
diff, so it can only recover a cascade write that is STILL UNCOMMITTED relative to
HEAD — the ordinary case for this repo's own workflow (an EM fires the cascade via a
plan stamp mid-session and notices a problem before committing; every chunk in THIS
plan's own execution is itself left uncommitted for the EM per that same convention).
A candidate whose cascade write has ALREADY been committed reads as "current text ==
HEAD text" (nothing to diff) and is REFUSED, named `"already committed — nothing
uncommitted to revert"` — this module does not walk git history to unwind a committed
commit, which is a materially different (and materially more destructive) operation
than restoring an uncommitted working-tree write. A future extension wanting to
retract a committed cascade write should read this docstring before assuming the
mechanism generalizes for free.

Archived-since candidates: a candidate this cascade advanced may since have been
archived (moved `state/handoffs/*.md` -> `archive/handoffs/YYYY-MM/*.md` by a
ceremony) — no longer live, so absent from this module's live-only candidate scan.
That absence is exactly "touched since... by a ceremony" (AC6f) and must be NAMED,
not silently skipped — this module additionally scans `archive/handoffs/` (month-
nested, `rglob`, mirroring `handoff_transition._resolve_blocker_deployment_state`'s
own per-root discrimination) for any record carrying `advanced_by == deliverable_id`
and reports each as refused, unconditionally (an archived handoff is terminal by
definition; there is nothing this module could safely revert it to).

Self-registration: importing this module calls register_op("deliverable.cascade_retract")
as a side-effect. Added to coordinator_core/ops/__init__.py's eager-import table
(same chunk) to trigger registration at start_server() time.

Negative-spec:
  - Does NOT re-run `deliverable_cascade._predicate_refusal` (AC6h) — that predicate
    answers a different question (is it safe to ADVANCE this live handoff now) than
    this module's own (is this handoff's CURRENT state still exactly what the cascade
    wrote). Re-running AC6h here would be the "re-query" DR-263 § 3.4 explicitly rules
    out as the retraction index.
  - Does NOT widen or re-derive `stamp_shipped_in`/`build_ship_mutate` — a revert never
    computes a NEW value for any field, it only restores a byte-identical prior one.
  - Does NOT touch `docs/plans/*.md` or any plan-stamp state — reverting a handoff's
    deployment_state does not, and per R1a's anchor should not, un-stamp the plan
    itself; the EM's plan-implemented stamp is a separate, deliberate act this module
    has no opinion on.
  - Does NOT walk git history past `HEAD` — see Scoping call above.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from coordinator_core.dag import _read_meta
from coordinator_core.execute_plan_assemble.row_spans import (
    _find_row_spans_in_plan,
    _parse_spine_rows,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.win_portability import no_console_creationflags

_SUBPROCESS_TIMEOUT_SEC = 15

#: The exact field set `deliverable_cascade.py` (frontmatter depth) and
#: `cascade_baton_rows.py` (row depth) ever write. See module docstring
#: § BYTE-ATTRIBUTABLE. Deliberately the union of `deliverable_cascade._advance_one`'s
#: two frontmatter fields (`advanced_by`/`advanced_at`, plus the ship-mutate's own
#: `deployment_state`/`shipped_in`) and `cascade_baton_rows._ROW_STAMP_LINE_RE`'s
#: four row fields — never widened independently of those two writers.
#:
#: Review: coordinator:code-reviewer — content-only, this regex cannot tell
#: "this line's field name happens to match" from "this is the specific
#: field the cascade wrote". It is now ONLY the first gate; every row-depth
#: match is additionally position-correlated against this candidate's own
#: row provenance in `_divergence_reason` below before being accepted.
_CASCADE_FIELD_LINE_RE = re.compile(
    r"^[ \t]*(deployment_state|shipped_in|shipped_in_kind|disposition(?:_ref|_detail)?"
    r"|advanced_by|advanced_at):[ \t]"
)

#: Row-depth-only subset of `_CASCADE_FIELD_LINE_RE` — mirrors
#: `cascade_baton_rows._ROW_STAMP_LINE_RE` exactly. A line matching this is
#: ambiguous between frontmatter and row depth by field name alone
#: (`advanced_by`/`advanced_at` are written at BOTH depths); position
#: inside a `## Tasks` row span is what disambiguates it, not the name.
_ROW_DEPTH_FIELD_LINE_RE = re.compile(
    r"^[ \t]*(disposition(?:_ref|_detail)?|advanced_by|advanced_at):[ \t]"
)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Own local copy of the module-private git subprocess wrapper — same
    established per-module convention this package already follows (see e.g.
    `deliverable_cascade._validate_fm`'s docstring note on why each mutating
    module keeps its own local copy rather than importing another module's
    private symbol)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _git_show_head(repo_root: Path, path_rel: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (content, error). `content` is None with `error` set when `path_rel`
    has no resolvable HEAD version (untracked, or HEAD itself unresolvable)."""
    try:
        result = _run_git(["show", f"HEAD:{path_rel}"], repo_root)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"git show HEAD:{path_rel} failed: {exc}"
    if result.returncode != 0:
        return None, (result.stderr or "").strip() or f"git show HEAD:{path_rel} failed"
    return result.stdout, None


# ---------------------------------------------------------------------------
# Candidate collection — provenance markers as the retraction index (never a
# re-query of live work-state; see module docstring).
# ---------------------------------------------------------------------------


def _scan_dir_for_advanced_by(base_dir: Path, deliverable_id: str, recursive: bool) -> List[Path]:
    if not base_dir.is_dir():
        return []
    globber = base_dir.rglob if recursive else base_dir.glob
    matches: List[Path] = []
    for path in sorted(globber("*.md")):
        if not path.is_file():
            continue
        try:
            fm = _read_meta(str(path))
        except Exception:  # noqa: BLE001 — quarantine an unreadable/malformed record
            continue
        if not fm:
            continue
        advanced_by = fm.get("advanced_by")
        if isinstance(advanced_by, str) and advanced_by.strip() == deliverable_id:
            matches.append(path)
    return matches


def _collect_candidates(worktree_root: Path, deliverable_id: str) -> tuple[List[Path], List[Path]]:
    """Returns (live_candidates, archived_candidates) — every record (live or
    archived) whose OWN frontmatter carries `advanced_by == deliverable_id`.
    `state/handoffs/` is scanned non-recursively (mirrors `deliverable_cascade`'s
    own flat containment discipline); `archive/handoffs/` is month-nested and
    scanned recursively (mirrors `handoff_transition._resolve_blocker_deployment_
    state`'s own per-root discrimination)."""
    live = _scan_dir_for_advanced_by(
        worktree_root / "state" / "handoffs", deliverable_id, recursive=False
    )
    archived = _scan_dir_for_advanced_by(
        worktree_root / "archive" / "handoffs", deliverable_id, recursive=True
    )
    return live, archived


# ---------------------------------------------------------------------------
# Byte-attributable diff gate — refuse-and-name, never blind-revert.
# ---------------------------------------------------------------------------


def _row_spans_for_divergence(
    current_text: str, deliverable_id: str, path_rel: str
) -> tuple[List[tuple[int, int]], List[tuple[int, int]]]:
    """Returns `(all_row_spans, attributable_row_spans)` — whole-file
    `(start, end)` line-spans (as produced by `_find_row_spans_in_plan`) for
    every `## Tasks` row, and the subset of those THIS cascade run advanced
    for `deliverable_id` (every row whose OWN on-disk `advanced_by`
    currently equals `deliverable_id` — the same row-level provenance
    marker `cascade_baton_rows._stamp_row_provenance` writes, used here as
    the retraction index at row depth exactly as the handoff's own
    frontmatter `advanced_by` is used to select the candidate itself — see
    module docstring § THE MECHANISM).

    `all_row_spans` is what distinguishes ROW depth from FRONTMATTER depth
    for a field name (`advanced_by`/`advanced_at`) both depths write — a
    line outside every row span is frontmatter/other, governed by
    `_CASCADE_FIELD_LINE_RE` alone; a line inside one additionally needs the
    OWNING row's span to be in `attributable_row_spans`.

    Fails closed on the attributable half: a spine that cannot be parsed,
    or is absent, yields an empty `attributable_row_spans` -- so a
    row-depth touch is refused as unattributable rather than accepted, on
    ANY row-provenance ambiguity."""
    rows, parse_error = _parse_spine_rows(current_text, path_rel)
    if parse_error is not None or not rows:
        return [], []
    lines = current_text.splitlines(keepends=True)
    spans = _find_row_spans_in_plan(lines, current_text)
    spans_by_id = {chunk_id: (start, end) for start, end, chunk_id in spans}
    all_spans = list(spans_by_id.values())
    attributable: List[tuple[int, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        chunk_id = row.get("id")
        if chunk_id is None:
            continue
        chunk_id = str(chunk_id)
        advanced_by = row.get("advanced_by")
        if isinstance(advanced_by, str) and advanced_by.strip() == deliverable_id and chunk_id in spans_by_id:
            attributable.append(spans_by_id[chunk_id])
    return all_spans, attributable


def _divergence_reason(
    head_text: str, current_text: str, deliverable_id: str, path_rel: str
) -> Optional[str]:
    """Returns a human-readable refusal reason when `current_text` diverges from
    `head_text` on any line outside `_CASCADE_FIELD_LINE_RE`'s field set, on a
    row-depth field line whose OWNING ROW this cascade run did not itself
    advance (see `_row_spans_for_divergence` -- Review: coordinator:code-reviewer,
    a content-only field-name match alone cannot tell a human's edit to a
    DIFFERENT, cascade-untouched row from the cascade's own write, since both
    use the same field vocabulary), or when a HEAD line was deleted outright
    (the cascade's writers never delete a line). Returns None when the ENTIRE
    diff is attributable to the cascade's own field set AND, for every
    row-depth line, the cascade's own row-provenance -- i.e. safe to revert by
    restoring `head_text` verbatim."""
    if head_text == current_text:
        return "already committed — nothing uncommitted to revert (see module docstring § Scoping call)"

    old_lines = head_text.splitlines(keepends=True)
    new_lines = current_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    all_spans, attributable_spans = _row_spans_for_divergence(current_text, deliverable_id, path_rel)

    def _in_any_row(idx: int) -> bool:
        return any(start <= idx < end for start, end in all_spans)

    def _in_attributable_row(idx: int) -> bool:
        return any(start <= idx < end for start, end in attributable_spans)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            first = old_lines[i1] if i1 < len(old_lines) else ""
            return (
                "touched since by something other than the cascade -- an original "
                f"line was removed, which the cascade's own writers never do "
                f"(first diverging line: {first!r})"
            )
        for idx, line in enumerate(old_lines[i1:i2], start=i1):
            if not _CASCADE_FIELD_LINE_RE.match(line):
                return (
                    "touched since by something other than the cascade -- a change "
                    "outside deployment_state/shipped_in/shipped_in_kind/disposition/disposition_ref/"
                    f"disposition_detail/advanced_by/advanced_at was found "
                    f"(first diverging line: {line!r})"
                )
        for idx, line in enumerate(new_lines[j1:j2], start=j1):
            if not _CASCADE_FIELD_LINE_RE.match(line):
                return (
                    "touched since by something other than the cascade -- a change "
                    "outside deployment_state/shipped_in/shipped_in_kind/disposition/disposition_ref/"
                    f"disposition_detail/advanced_by/advanced_at was found "
                    f"(first diverging line: {line!r})"
                )
            if _ROW_DEPTH_FIELD_LINE_RE.match(line) and _in_any_row(idx) and not _in_attributable_row(idx):
                return (
                    "touched since by something other than the cascade -- a "
                    "row-depth field changed on a row this cascade run did not "
                    "itself advance (its own `advanced_by` provenance does not "
                    f"name this deliverable_id) (first diverging line: {line!r})"
                )
    return None


# ---------------------------------------------------------------------------
# Write — restore HEAD content verbatim, one locked_rmw per candidate.
# ---------------------------------------------------------------------------


def _retract_one(
    candidate_path: Path, repo_root: Path, path_rel: str, deliverable_id: str
) -> tuple[bool, str]:
    """Attempt to retract a single candidate. Returns (reverted, reason) — `reason`
    is a refusal string when `reverted` is False, or a success message when True.
    Never raises — every failure mode folds into a named refusal."""
    head_text, head_error = _git_show_head(repo_root, path_rel)
    if head_text is None:
        return False, f"no HEAD version resolvable: {head_error}"

    _state = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        divergence = _divergence_reason(head_text, old_text, deliverable_id, path_rel)
        if divergence is not None:
            raise MutateAbort(divergence)
        if head_text == old_text:
            # Only reachable if HEAD changed between the pre-lock read above and
            # the locked read here (concurrent commit) — treat as the same
            # already-committed refusal, not a crash.
            raise MutateAbort(
                "already committed — nothing uncommitted to revert "
                "(see module docstring § Scoping call)"
            )
        _state["applied"] = True
        _state["message"] = f"reverted {candidate_path} to its own last-committed (HEAD) content"
        return head_text

    try:
        locked_rmw(candidate_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return False, f"handoff not found at retraction time: {candidate_path}"
    except LockTimeout as exc:
        return False, f"timed out waiting for file lock on {candidate_path}: {exc}"
    except MutateAbort as exc:
        return False, (exc.args[0] if exc.args else "mutation aborted")

    return bool(_state["applied"]), _state["message"]


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("deliverable.cascade_retract")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "deliverable.cascade_retract" handler — AC6f's retraction entrypoint.

    Required params:
        deliverable_id (str) — the SAME join key a prior `deliverable.cascade_terminal`
                                call advanced against. Every live or archived handoff
                                carrying `advanced_by == deliverable_id` is a candidate
                                (the provenance index — see module docstring).

    Returns:
        {
          "exit_code": 0 | 1,
          "deliverable_id": ...,
          "candidates_matched": <int>,
          "reverted": [{"handoff_path": ..., "message": ...}, ...],
          "refused":  [{"handoff_path": ..., "reason": ...}, ...],
          "error": <str, present iff exit_code==1>,
        }

    Failure posture (mirrors `deliverable_cascade`'s own "resolves nothing is never
    silent" discipline): exit_code is 1 whenever `reverted` is empty — zero
    candidates matched deliverable_id, or every matched candidate was refused.
    """
    deliverable_id: str = (params.get("deliverable_id") or "").strip()

    if not deliverable_id:
        return {
            "exit_code": 1,
            "deliverable_id": deliverable_id,
            "candidates_matched": 0,
            "reverted": [],
            "refused": [],
            "error": "deliverable.cascade_retract: 'deliverable_id' is required",
        }
    if repo_root is None:
        return {
            "exit_code": 1,
            "deliverable_id": deliverable_id,
            "candidates_matched": 0,
            "reverted": [],
            "refused": [],
            "error": "deliverable.cascade_retract: repo_root is required (no founding root available)",
        }

    worktree_root = main_worktree_root(repo_root)
    live_candidates, archived_candidates = _collect_candidates(worktree_root, deliverable_id)

    reverted: List[dict] = []
    refused: List[dict] = []

    for path in archived_candidates:
        refused.append(
            {
                "handoff_path": str(path),
                "reason": (
                    "archived since the cascade fired -- terminal by definition, "
                    "nothing safe to revert it to"
                ),
            }
        )

    for path in live_candidates:
        try:
            path_rel = path.relative_to(worktree_root).as_posix()
        except ValueError:
            refused.append(
                {"handoff_path": str(path), "reason": "candidate path escapes the worktree root"}
            )
            continue
        did_revert, message = _retract_one(path, repo_root, path_rel, deliverable_id)
        if did_revert:
            reverted.append({"handoff_path": str(path), "message": message})
        else:
            refused.append({"handoff_path": str(path), "reason": message})

    candidates_matched = len(live_candidates) + len(archived_candidates)
    result = {
        "exit_code": 0 if reverted else 1,
        "deliverable_id": deliverable_id,
        "candidates_matched": candidates_matched,
        "reverted": reverted,
        "refused": refused,
    }
    if not reverted:
        if not candidates_matched:
            result["error"] = (
                f"deliverable.cascade_retract: no handoff (live or archived) carries "
                f"advanced_by={deliverable_id!r} -- nothing to retract"
            )
        else:
            result["error"] = (
                f"deliverable.cascade_retract: {candidates_matched} candidate(s) matched "
                f"advanced_by={deliverable_id!r} but every one was refused -- see 'refused'"
            )
    return result
