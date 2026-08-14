"""
coordinator_core.ops.handoff_gate_aging — 14d/7d awaiting_gate handoff aging predicate,
plus the C6 three-way triage signal (evidence-resolved / review-due / merely-aged).

`review-due` carries TWO distinct origins as of the gate-dependency-template-
emission-spec C2 chunk: a `kind: deadline` `gate_evidence` leg that has
elapsed (D3a, the original meaning — a prompt, never auto-promoted to
evidence-resolved), OR a prose `gate_dependency` whose entire structured
`blocked_by` graph has independently shipped out from under it
(`gate_eval.evaluate_gate_triage`'s prose-dominance contradiction carve-out,
mirroring `evaluate_gate`'s `contradiction` key). Neither origin is
auto-promoted to `evidence-resolved` — both are a prompt for a human to
look, never a silent free.

Purpose: mechanizes the aging-reconcile predicate that was pure EM-arithmetic
prose (pickup/SKILL.md § Step 3.4d, docs/wiki/spinoff-handoffs.md § Awaiting_gate
aging § Thresholds) into a single fail-loud tool call.

RETIREMENT OF THE STANDALONE BATCH NAG (2026-07-27, docs/plans/2026-07-26-
gate-resolution-widen-and-migrate.md § C16): `workday-start.md` Step 1.2 no
longer force-invokes this module as a daily batch nag. Evidence: DoE-claude
`state/audits/2026-07-27-gate-resolver-dry-run.md` (run label "post-C12c
re-run") shows `coordinator_core.reconcile.gate_eval` already surfaces every
`awaiting_gate` handoff that needs a human look on EVERY resolver pass,
unconditionally of calendar age — a non-empty `blocking_notes` or a prose
`gate_dependency` DOMINATES to `surface`/`indeterminate` regardless of how
young the handoff is (gate_eval module docstring "BLOCKING_NOTES DOMINANCE",
"PROSE-DOMINANCE PRECEDENCE"). The ONE bucket the resolver never surfaces —
`evaluate_gate`'s `not-cleared` verdict, real structured `blocked_by` edges
still legitimately open, nothing wrong — is a DELIBERATE quiet design in
`gate_eval._evaluate_structured_gate` (its own comment: "deliberately NOT
surfaced ... benign 'still gated, no action' path"), not a gap this module's
calendar override should second-guess. Net effect of running both: for the
88/107-handoff "surface" bucket in the dry run, this module's 14d/7d force-
recheck was pure duplication of a signal the resolver already re-presents
every single pass; for the 5/107 "not-cleared (benign)" bucket, nagging it
after 14 days contradicts a design call `gate_eval.py` already made on
purpose. Retiring the standalone invocation removes the duplication without
touching `gate_eval.py`'s own ratified quiet-bucket contract (out of scope
here — see this module's own docstring, not re-litigated).

STILL LIVE: `check_one` remains directly consumed by
`coordinator_core.pickup_assemble.compute_aging_verdict` as supplementary
per-handoff calendar evidence for the `jgate` judgment point
("Has this awaiting_gate handoff's gate actually cleared?") — a
fundamentally different consumer than the retired batch nag: `jgate` is
ALREADY asked, by construction, for every single `awaiting_gate` handoff on
every `/pickup`, regardless of what this predicate says. Surfacing the raw
age there is supplementary color for a judgment call that happens anyway,
not a second, competing surfacing mechanism — so it stays. The bin CLI
(`coordinator/bin/handoff-gate-aging`) also stays installed and runnable
ad hoc; only the workday-start.md morning-ceremony force-invocation retired.

Predicate — a handoff is STALE (force re-check) iff ALL THREE hold:
  1. deployment_state: awaiting_gate
  2. now - created >= 14 days (AGING_THRESHOLD_DAYS)
  3. last_gate_recheck: absent, OR now - last_gate_recheck >= 7 days (RECHECK_COOLDOWN_DAYS)

Exit codes (`scan`/`main` — no longer force-invoked by workday-start.md; kept
for any direct/ad-hoc CLI run):
  0 — no stale handoffs found, no parse errors
  1 — one or more stale handoffs found (printed to stdout); wins over 2 if both
      a stale hit and a parse error occurred in the same directory scan
  2 — internal error: missing arg, path not found, awaiting_gate handoff missing
      `created:`, or unparseable date

`--triage` (AC4, opt-in — see `main`): swaps the print/rc path to
`scan_triage`'s three-way `signal`. rc 1 if any handoff signals
`evidence-resolved`/`review-due`/`merely-aged`; else rc 2 if any signals
`parse-error`; else rc 0. Never reached without the flag.

Port of: handoff-gate-aging.sh (DoE 67202df6, 2026-07-16)
Spec backlink: DoE-claude:pln-handoff-spinoff-machinery-robu-0d0f15 § C5c
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md, chunk B3

Negative-spec (STALE predicate, `check_one`/`scan`/`main` — pre-existing, unchanged):
    - Does NOT do a full YAML parse — reuses the shared frontmatter-scalar
      extractor (_fm_util), which reads only the first frontmatter fence block,
      mirroring the bash oracle's awk-based scoped read.
    - stderr diagnostics keep the literal `handoff-gate-aging.sh: ` prefix
      (NOT renamed to the trampoline's extension-less name) — `/pickup` Step
      3.4d text says "surface the stderr diagnostic verbatim", and that text
      quotes the bash-era program name; changing the prefix would silently
      break that quoted-verbatim contract.
    - Directory scan is one level only (`*.md`, no recursion) — state/handoffs/
      is flat by design, mirroring the bash `find -maxdepth 1`.

C6 TRIAGE SIGNAL (`classify_gate`, docs/plans/2026-07-26-structured-sibling-
evidence-gates.md § C6/AC4): the 14d/7d predicate above answers only "is this
gate merely OLD" — it cannot tell a gate that is old because nobody looked
apart from a gate whose blocking evidence has actually resolved. `classify_gate`
distinguishes THREE signals for an `awaiting_gate` handoff:
    "evidence-resolved" — `coordinator_core.reconcile.gate_eval.evaluate_gate_
        triage` (C3) reports `status == "freed"`: the gate's structured
        `blocked_by` edges (or, once a caller supplies one, a `gate_evidence`
        AND-reduce) are ALL satisfied. ACTIONABLE — distinct from merely old.
    "review-due" — `evaluate_gate_triage` reports `status == "review-due"`:
        EITHER a `kind: deadline` `gate_evidence` leg has elapsed (D3a),
        OR a dominant prose `gate_dependency` has gone stale under a
        structured `blocked_by` graph that has since shipped in full
        (the prose-dominance contradiction carve-out — see
        `gate_eval.evaluate_gate_triage`'s own docstring). Both origins are
        a PROMPT for human review, never auto-promoted to
        `evidence-resolved` — this module never treats either as a freed
        gate.
    "merely-aged" — neither of the above; falls through to the pre-existing,
        UNCHANGED `check_one` 14d/7d predicate.
This module IMPORTS `evaluate_gate_triage` (C3) — it does NOT re-implement any
gate-resolution logic; every verdict returned by `classify_gate` is `gate_eval`'s
own, this function only maps `status` onto the aging-vocabulary signal name and
folds in the pre-existing aging check for the fall-through branch. Writing a
second evaluator here would be the plan's named anti-scope failure mode.

C6 SCOPE BOUNDARY (AC4, landed) — `gate_evidence` IS now auto-read off
frontmatter by `scan_triage`, once both blockers commit `6aa14244` stubbed
this against are discharged: the DoE-ratified `gate_evidence:` schema shape
was ratified in `3c5e048d`, and `coordinator_core.dag`'s truncating
`_parse_yaml_list_block` (a sequence-of-mappings value read as an opaque
scalar, dropping every more-indented continuation line) is never used here —
`_read_gate_evidence_resolved` reads via the SAME full-YAML `fm_dict` route
`coordinator_core.ops.handoff_transition._gate_recheck` already uses
(`split_frontmatter` + `yaml.safe_load(split.fm_text)`), which round-trips a
multi-leg block intact. Each on-disk leg is then LIVE re-resolved via
`handoff_transition._reresolve_gate_evidence_leg` — the same helper C4's
gate-recheck verb uses — before being handed to `classify_gate`, since
`evaluate_gate_triage`/`reduce_gate_evidence` require every I/O-kind leg to
arrive caller-pre-resolved (`{read_ok, observed, error}`) rather than the
bare on-disk declaration; a `deadline` leg's `elapsed` bool is computed the
same way. `classify_gate` itself is UNCHANGED — it still takes
`gate_evidence` as an explicit CALLER-SUPPLIED argument (mirroring
`evaluate_gate_triage`'s own contract) and never reads a file's own
frontmatter itself; `scan_triage` is simply no longer the caller that always
passes `None` — it resolves gate_evidence for every `awaiting_gate` handoff
before calling `classify_gate`, so "review-due"/"evidence-resolved" are now
reachable from a live directory scan whenever a handoff actually carries a
`gate_evidence:` block. This module still authors, infers, or backfills
nothing — a handoff with no `gate_evidence:` block (or a malformed one)
resolves to `gate_evidence=None`, identical to pre-AC4 behaviour.

Spec backlink: pln-structured-sibling-evidence-ga-6e2ceb § C6/AC4
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops._git_root_util import git_root
from coordinator_core.ops.handoff_reconcile import _collect_all_handoffs_for_gate_index
from coordinator_core.ops.handoff_transition import _read_gate_evidence_resolved
from coordinator_core.reconcile.gate_eval import evaluate_gate_triage

AGING_THRESHOLD_DAYS = 14
RECHECK_COOLDOWN_DAYS = 7

_PROG = "handoff-gate-aging.sh"  # literal program-name prefix — see negative-spec


def _parse_date(value: str) -> Optional[date]:
    """Parse a YYYY-MM-DD-ish scalar into a date, or None if unparseable."""
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        print(f"skip: _parse_date: return date.fromisoformat(value.strip()) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


def check_one(path: Path, today: date) -> Tuple[str, int]:
    """Evaluate the 14d/7d aging predicate for one handoff file.

    Returns (stdout_line_or_empty, rc) where rc is 0 (not stale / no error) or
    2 (parse error). STALE detection is communicated by a non-empty
    stdout_line, matching the bash script's "empty stdout + rc" duality —
    the printed `STALE: ...` line and the rc are independent signals so a
    directory scan can distinguish stale-hits from parse-errors per file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"{_PROG}: {path}: unreadable: {exc}", file=sys.stderr)
        return ("", 2)

    deployment_state = extract_frontmatter_scalar(text, "deployment_state")
    if deployment_state != "awaiting_gate":
        return ("", 0)

    created = extract_frontmatter_scalar(text, "created")
    if not created:
        print(
            f"{_PROG}: {path}: awaiting_gate handoff missing 'created:' field — "
            "cannot evaluate aging",
            file=sys.stderr,
        )
        return ("", 2)

    created_date = _parse_date(created)
    if created_date is None:
        print(f"{_PROG}: {path}: unparseable created date '{created}'", file=sys.stderr)
        return ("", 2)

    age_days = (today - created_date).days
    if age_days < AGING_THRESHOLD_DAYS:
        return ("", 0)

    last_recheck = extract_frontmatter_scalar(text, "last_gate_recheck")
    if last_recheck:
        recheck_date = _parse_date(last_recheck)
        if recheck_date is None:
            print(
                f"{_PROG}: {path}: unparseable last_gate_recheck date '{last_recheck}'",
                file=sys.stderr,
            )
            return ("", 2)
        recheck_age_days = (today - recheck_date).days
        if recheck_age_days < RECHECK_COOLDOWN_DAYS:
            return ("", 0)
        return (
            f"STALE: {path} (created {age_days}d ago, last_gate_recheck {recheck_age_days}d ago)",
            0,
        )

    return (f"STALE: {path} (created {age_days}d ago, last_gate_recheck absent)", 0)


def scan(target: str, today: Optional[date] = None) -> Tuple[List[str], int]:
    """Top-level driver — mirrors the bash file's FILES=(...) collection + loop.

    target: a single handoff file path, or a directory (scanned one level for
    *.md files, no recursion — state/handoffs/ is flat by design).

    Returns (stale_lines, rc) with rc arbitration: STALE wins over
    PARSE_ERROR-with-zero-stale (mirrors the bash: `PARSE_ERROR && STALE_COUNT==0`
    -> 2, else if `STALE_COUNT>0` -> 1, else 0).
    """
    if today is None:
        today = date.today()

    if os.path.isdir(target):
        files = sorted(
            os.path.join(target, name)
            for name in os.listdir(target)
            if name.endswith(".md")
        )
    else:
        files = [target]

    lines: List[str] = []
    stale_count = 0
    parse_error = False
    for f in files:
        line, rc = check_one(Path(f), today)
        if rc == 2:
            parse_error = True
        if line:
            lines.append(line)
            stale_count += 1

    if parse_error and stale_count == 0:
        return lines, 2
    if stale_count > 0:
        return lines, 1
    return lines, 0


def classify_gate(
    path: Path,
    today: date,
    worktree_root: Optional[Path] = None,
    gate_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """C6/AC4 — three-way triage signal for one handoff, on top of the
    pre-existing 14d/7d predicate. See module docstring "C6 TRIAGE SIGNAL"
    and "C6 SCOPE BOUNDARY" for the full derivation and why `gate_evidence`
    is never auto-read off frontmatter.

    `worktree_root`: repo root used to build the live+archived `blocked_by`
    resolution index (`evaluate_gate_triage`'s own structured-gate path,
    unchanged from its pre-C6 behaviour). Defaults to `git_root(cwd=path.
    parent)` when not supplied; if that also fails to resolve (not inside a
    git repo, e.g. a bare tmp_path fixture with no `.git`), `blocked_by`
    resolution degrades to an empty index — every `blocked_by` id then reads
    as unresolved, which is the FAIL-SAFE direction (never silently
    "evidence-resolved" on a corpus this function could not actually see).

    `gate_evidence`: caller-supplied, never read off `path`'s own
    frontmatter (see module docstring "C6 SCOPE BOUNDARY"). `None` (the
    default) is what every disk-scan caller passes today, since no live
    handoff carries one; a future caller (C4's gate-recheck verb) that has
    itself re-verified a `gate_evidence` block's legs may pass one in.

    Returns:
        {handoff_id, path, signal, triage_status, aging_line, aging_rc,
         scan_errors}

    `scan_errors`: threaded straight from `_collect_all_handoffs_for_gate_index`
    (empty when `worktree_root` never resolved, since no walk happened) and
    also fed into `evaluate_gate_triage` as `scan_incomplete`/`scan_errors` so
    an unscannable subtree is folded into `triage`'s own evidence/reason
    rather than silently discarded — the same axis
    `review_brightline_gate.py`'s `_from_handoff_main` propagates end-to-end.
    Review: code-reviewer — Finding 3 (P2): this caller previously discarded
    the shared walker's `scan_errors` entirely (`_scan_errors` throwaway),
    making a possibly-incomplete scan indistinguishable from a complete one
    to every downstream consumer of `classify_gate`'s output.

    `signal` is exactly one of:
        "not-gated"          — deployment_state != awaiting_gate
        "evidence-resolved"  — gate_eval status == "freed" (ACTIONABLE)
        "review-due"         — gate_eval status == "review-due" (D3a elapsed-
                                deadline prompt, OR a prose-dominance
                                contradiction carve-out — see module
                                docstring; never auto-promoted to
                                evidence-resolved)
        "merely-aged"        — gate_eval status in {"still-blocked",
                                "indeterminate"} AND the pre-existing
                                `check_one` 14d/7d predicate flags STALE
        "not-stale"           — as above, but `check_one` does not flag STALE
        "parse-error"         — `check_one`'s own rc==2 path (missing or
                                unparseable `created`/`last_gate_recheck`),
                                reached only when neither of the two gate_eval
                                signals above already answered the question

    Negative-spec:
        - Does NOT duplicate `evaluate_gate_triage`'s classification logic —
          every verdict is `gate_eval`'s (C3); this function only maps
          `status` onto the aging-vocabulary `signal` name.
        - Does NOT auto-promote a "review-due" gate to "evidence-resolved" —
          the two are checked as separate, mutually exclusive branches.
        - Does NOT read or infer a `gate_evidence:` block off `path`'s own
          frontmatter — see module docstring "C6 SCOPE BOUNDARY".
    """
    aging_line, aging_rc = check_one(path, today)

    meta = _read_meta(str(path))
    deployment_state = meta.get("deployment_state")
    handoff_id = meta.get("id") or path.stem
    if deployment_state != "awaiting_gate":
        return {
            "handoff_id": handoff_id,
            "path": str(path),
            "signal": "not-gated",
            "triage_status": None,
            "aging_line": aging_line,
            "aging_rc": aging_rc,
            "scan_errors": [],
        }

    handoff = dict(meta)
    handoff.setdefault("id", handoff_id)

    if worktree_root is None:
        resolved_root = git_root(cwd=path.parent)
        worktree_root = Path(resolved_root) if resolved_root else None

    all_handoffs: List[Dict[str, Any]] = []
    scan_errors: List[str] = []
    if worktree_root is not None:
        all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(worktree_root)

    triage = evaluate_gate_triage(
        handoff,
        all_handoffs,
        gate_evidence=gate_evidence,
        scan_incomplete=bool(scan_errors),
        scan_errors=scan_errors,
    )
    status = triage["status"]

    if status == "freed":
        signal = "evidence-resolved"
    elif status == "review-due":
        signal = "review-due"
    elif aging_rc == 2:
        signal = "parse-error"
    elif aging_line:
        signal = "merely-aged"
    else:
        signal = "not-stale"

    return {
        "handoff_id": handoff_id,
        "path": str(path),
        "signal": signal,
        "triage_status": status,
        "aging_line": aging_line,
        "aging_rc": aging_rc,
        "scan_errors": scan_errors,
    }


def scan_triage(target: str, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Directory/file driver for `classify_gate` — mirrors `scan`'s file
    collection (one level, `*.md`, no recursion) but returns every file's
    full triage dict rather than only the STALE lines.

    `gate_evidence` is resolved per-file via `_read_gate_evidence_resolved`
    (AC4 — see module docstring "C6 SCOPE BOUNDARY") before calling
    `classify_gate`; a handoff with no `gate_evidence:` block still passes
    `None`, identical to pre-AC4 behaviour.
    """
    if today is None:
        today = date.today()

    if os.path.isdir(target):
        files = sorted(
            os.path.join(target, name)
            for name in os.listdir(target)
            if name.endswith(".md")
        )
    else:
        files = [target]

    results = []
    for f in files:
        p = Path(f)
        gate_evidence = _read_gate_evidence_resolved(p, today)
        results.append(classify_gate(p, today, gate_evidence=gate_evidence))
    return results


def main(argv: List[str]) -> int:
    """CLI entry: arg validation, scan, print, return rc.

    `--triage` (AC4, opt-in, additive-only): swaps the print/rc path from the
    pre-existing `scan`/`check_one` STALE predicate (module docstring
    negative-spec — unchanged, byte-identical without the flag) to
    `scan_triage`'s three-way `signal`. Never the default — a caller that
    passes no flag gets exactly today's output shape and rc arbitration.
    """
    if not argv:
        print(f"{_PROG}: missing argument: <handoff-path-or-directory>", file=sys.stderr)
        return 2

    if argv[0] in ("--help", "-h"):
        print(f"usage: {_PROG} <handoff-path-or-directory> [--triage]")
        return 0

    args = list(argv)
    triage = "--triage" in args
    if triage:
        args = [a for a in args if a != "--triage"]

    if not args:
        print(f"{_PROG}: missing argument: <handoff-path-or-directory>", file=sys.stderr)
        return 2

    target = args[0]
    if not os.path.exists(target):
        print(f"{_PROG}: path not found: {target}", file=sys.stderr)
        return 2

    if triage:
        results = scan_triage(target)
        for result in results:
            print(
                f"{result['signal']}: {result['path']} "
                f"(handoff_id={result['handoff_id']}, triage_status={result['triage_status']})"
            )
        if any(
            result["signal"] in {"evidence-resolved", "review-due", "merely-aged"}
            for result in results
        ):
            return 1
        if any(result["signal"] == "parse-error" for result in results):
            return 2
        return 0

    lines, rc = scan(target)
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
