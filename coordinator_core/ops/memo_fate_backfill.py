"""
coordinator_core.ops.memo_fate_backfill — JSON-RPC "memo.fate_backfill" operation.

Purpose: ruling (a) of the cross-repo ask
`cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-fate-coverage-and-legacy-log-
reader.md` item 1 — derive a `distill_fate` VALUE for a pre-stamping-cutover memo
from its existing `decision:` disposition, WITHOUT writing anything (COMPUTE_ONLY;
this module never mutates a memo's frontmatter). Ruling (b), the safety-floor fix
this backfill is downstream of, lives in `coordinator_core.distill.delete_guard`
(`check_distill_fate`'s absent-fate branch) and is independent of this module —
this op derives fate CANDIDATES for a human/EM apply step to review and stamp via
`memo.transition`, it does not itself authorize a delete.

Mapping (closed, explicit — never inferred from unlisted vocabulary):
  - `decision` in {"noop", "fyi-ack"}                         -> "ephemeral"
  - `decision == "accepted"` AND `realized_by` resolves on disk -> "commitment"
    (reuses `coordinator_core.distill.delete_guard.resolve_realized_by` — the
    SAME shape-dispatch Guard 5 uses, so this op can never derive "commitment"
    from a `realized_by` value the delete guard itself would treat as
    unresolved).
  - Everything else is QUARANTINED, never coerced into a fate. This includes:
      * `decision` absent entirely,
      * `decision == "accepted"` with an unresolvable/absent `realized_by`,
      * any `decision` value outside the closed set above — this deliberately
        widens the quarantine net to `partial`/`declined`/`superseded` and
        every malformed prose-fragment literal observed in the field
        (`"Seam`, `"Both`, `"Diagnosis`, `"Adopted`, `"Consumed,`, `"Fixed,` —
        none of these match the closed set, so all fall into quarantine by
        construction, with no special-case string sniffing required).

    The source ask's third rule ("boundary/shape rulings -> ratification") is
    NOT implemented as a `decision:`-keyed mapping: no confirmed literal
    vocabulary for "boundary/shape ruling" exists in this repo's `decision:`
    corpus (which is exactly {accepted, partial, declined} — see this
    module's own quarantine-widening note above), and coercing an unconfirmed
    guess into `ratification` (which the delete guard treats as PASSING once
    `in_repo_capture` resolves) is exactly the coercion this task instructs
    against. A `decision:` value that should map to `ratification` is
    quarantined here for a human read, same as any other unmapped value —
    never silently guessed.

Negative-spec: this module performs NO writes — it does not call
`memo.transition`, does not stamp `distill_fate` onto any file, and forms no
durable store of its own. It is read-only compute over on-disk memo
frontmatter plus (for the `realized_by` resolvability check) read-only `git`
subprocess calls, identical in shape to `delete_guard.resolve_realized_by`.
The mechanically-enforceable output is a REPORT: counts per derived fate plus
a visible, capped-nothing quarantined set — an EM applies the report via
`memo.transition`'s existing `distill_fate` param, this op never does.

Negative-spec (never auto-deleted): nothing this op computes is a
delete-eligibility signal on its own — a derived fate is a CANDIDATE stamp
value for review, not a Guard-6 pass. `check_distill_fate` continues to gate
solely on the frontmatter's actual (stamped) `distill_fate:` field; this
module never feeds its output directly into a delete decision.

Spec backlink: cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-fate-coverage-and-legacy-log-reader.md § 1(a)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import yaml

from coordinator_core.distill.delete_guard import resolve_realized_by
from coordinator_core.ipc import register_op
from coordinator_core.memo_corpus import memo_corpus_root

__all__ = [
    "derive_fate",
    "collect_memo_records",
    "backfill_fates",
]

# Closed, explicit decision -> fate mapping. Never widened by inference —
# a new literal earns a spot here only by explicit ruling, same discipline
# SIDECAR_SUFFIXES documents for its own closed enum.
_EPHEMERAL_DECISIONS = frozenset({"noop", "fyi-ack"})
_COMMITMENT_DECISION = "accepted"


def _parse_frontmatter(raw: str) -> Optional[dict]:
    """Extract + parse the YAML frontmatter block of a memo file.

    Returns None (quarantine at the record level) on missing/malformed
    frontmatter — same convention as memo_fate_partition.py / memo_triage.py."""
    if not raw.startswith("---\n"):
        return None
    parts = raw.split("---\n", 2)
    if len(parts) < 2:
        return None
    try:
        fm = yaml.safe_load(parts[1])
    except Exception:  # noqa: BLE001 — quarantine parity with sibling ops
        return None
    return fm if isinstance(fm, dict) else None


def collect_memo_records(archive_dir: Path, worktree_root: Path) -> Tuple[List[dict], bool, List[dict]]:
    """Enumerate cross-repo/archive/*.md as parsed memo records, mirroring
    memo_fate_partition.collect_memo_records exactly (same degraded-scan
    signal, same os.scandir choice over Path.glob for the same
    PermissionError-swallowing reason).

    Returns `(records, degraded, read_errors)`. `read_errors` is a list of
    `{"path": <rel-or-raw path>, "reason": str}` rows — one per per-file
    `OSError` skip. Review: code-reviewer Finding (2026-08-06) — unlike the
    archive-dir-scan failure (which sets `degraded=True` and is surfaced in
    the outcome), a single unreadable file used to vanish from the corpus
    with zero trace in `backfill_fates`' output; this op's stated purpose is
    surfacing candidates for human review, so a silently-dropped memo is
    itself a review-visibility gap. Missing/malformed-frontmatter skips are
    NOT included here — that's a data-shape decision (this memo has no
    signal to derive from), not an I/O failure, and is stderr-logged only,
    matching the module's existing posture for that case."""
    records: List[dict] = []
    if not archive_dir.is_dir():
        return records, False, []

    try:
        entries = sorted(os.scandir(archive_dir), key=lambda e: e.name)
    except OSError as exc:
        print(
            f"memo.fate_backfill: cannot scan {archive_dir} — {exc}; memo corpus "
            "is degraded (NOT the same as \"no memos to backfill\")",
            file=sys.stderr,
        )
        return records, True, []

    read_errors: List[dict] = []
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        fpath = Path(entry.path)
        try:
            raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError as exc:
            print(f"memo.fate_backfill: skipping {fpath.name} — read error: {exc}", file=sys.stderr)
            try:
                rel_path = fpath.resolve().relative_to(worktree_root.resolve()).as_posix()
            except ValueError:
                rel_path = fpath.as_posix()
            read_errors.append({"path": rel_path, "reason": f"{exc.__class__.__name__}: {exc}"})
            continue

        fm = _parse_frontmatter(raw)
        if fm is None:
            print(
                f"memo.fate_backfill: skipping {fpath.name} — missing/malformed frontmatter",
                file=sys.stderr,
            )
            continue

        try:
            rel_path = fpath.resolve().relative_to(worktree_root.resolve()).as_posix()
        except ValueError:
            rel_path = fpath.as_posix()

        records.append({"memo_id": fpath.stem, "path": rel_path, "fm": fm})

    return records, False, read_errors


def derive_fate(fm: dict, repo_root: Path) -> Tuple[Optional[str], str]:
    """Derive a candidate `distill_fate` value for ONE memo's frontmatter dict.

    Returns `(fate_or_None, reason)`. `fate_or_None` is None for every
    quarantined case — the reason string always names WHY, never silently
    drops information.

    Only memos with NO existing `distill_fate` are candidates for this
    function at all (an already-stamped memo is out of scope for backfill —
    callers are expected to pre-filter, same as this op's handler does)."""
    decision = fm.get("decision")

    if not isinstance(decision, str) or not decision.strip():
        return None, "decision absent or empty — no signal to derive from"

    decision = decision.strip()

    if decision in _EPHEMERAL_DECISIONS:
        return "ephemeral", f"decision={decision!r} maps to ephemeral"

    if decision == _COMMITMENT_DECISION:
        realized_by = fm.get("realized_by")
        if not isinstance(realized_by, str) or not realized_by.strip():
            return None, "decision=accepted but realized_by absent or empty — cannot derive commitment"
        if not resolve_realized_by(realized_by.strip(), repo_root):
            return (
                None,
                f"decision=accepted but realized_by does not resolve on disk: {realized_by!r}",
            )
        return "commitment", f"decision=accepted with resolvable realized_by={realized_by!r}"

    return None, f"decision={decision!r} is outside the closed backfill mapping — quarantined"


def backfill_fates(
    records: Iterable[dict],
    *,
    worktree_root: Path,
    degraded: bool = False,
    read_errors: Optional[List[dict]] = None,
) -> dict:
    """Pure derivation core — given parsed memo records (already pre-filtered
    to absent-distill_fate memos by the caller, or not — this function
    re-checks), returns the backfill outcome.

    Returns:
        {
            "derived": {"ephemeral": [{"memo_id","path","decision","reason"}, ...],
                        "commitment": [...]},
            "quarantined": [{"memo_id","path","decision","reason"}, ...],
            "skipped_already_stamped": [{"memo_id","path","distill_fate"}, ...],
            "read_errors": [{"path": str, "reason": str}, ...],
            "counts": {"total": int, "derived_ephemeral": int,
                       "derived_commitment": int, "quarantined": int,
                       "skipped_already_stamped": int, "read_errors": int},
            "degraded": bool,
        }

    `read_errors` (from `collect_memo_records`) is passed through verbatim —
    it names per-file I/O failures that dropped a memo from the corpus
    entirely (never reached `records`), so it is surfaced alongside, not
    folded into, the four-way `counts` partition below (Review: code-reviewer
    Finding, 2026-08-06 — see `collect_memo_records`'s docstring).

    Invariant: every record lands in exactly one of {derived-ephemeral,
    derived-commitment, quarantined, skipped_already_stamped} — never zero,
    never two. The quarantined set is ALWAYS returned in full — never capped,
    truncated, or summarized-away (AC: "visible, never a silent cap")."""
    derived_ephemeral: List[dict] = []
    derived_commitment: List[dict] = []
    quarantined: List[dict] = []
    skipped: List[dict] = []

    for rec in records:
        fm = rec["fm"]
        existing_fate = fm.get("distill_fate")
        if existing_fate:
            skipped.append(
                {"memo_id": rec["memo_id"], "path": rec["path"], "distill_fate": existing_fate}
            )
            continue

        fate, reason = derive_fate(fm, worktree_root)
        row = {
            "memo_id": rec["memo_id"],
            "path": rec["path"],
            "decision": fm.get("decision"),
            "reason": reason,
        }
        if fate == "ephemeral":
            derived_ephemeral.append(row)
        elif fate == "commitment":
            derived_commitment.append(row)
        else:
            quarantined.append(row)

    read_errors = list(read_errors) if read_errors else []
    total = len(derived_ephemeral) + len(derived_commitment) + len(quarantined) + len(skipped)
    return {
        "derived": {"ephemeral": derived_ephemeral, "commitment": derived_commitment},
        "quarantined": quarantined,
        "skipped_already_stamped": skipped,
        "read_errors": read_errors,
        "counts": {
            "total": total,
            "derived_ephemeral": len(derived_ephemeral),
            "derived_commitment": len(derived_commitment),
            "quarantined": len(quarantined),
            "skipped_already_stamped": len(skipped),
            "read_errors": len(read_errors),
        },
        "degraded": degraded,
    }


@register_op("memo.fate_backfill")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "memo.fate_backfill" handler — COMPUTE_ONLY, no writes.

    Params:
        archive_dir (str, optional): override for the memo-corpus archive/ —
            test isolation, mirrors memo_fate_partition.py's convention.

    Returns: the `backfill_fates` outcome dict (see docstring above). Callers
    (an EM or a follow-up apply step) are responsible for actually stamping
    a reviewed `derived` entry via `memo.transition`'s `distill_fate` param —
    this op computes candidates only.

    Worktree resolution mirrors memo_fate_partition.py / memo_triage.py:
      - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
      - None → structured all-zero outcome (COMPUTE_ONLY, no hard failure —
        same posture memo_triage.py takes)."""
    from coordinator_core.ops.fleet._common import main_worktree_root

    if repo_root is None:
        return {
            "derived": {"ephemeral": [], "commitment": []},
            "quarantined": [],
            "skipped_already_stamped": [],
            "read_errors": [],
            "counts": {
                "total": 0,
                "derived_ephemeral": 0,
                "derived_commitment": 0,
                "quarantined": 0,
                "skipped_already_stamped": 0,
                "read_errors": 0,
            },
            "degraded": False,
        }

    worktree_root = main_worktree_root(repo_root)

    archive_override = params.get("archive_dir")
    archive_dir = (
        Path(archive_override)
        if isinstance(archive_override, str) and archive_override
        else Path(memo_corpus_root(str(worktree_root))) / "archive"
    )

    records, degraded, read_errors = collect_memo_records(archive_dir, worktree_root)
    outcome = backfill_fates(
        records, worktree_root=worktree_root, degraded=degraded, read_errors=read_errors
    )
    return outcome
