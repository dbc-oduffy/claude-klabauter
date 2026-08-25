"""
coordinator_core.ops.distill_apply_disposal — JSON-RPC "distill.apply_disposal"
operation (C14 — Phase 5 delete tier, stamped manifest only).

Purpose: the ONE act-time delete op in the distill-disposal tier. Given a
run_id, loads the SAME disposal-manifest C12 assembled and C13 stamped (never
a fresh manifest, never a caller-supplied bare path — same run_id-addressing
discipline as distill.stamp_disposal), verifies the stamp is complete and
sha-unstirred, verifies drain-ordering (the harvest commit already landed in
history), re-runs delete_guard per eligible row at execute time (TOCTOU
re-check — the stamp authorizes the PM's *decision*, this re-check verifies
the *state* still holds), partitions survivors into tracked/untracked via
`git ls-files`, deletes both (untracked first via plain `Path.unlink`, then
tracked via a scoped git-rm-then-commit that ALSO folds in the canonical-log
append), and emits a ceremony receipt. Lineage denormalization onto a
surviving predecessor (§ Lineage denormalization below) is GATED on the
child's own delete having actually succeeded — an untracked child's parent
write follows its confirmed `unlink`; a tracked child's parent write follows
its confirmed `git rm`, staged into the SAME commit as that rm and the
canonical-log append. A child whose own delete fails contributes NO
denormalization write; the pre-delete plan is never trusted as a stand-in
for the actual outcome (this is a fix, not the original design — see the
"gate on actual delete success" note below).

DEC-2 / DR-228 § D2a/D2b — this op is the disposal tier's only DELETING
member; distill.assemble_disposal_manifest (C12) and distill.stamp_disposal
(C13) never delete or commit.

Fail-closed refusal order (AC2; each is checked BEFORE any file is touched):
  1. manifest absent / schema-invalid                -> ApplyDisposalError
  2. stamp absent or partial (not stamp_complete)     -> ApplyDisposalError
  3. stamp sha != freshly-computed canonical-body sha -> ApplyDisposalError
     (sha-drift: the manifest body changed after the PM stamped it)
  4. mass_throttle flag set AND the stamped note does not acknowledge it
     (case-insensitive MASS_THROTTLE_ACK_MARKER substring)
                                                       -> ApplyDisposalError
  4b. eligible count exceeds MASS_THROTTLE_HARD_CAP -- unconditional, no ack
      can lift it (2026-07-23 E2 mass-throttle-hard-cap fix: gate 4's ack is a
      SOFT band; this is the ceiling above it)       -> ApplyDisposalError
  5. drain-ordering: harvest_committed_sha is not an ancestor of HEAD
     (`git merge-base --is-ancestor`, never a bare `git cat-file -e`, which
     would pass for a dangling/stashed/unmerged-branch object), OR that sha's
     commit does not touch the canonical log or the wiki tree (containment
     check, `git show --name-only`)                   -> ApplyDisposalError

Only after all five pass does this op look at individual rows. Per eligible
row (D2a-iv, act-time-terminality-re-verifying):
  - path absent on disk                -> skipped "already-deleted" (D2a-i,
    per-record idempotent replay — a blind retry of an already-applied
    manifest is a safe no-op: zero new deletions, zero duplicate log rows,
    because a row whose path is already gone never re-enters the delete/log
    batch below).
  - delete_guard re-evaluation (via distill_disposal_manifest's
    evaluate_candidate_receipts — the SAME guard-dispatch this module's
    sibling C12 op used to assemble the manifest, imported not
    reimplemented, DEC-5) now returns NOT eligible -> skipped
    "newly-blocked-at-apply-time: ..." (D2a-iv TOCTOU re-block; never
    deleted despite the manifest saying eligible=True at assemble time).
  - still eligible -> survivor; queued for delete.

Survivors are processed as a SET, not a sequence (D2a-ii, commutative): they
are sorted by path before any git/log operation runs, so an order-shuffled
input `candidates`/manifest-row list produces byte-identical output (same
deletions, same log rows, same commit pathspec) regardless of input order —
see the module's own order-shuffled test fixture.

Commit mechanics (DR-228 § D3 — "exactly one scoped commit over the union of
touched paths (canonical-log append + deleted tracked paths)"): this module
does NOT call `coordinator_core.ops.fleet._common.rm_and_commit` directly,
because that helper's own commit only ever covers the paths it `git rm`s — it
has no parameter for folding an ADDED/MODIFIED (not deleted) path (the
canonical log) into the SAME commit. `_delete_tracked_and_append_log` below
is a bespoke sibling that mirrors rm_and_commit's mechanics EXACTLY (private
`GIT_INDEX_FILE` isolated from the main index via `git read-tree HEAD`, plain
`git rm` — NEVER `git rm -f` — per tracked survivor, one `git commit`
over the exact pathspec, `git checkout HEAD -- <paths>` reversal on commit
failure, non-fatal-but-logged main-index resync) but additionally
`git add`s the canonical-log file (already re-written on disk via
`log_append.append_rows`'s own all-or-nothing validate-then-write) into the
SAME private index and the SAME commit pathspec. Tracked/untracked
classification reuses `coordinator_core.ops.fleet._findings_reap._is_tracked`
(the same `git ls-files --error-unmatch` tri-state helper DR-218's reap ops
already use) rather than re-deriving it.

Untracked survivors are `Path.unlink()`ed directly — no git history to
preserve, no commit call for that half (D3). Named residual (D5's
untracked-delete asymmetry, restated for this op): an untracked survivor is
unlinked BEFORE the tracked-rm/log-commit step so that, if the batch commit
subsequently fails, its log row is never written (the unlink already
happened but the commit that would have recorded it did not land) — this is
the same accepted asymmetry DR-228 § D5 names ("no git-revert claim is made
for [the untracked] half"); it is not a defect this op resolves.

MUTATING (DR-208 fail-closed: any file write disqualifies COMPUTE_ONLY;
five-question affirmation block cites DR-228 § D2a/D2b/D3/D4 — see
coordinator_core/authz/classification.py).

Negative-spec:
  - Does NOT assemble a manifest (C12's job) or write the PM stamp (C13's
    job) — this op only READS an already-stamped manifest at the well-known
    run_id path (`distill_stamp_disposal.manifest_path_for_run`, reused not
    re-derived).
  - Does NOT accept `disposal_authorized_*` as an operator param of any kind
    — the stamp is read-only input here; this op never writes it.
  - Does NOT invent a second stamp/override channel for the mass-throttle
    acknowledgment — the ONLY channel is the already-stamped `note` field
    (F2; C13's `note` param is the sole authoring surface).
  - Does NOT use `git rm -f`, `git add -A`, `git add .`, or a directory-
    prefix pathspec — exact-pathspec-only (D3).
  - Does NOT use blocking `subprocess.run` for any git call — every git
    subprocess is `asyncio.create_subprocess_exec` + await (D4).
  - Does NOT judge ripeness, reality-check verdicts, or write prose — this
    op's only judgment is mechanical (guard re-verify, drain-ordering,
    throttle-ack), per the plan's negative spec (AC12).

Spec backlink: pln-makima-driven-ceremony-redesig-c7fe9a § C14
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D2a, D2b, D3, D4
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coordinator_core.dag import _parse_inline_list, _read_meta, handoff_edges, resolve_target
from coordinator_core.distill import log_append as _log_append
from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.frontmatter.primitives import (
    FrontmatterSplit,
    read_fm_field,
    rebuild,
    remove_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
)
from coordinator_core.ops.distill_disposal_manifest import (
    MASS_THROTTLE_HARD_CAP,
    evaluate_candidate_receipts,
)
from coordinator_core.ops.distill_stamp_disposal import (
    DisposalStampError,
    load_disposal_manifest,
    manifest_path_for_run,
)
from coordinator_core.ops.fleet._common import (
    _make_git_env,
    _update_index_with_retry,
    main_worktree_root,
)
from coordinator_core.ops.fleet._findings_reap import _is_tracked, _is_tracked_batch
from coordinator_core.ops.fleet.migrate_handoff_vocabulary import (
    _HEIR_EDGE_KINDS,
    _insert_raw_line_after,
    _successor_ref,
)
from coordinator_core.ipc import register_op
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.wire_paths import rel_id

__all__ = [
    "MASS_THROTTLE_ACK_MARKER",
    "MASS_THROTTLE_HARD_CAP",
    "CANONICAL_LOG_RELPATH",
    "WIKI_TREE_PREFIX",
    "ApplyDisposalError",
    "ApplyDisposalResult",
    "verify_stamp_and_throttle",
    "verify_drain_ordering",
    "compute_apply_plan",
    "apply_disposal_manifest",
    "write_apply_receipt",
]

# ---------------------------------------------------------------------------
# Lineage denormalization (2026-07-23 opticon lineage-severing fix)
#
# `delete_guard.check_active_reference` blocks a delete when the candidate is
# still referenced FROM elsewhere — sound for a doc other docs point at, but
# handoff succession edges point the OTHER way: `predecessor:` /
# `additional_predecessors:` / `origin_handoff:` live on the SUCCESSOR, naming
# the parent, and there is no reverse `successor:` field anywhere in the
# corpus. A successor handoff therefore has ZERO inbound references BY
# CONSTRUCTION and always clears the active-reference guard — deleting it
# silently severs the only on-disk record of the lineage and orphans the
# surviving parent. Measured blast radius (project-opticon, two runs): 45
# archived handoffs deleted, 3 confirmed sole lineage carriers.
#
# The fix is NOT to widen the guard (that would make the handoff cohort
# effectively undisposable — defeats distill's purpose). Instead: if a
# handoff candidate carries a succession edge naming a parent that SURVIVES
# this disposal batch, a forward edge is denormalized onto that surviving
# parent — the lineage record then lives on the record that persists, not
# the one about to be deleted. Planned (pure, no writes) in
# `_plan_denormalizations` from `compute_apply_plan`; written to disk in
# `_write_denormalizations`, called from `_delete_tracked_and_append_log`
# ONLY for entries whose own child has ALREADY been confirmed deleted (its
# `unlink` for an untracked child, or its `git rm` for a tracked one, staged
# into the same commit) — never from the pre-delete plan alone. A child
# whose delete fails contributes no denormalization write.
#
# The forward edge is `disposed_successors:` — a NEW, MULTI-VALUED field, NOT
# DR-084's `continued_into`. `continued_into` is single-valued and is the
# schema-conditional companion of `deployment_state: continued`
# (`frontmatter/schemas/handoff.schema.json`/`handoff-archived.schema.json`:
# `if deployment_state == "continued" then required: [continued_into]`,
# written atomically by every existing writer —
# `ops/handoff_archive_transition.py`, `ops/fleet/migrate_handoff_vocabulary.py`).
# This op has no authority to transition a surviving parent's lifecycle, and a
# single slot cannot express N disposed children of one parent without either
# losing the Nth-1 entries or inventing a conflict policy that is itself a
# smaller instance of the exact lineage-severing defect this fix exists to
# close. `disposed_successors` sidesteps both: it is purely additive (a list;
# multiple disposed children of one parent each get their own entry, no
# conflict is possible), and it carries no lifecycle implication for the
# parent's own `deployment_state`.
#
# Edge-kind resolution reuses `coordinator_core.ops.fleet
# .migrate_handoff_vocabulary`'s `_HEIR_EDGE_KINDS` and `_successor_ref`
# (rather than re-deriving them) and `_insert_raw_line_after` for the
# provenance comment. The target named in `disposed_successors` is a file
# this op is ABOUT TO DELETE, so a bare path/id would read as a resolvable
# reference when it will not be — every added entry carries a one-line
# `# distill: ...` provenance comment naming the disposing run and date, so
# the parent stops reading as a dead end without implying the target is
# still on disk.
# ---------------------------------------------------------------------------

_LOG = logging.getLogger(__name__)

#: Case-insensitive substring the stamp's `disposal_authorized_note` must
#: carry to acknowledge a manifest-level mass-throttle flag (F2) — the ONLY
#: acknowledgment channel; there is no second stamp or override param.
MASS_THROTTLE_ACK_MARKER: str = "mass-throttle-ack"

#: The canonical-log target (forward-slash, worktree-root-relative — Windows
#: needle discipline) drain-ordering's containment check looks for in the
#: harvest commit's touched-paths set.
CANONICAL_LOG_RELPATH: str = "state/distillation-log.md"

# Generator-provenance: deletes/rewrites a data-dependent set of tracked
# handoff files (whichever candidates the disposal manifest names), appends
# rows to state/distillation-log.md, and rewrites surviving parents'
# disposed_successors frontmatter under state/handoffs/.
MUTATES = ["state/distillation-log.md", "state/handoffs/**/*.md", "docs/wiki/**/*.md"]

#: Wiki-tree path prefix (forward-slash) — the OTHER acceptable containment
#: target for drain-ordering (a harvest commit may touch wiki guides instead
#: of, or in addition to, the canonical log).
WIKI_TREE_PREFIX: str = "docs/wiki/"


class ApplyDisposalError(Exception):
    """Raised on any fail-loud refusal condition (unstamped, sha-drifted,
    throttle-unacknowledged, ordering-violation) before any file is touched.
    Callers (the op handler) translate this to a JSON-RPC error."""


def _subprocess_kwargs() -> dict[str, Any]:
    """Extra kwargs for every asyncio.create_subprocess_exec call in this
    module — CREATE_NO_WINDOW on win32 (Windows first-class: a console-
    spawning git subprocess on every apply_disposal call is a visible,
    avoidable regression), no-op on POSIX (creationflags is a Windows-only
    subprocess kwarg; passing it on POSIX raises). Routed through the
    canonical coordinator_core.win_portability.no_console_creationflags()
    primitive rather than a hardcoded flag literal."""
    return no_console_creationflags()


async def _run_git(
    *args: str, cwd: Path, env: dict[str, str]
) -> tuple[int, bytes, bytes]:
    """Awaited git subprocess call (D4 — never blocking subprocess.run).
    Returns (returncode, stdout, stderr) as raw bytes."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_subprocess_kwargs(),
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


# ---------------------------------------------------------------------------
# Refusal gates — checked BEFORE any file is touched (AC2)
# ---------------------------------------------------------------------------


def verify_stamp_and_throttle(manifest: dict[str, Any]) -> None:
    """Gates 2-4b: stamp must be complete, sha must match the CURRENT
    canonical body, and — if mass_throttle is set — the stamped note must
    carry MASS_THROTTLE_ACK_MARKER (case-insensitive). Above that soft band,
    gate 4b is an UNCONDITIONAL ceiling: a run whose eligible count exceeds
    MASS_THROTTLE_HARD_CAP is refused regardless of the ack, because a single
    stamp note is not sufficient authorization for an arbitrarily large
    batch — the only way past gate 4b is to split the run into multiple
    smaller ones, each individually re-assembled and re-stamped. Raises
    ApplyDisposalError on any failure; returns None (no side effect) on
    success."""
    if not _schema.stamp_complete(manifest):
        raise ApplyDisposalError(
            "distill.apply_disposal refuses an unstamped (or partially "
            "stamped) manifest — run distill.stamp_disposal first "
            "(DR-228 § D2b(vi))."
        )

    computed_sha = _schema.compute_manifest_sha(manifest)
    stamped_sha = manifest["disposal_authorized_sha"]
    if stamped_sha != computed_sha:
        raise ApplyDisposalError(
            "distill.apply_disposal refuses: manifest body sha has DRIFTED "
            f"since it was stamped (stamped sha={stamped_sha!r}, current "
            f"computed sha={computed_sha!r}) — re-assemble and get a fresh "
            "PM stamp; never apply over drifted content."
        )

    if manifest.get("mass_throttle"):
        note = manifest.get("disposal_authorized_note") or ""
        if MASS_THROTTLE_ACK_MARKER not in note.lower():
            raise ApplyDisposalError(
                "distill.apply_disposal refuses: manifest-level mass_throttle "
                "flag is set but the stamped disposal_authorized_note does "
                f"not acknowledge it (expected the substring "
                f"{MASS_THROTTLE_ACK_MARKER!r}, case-insensitive) — F2: the "
                "PM stamp alone is not sufficient defense-in-depth for a "
                "mass-eligible manifest; re-stamp with an acknowledging note "
                "to proceed."
            )

    # Gate 4b (hard cap): unconditional, checked regardless of mass_throttle
    # or ack state above — a stamp note can authorize the SOFT band (ratio/
    # absolute -> hard cap) but cannot lift the hard cap itself. This is
    # deliberately a separate check from the mass_throttle/ack block above,
    # not nested inside it, so the ceiling holds even in a hypothetical future
    # where the soft-band flag computation changes shape.
    eligible_count = manifest.get("scan_stats", {}).get("eligible_count", 0)
    if eligible_count > MASS_THROTTLE_HARD_CAP:
        raise ApplyDisposalError(
            "distill.apply_disposal refuses: eligible-delete count "
            f"{eligible_count} exceeds MASS_THROTTLE_HARD_CAP="
            f"{MASS_THROTTLE_HARD_CAP} — no stamped ack can authorize a "
            "single run above this ceiling; split the candidates into "
            "multiple runs, each re-assembled and re-stamped, and apply "
            "them separately."
        )


async def verify_drain_ordering(
    worktree_root: Path, harvest_committed_sha: str
) -> None:
    """Gate 5: harvest_committed_sha must (a) be an ancestor of HEAD via
    `git merge-base --is-ancestor` (never a bare `git cat-file -e`, which
    passes for a dangling/stashed/unmerged-branch object — F4) AND (b) its
    own commit must touch the canonical log or the wiki tree (containment —
    a caller cannot satisfy the gate with an arbitrary unrelated ancestor
    sha). Raises ApplyDisposalError on either failure."""
    env = _make_git_env()

    rc, _out, _err = await _run_git(
        "merge-base", "--is-ancestor", harvest_committed_sha, "HEAD",
        cwd=worktree_root, env=env,
    )
    if rc != 0:
        raise ApplyDisposalError(
            f"distill.apply_disposal refuses: harvest_committed_sha "
            f"{harvest_committed_sha!r} is not an ancestor of HEAD (drain-"
            "ordering violation, or the sha does not resolve to a commit at "
            "all) — delete must NEVER precede the harvest commit landing in "
            "history (DR-228 § D2b(vii))."
        )

    rc, out, _err = await _run_git(
        "show", "--name-only", "--pretty=format:", harvest_committed_sha,
        cwd=worktree_root, env=env,
    )
    if rc != 0:
        raise ApplyDisposalError(
            f"distill.apply_disposal refuses: could not enumerate touched "
            f"paths for harvest_committed_sha {harvest_committed_sha!r} "
            "(git show failed) — containment cannot be verified."
        )
    touched = {line.strip() for line in out.decode("utf-8", errors="replace").splitlines() if line.strip()}
    contains_log = CANONICAL_LOG_RELPATH in touched
    contains_wiki = any(p.startswith(WIKI_TREE_PREFIX) for p in touched)
    if not (contains_log or contains_wiki):
        raise ApplyDisposalError(
            f"distill.apply_disposal refuses: harvest_committed_sha "
            f"{harvest_committed_sha!r} is a valid ancestor of HEAD but its "
            f"commit touches neither {CANONICAL_LOG_RELPATH!r} nor "
            f"{WIKI_TREE_PREFIX!r} — a caller cannot satisfy the "
            "drain-ordering gate with an arbitrary ancestor unrelated to the "
            "harvest (DR-228 § D2b(vii))."
        )


# ---------------------------------------------------------------------------
# Per-row TOCTOU re-verify + commutative ordering (D2a-i, D2a-ii, D2a-iv)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyPlan:
    """Result of compute_apply_plan: the rows still eligible to delete at
    apply time (sorted by path — commutativity), plus the skip lists (each
    entry a {"path", "reason"} dict) for rows that will NOT be deleted this
    run.

    denorm_writes / denorm_skipped: the lineage-denormalization plan (see
    module docstring § Lineage denormalization) — which surviving parents get
    a `disposed_successors` entry appended before their child is deleted, and
    which candidate succession edges produced no write and why. Both are pure
    plan output; nothing is written to disk until `apply_disposal_manifest`
    calls `_write_denormalizations`. Multiple entries MAY share the same
    `parent` (one per disposed child) — `disposed_successors` is
    multi-valued, so a parent with N disposed children in one batch gets N
    entries, never just the first."""

    survivors: list[dict[str, Any]]
    already_deleted: list[dict[str, str]] = field(default_factory=list)
    newly_blocked: list[dict[str, str]] = field(default_factory=list)
    denorm_writes: list[dict[str, str]] = field(default_factory=list)
    denorm_skipped: list[dict[str, str]] = field(default_factory=list)


def _existing_disposed_successors(parent_split: FrontmatterSplit) -> list[str] | None:
    """Return the parent's current `disposed_successors:` list, parsed via
    `dag._parse_inline_list` (the same quote-aware inline-list parser
    `dag.handoff_edges` relies on for `additional_predecessors`) — never a
    hand-rolled split. Absent field -> []. A block-list (`- item` per line) or
    any other non-inline shape -> None, a distinct sentinel from "empty",
    signalling "not a supported on-disk shape for this writer to append to"
    (this op only ever WRITES the inline form; a differently-shaped existing
    value is left alone rather than risk corrupting it)."""
    raw = read_fm_field(parent_split.fm_text, "disposed_successors")
    if raw is None:
        return []
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        return [str(v) for v in _parse_inline_list(raw)]
    return None


def _plan_denormalizations(
    worktree_root: Path, run_id: str, survivors: list[dict[str, Any]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Pure (read-only) lineage-denormalization plan — see module docstring §
    Lineage denormalization for the defect this closes.

    For every HANDOFF survivor (artifact_class == "handoff") carrying a
    succession edge (`_HEIR_EDGE_KINDS` — predecessor / additional_predecessors[]
    / origin_handoff, imported from migrate_handoff_vocabulary.py, NOT
    re-derived) naming a parent that will SURVIVE this disposal batch, plans a
    `disposed_successors` list ENTRY (one per disposed child, never a
    single-slot overwrite) onto that parent.

    Returns (writes, skipped):
      writes   — one entry per (parent, child) addition — MULTIPLE entries
                 MAY share the same `parent` (a parent with N disposed
                 children in this batch gets N entries):
                 [{"parent": <repo-rel>, "child": <repo-rel>,
                   "successor_ref": <value>, "provenance_comment": <# line>}].
      skipped  — every succession edge that did NOT produce a write, with a
                 reason: the parent is itself in this disposal batch, the
                 parent is disk-absent (including git-history-only
                 resolution — there is no disk survivor to write onto), the
                 edge itself is unresolvable, the parent's own frontmatter is
                 unreadable/unparseable, or the parent's existing
                 `disposed_successors` value is on disk in a shape this
                 writer does not append to (a block list, not the inline
                 form this op always writes — see `_existing_disposed_successors`).

    There is deliberately NO "conflicting value" branch: `disposed_successors`
    is multi-valued, so two survivors resolving to the same parent are never
    in contention — both get their own entry. The only per-entry idempotency
    check is MEMBERSHIP: an entry whose `successor_ref` is already present in
    the parent's existing (or this-same-run's newly-planned) list is a
    SILENT no-op — no write entry, no skip entry — so re-running this plan
    over an already-denormalized corpus produces zero writes for that entry.

    Never mutates anything: every read here is a plain file read, matching
    `compute_apply_plan`'s own "mutates nothing" contract — this function is
    called FROM compute_apply_plan for exactly that reason, preserving the
    dry-run-stays-write-free invariant `apply_disposal_manifest`'s callers
    rely on.
    """
    survivor_rel_paths = {row["path"] for row in survivors}
    today = datetime.now(timezone.utc).date().isoformat()

    writes: list[dict[str, str]] = []
    planned_by_parent: dict[str, set[str]] = {}
    skipped: list[dict[str, str]] = []

    for row in survivors:
        if row.get("artifact_class") != "handoff":
            continue

        child_abs = worktree_root / row["path"]
        meta = _read_meta(str(child_abs))
        raw_edges = handoff_edges(meta, _HEIR_EDGE_KINDS)
        if not raw_edges:
            continue

        successor_ref = _successor_ref(child_abs, worktree_root)
        comment = (
            f"# distill: {row['path']} disposed by /distill on {today} "
            f"(run {run_id}) — survives only in git history from here on"
        )
        child_handoff_dir = str(child_abs.parent)

        for raw_ref in raw_edges:
            resolved = resolve_target(raw_ref, child_handoff_dir, str(worktree_root))
            if resolved is None:
                skipped.append({
                    "parent": "", "child": row["path"],
                    "reason": f"edge-unresolvable: {raw_ref!r}",
                })
                continue
            if resolved == "git-history":
                skipped.append({
                    "parent": raw_ref, "child": row["path"],
                    "reason": "parent-absent (git-history only — no disk survivor to write onto)",
                })
                continue

            parent_abs = Path(resolved)
            parent_rel = rel_id(parent_abs, worktree_root)

            if parent_rel in survivor_rel_paths:
                skipped.append({
                    "parent": parent_rel, "child": row["path"],
                    "reason": "parent-also-in-disposal-batch",
                })
                continue
            if not parent_abs.exists():
                skipped.append({
                    "parent": parent_rel, "child": row["path"],
                    "reason": "parent-absent",
                })
                continue

            try:
                parent_text = parent_abs.read_text(encoding="utf-8")
            except OSError as exc:
                skipped.append({
                    "parent": parent_rel, "child": row["path"],
                    "reason": f"parent-read-failed: {exc}",
                })
                continue
            parent_split = split_frontmatter(parent_text)
            if parent_split is None:
                skipped.append({
                    "parent": parent_rel, "child": row["path"],
                    "reason": "parent-no-frontmatter",
                })
                continue

            existing = _existing_disposed_successors(parent_split)
            if existing is None:
                skipped.append({
                    "parent": parent_rel, "child": row["path"],
                    "reason": "parent-disposed-successors-not-inline-list — not auto-appended",
                })
                continue

            already_present = set(existing) | planned_by_parent.get(parent_rel, set())
            if successor_ref in already_present:
                continue  # Idempotent — already recorded (on disk or planned this run).

            entry = {
                "parent": parent_rel,
                "child": row["path"],
                "successor_ref": successor_ref,
                "provenance_comment": comment,
            }
            planned_by_parent.setdefault(parent_rel, set()).add(successor_ref)
            writes.append(entry)

    return writes, skipped


def compute_apply_plan(worktree_root: Path, manifest: dict[str, Any]) -> ApplyPlan:
    """Partition manifest["rows"] into (survivors, already_deleted,
    newly_blocked), and plan lineage denormalizations for the survivors (see
    `_plan_denormalizations`) — pure except for the on-disk existence check,
    the delete_guard re-evaluation, and the denormalization plan's own reads,
    none of which mutate anything.

    A row with eligible=False (retained at assemble time) is silently
    excluded — it was never a delete candidate and stays that way.

    survivors is SORTED BY PATH (D2a-ii, commutativity) — the caller must
    never rely on manifest["rows"] input order for the delete/log batch (and
    the denormalization plan itself depends on this ordering for its own
    deterministic same-parent tie-break — see `_plan_denormalizations`).
    """
    survivors: list[dict[str, Any]] = []
    already_deleted: list[dict[str, str]] = []
    newly_blocked: list[dict[str, str]] = []

    for row in manifest["rows"]:
        if not row.get("eligible"):
            continue

        abs_path = worktree_root / row["path"]
        if not abs_path.exists():
            # D2a-i — per-record idempotent replay: a blind retry finds the
            # path already gone and skips it, never re-deleting or erroring.
            already_deleted.append({"path": row["path"], "reason": "already-deleted"})
            continue

        receipt = evaluate_candidate_receipts(abs_path, worktree_root)
        if not receipt["eligible"]:
            # D2a-iv — act-time-terminality-re-verifying (TOCTOU re-block):
            # the manifest said eligible=True at assemble time, but re-running
            # the SAME guard set now blocks it. Never deleted.
            newly_blocked.append({
                "path": row["path"],
                "reason": "newly-blocked-at-apply-time: blocked by "
                + ", ".join(receipt["blocked_by"]),
            })
            continue

        survivors.append(row)

    survivors.sort(key=lambda r: r["path"])
    denorm_writes, denorm_skipped = _plan_denormalizations(
        worktree_root, manifest["run_id"], survivors
    )
    return ApplyPlan(
        survivors=survivors,
        already_deleted=already_deleted,
        newly_blocked=newly_blocked,
        denorm_writes=denorm_writes,
        denorm_skipped=denorm_skipped,
    )


# ---------------------------------------------------------------------------
# Lineage-denormalization writer — turns a (caller-gated) subset of
# _plan_denormalizations' pure plan into on-disk mutations. Called from
# _delete_tracked_and_append_log AFTER that function's own tracked git-rm
# loop, over an entry set already filtered to children whose own delete
# actually succeeded (module docstring § Lineage denormalization) — never
# over the raw pre-delete plan. Partitions writes into tracked (staged into
# the SAME commit as the tracked delete + log-append) vs untracked (written
# directly, no commit — mirrors the untracked-survivor delete path, which
# also has nothing to commit).
# ---------------------------------------------------------------------------


def _serialize_disposed_successors_line(items: list[str]) -> str:
    """Render `disposed_successors:` as the ONE on-disk shape this writer ever
    produces — an inline YAML list, each item quoted per
    `serialize_yaml_scalar`'s structural-character rules. Never a block list
    (`- item` per line) — `_existing_disposed_successors` only appends to
    this exact shape and fails closed (skips, never corrupts) on any other."""
    quoted = ", ".join(serialize_yaml_scalar(v) for v in items)
    return f"disposed_successors: [{quoted}]"


def _set_disposed_successors(fm_text: str, items: list[str]) -> str:
    """Replace (or insert) the `disposed_successors:` line with the full,
    already-merged `items` list. Removes any existing line first —
    `remove_fm_field`'s block-scalar guard is a no-op here since this field is
    always single-line inline (see `_serialize_disposed_successors_line`) —
    then inserts the fresh line anchored after `deployment_state:`, mirroring
    where `migrate_handoff_vocabulary.py` anchors `continued_into`."""
    if read_fm_field(fm_text, "disposed_successors") is not None:
        fm_text = remove_fm_field(fm_text, "disposed_successors")
    return _insert_raw_line_after(
        fm_text, "deployment_state", _serialize_disposed_successors_line(items)
    )


async def _write_denormalizations(
    worktree_root: Path, denorm_writes: list[dict[str, str]]
) -> tuple[list[Path], list[Path], list[dict[str, str]], list[dict[str, str]]]:
    """Apply every ALREADY-GATED `disposed_successors` addition to disk, ONE
    combined rewrite per parent (`denorm_writes` entries are grouped by
    `parent` first — a parent with N disposed children in this batch has N
    entries and gets exactly one on-disk rewrite covering all of them). The
    caller (`_delete_tracked_and_append_log`) is responsible for filtering
    `denorm_writes` to children whose own delete actually succeeded BEFORE
    calling this function — this function itself does not know or care
    whether a child was deleted; it only writes what it is handed.

    Returns (tracked_paths, untracked_paths, applied_entries, failed):
      tracked_paths   — parent files written AND git-tracked; the caller must
                         `git add` these into the tracked-delete/log commit
                         (see `_delete_tracked_and_append_log`) — a write
                         here is NOT yet durable until that commit lands.
      untracked_paths — parent files written directly; nothing further to do,
                         same asymmetry DR-228 § D5 already accepts for
                         untracked survivor deletes (no git history to
                         preserve, no commit to fold into).
      applied_entries — the subset of `denorm_writes` actually written this
                         call (membership-idempotent entries already present
                         on disk are excluded) — the caller uses this, not
                         `denorm_writes` itself, to report what landed,
                         because it is possible for SOME of a parent's
                         entries to already be present while others are new.
      failed          — {"path", "reason"} dicts for any parent whose planned
                         addition(s) could not be applied — the read failed,
                         the parent lost its frontmatter block since plan
                         time, its `disposed_successors` is on disk in a
                         shape this writer does not append to (re-verified
                         here, fail-closed, never corrupted), or its
                         tracked-status could not be determined. `path` here
                         is the PARENT's path (failures are per-parent, not
                         per-entry, since the rewrite is one shared write).

    Each parent's existing list is re-read and re-verified immediately before
    mutation (narrow TOCTOU re-check, mirrors compute_apply_plan's own
    re-verify-at-apply-time discipline for delete_guard) — membership only,
    never equality/conflict, since a list has no single-slot contention.
    """
    tracked: list[Path] = []
    untracked: list[Path] = []
    applied_entries: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    by_parent: dict[str, list[dict[str, str]]] = {}
    for entry in denorm_writes:
        by_parent.setdefault(entry["parent"], []).append(entry)

    # Pass 1 — read, parse, and compute each parent's to_add set. No git
    # spawn here; the tracked-status probe is deferred to a single batched
    # call below (amplification hitlist, 2026-08-19) instead of one spawn
    # per parent.
    prepared: list[dict[str, Any]] = []
    for parent_rel, entries in by_parent.items():
        parent_abs = worktree_root / parent_rel
        try:
            original = parent_abs.read_text(encoding="utf-8")
        except OSError as exc:
            failed.append({"path": parent_rel, "reason": f"denorm-read-failed: {exc}"})
            continue

        split = split_frontmatter(original)
        if split is None:
            failed.append({
                "path": parent_rel,
                "reason": "denorm-no-frontmatter-at-apply-time",
            })
            continue

        existing = _existing_disposed_successors(split)
        if existing is None:
            failed.append({
                "path": parent_rel,
                "reason": "denorm-disposed-successors-not-inline-list-at-apply-time",
            })
            continue

        to_add = [e for e in entries if e["successor_ref"] not in existing]
        if not to_add:
            continue  # Idempotent — every entry already present on disk.

        prepared.append({
            "parent_rel": parent_rel,
            "parent_abs": parent_abs,
            "split": split,
            "existing": existing,
            "to_add": to_add,
        })

    status_map = await _is_tracked_batch(
        worktree_root, [item["parent_abs"] for item in prepared]
    )

    # Pass 2 — write. `status` here only routes the write into the
    # tracked/untracked return list for the caller's commit; it is NOT a
    # delete guard, so hoisting the probe out of the per-parent loop does
    # not widen any destructive-action TOCTOU window (unlike the
    # recheck-immediately-before-unlink sites this chunk leaves alone).
    for item in prepared:
        parent_rel = item["parent_rel"]
        parent_abs = item["parent_abs"]
        split = item["split"]
        existing = item["existing"]
        to_add = item["to_add"]

        status = status_map[parent_abs]
        if status not in ("tracked", "untracked"):
            failed.append({
                "path": parent_rel,
                "reason": "denorm-tracked-status-indeterminate: git ls-files rc!=0/1",
            })
            continue

        new_list = existing + [e["successor_ref"] for e in to_add]
        fm_text = _set_disposed_successors(split.fm_text, new_list)
        # Reversed so the FIRST addition's comment ends up visually first,
        # immediately under the field — each insert anchors right after the
        # disposed_successors line, so inserting last-to-first yields
        # forward reading order (a purely cosmetic concern, not correctness).
        for e in reversed(to_add):
            fm_text = _insert_raw_line_after(
                fm_text, "disposed_successors", e["provenance_comment"]
            )
        rebuilt = rebuild(split, fm_text)

        try:
            parent_abs.write_text(rebuilt, encoding="utf-8", newline="\n")
        except OSError as exc:
            failed.append({"path": parent_rel, "reason": f"denorm-write-failed: {exc}"})
            continue

        applied_entries.extend(to_add)
        if status == "tracked":
            tracked.append(parent_abs)
        else:
            untracked.append(parent_abs)

    return tracked, untracked, applied_entries, failed


# ---------------------------------------------------------------------------
# Delete + log-append + ONE commit (D3) — bespoke sibling of
# fleet._common.rm_and_commit (see module docstring for why this is not a
# direct reuse of that helper).
# ---------------------------------------------------------------------------


async def _delete_tracked_and_append_log(
    worktree_root: Path,
    tracked_paths: list[Path],
    log_path: Path,
    log_rows: list[dict[str, str]],
    subject: str,
    denorm_writes: list[dict[str, str]] | None = None,
    confirmed_untracked_child_paths: set[str] | None = None,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """git-rm every tracked_paths entry FIRST, THEN gate `denorm_writes` on
    the ACTUAL per-child outcome of that rm loop (plus the caller-confirmed
    untracked-child outcomes in `confirmed_untracked_child_paths`) before any
    parent file is touched — the fix for the defect named in the module
    docstring § Lineage denormalization: a denorm entry whose own child never
    actually got deleted must never be written, regardless of what the
    pre-delete plan said. Only the surviving, gated entries are handed to
    `_write_denormalizations`; any resulting tracked-parent path is then
    git-added into the SAME commit as the tracked deletes and the log
    append. Returns (committed_child_ids, denorm_written, denorm_write_failed,
    failed):
      committed_child_ids — rel-posix ids of tracked_paths entries that made
                             it into the commit (empty if the commit failed).
      denorm_written       — {"parent", "child", "successor_ref",
                             "provenance_comment"} entries that actually
                             landed (untracked-parent entries as soon as
                             written; tracked-parent entries only once the
                             commit below succeeds).
      denorm_write_failed  — {"path", "reason"} dicts for a parent whose
                             gated write could not be applied to disk (see
                             `_write_denormalizations`'s own `failed`).
      failed               — {"path", "reason"} dicts for any tracked_paths
                             or staged tracked-parent-denorm entry that could
                             not be rm'd, staged, or committed.

    A tracked-parent denorm write that fails to stage (`git add`) is reverted
    via `git checkout HEAD --` immediately (its on-disk mutation is not yet
    committed, so HEAD's version IS the pre-write content) — this does not
    abort the batch; the reaped child deletes and any other denorm adds still
    proceed to the commit.

    Empty tracked_paths AND empty log_rows AND empty denorm_writes -> returns
    ([], [], [], []), no git call at all (a clean no-op — D2a-i
    idempotent-replay convergence).
    """
    denorm_writes = denorm_writes or []
    confirmed_untracked_child_paths = confirmed_untracked_child_paths or set()
    if not tracked_paths and not log_rows and not denorm_writes:
        return [], [], [], []

    idx_fd, idx_path = tempfile.mkstemp(prefix="distill-apply-idx-")
    os.close(idx_fd)

    try:
        base_env = _make_git_env(idx_path=idx_path)

        rc, _out, err = await _run_git("read-tree", "HEAD", cwd=worktree_root, env=base_env)
        if rc != 0:
            err_msg = err.decode(errors="replace").strip()
            _LOG.error("distill.apply_disposal: git read-tree HEAD failed: %s", err_msg)
            return [], [], [], [
                {"path": rel_id(p, worktree_root), "reason": f"index-init-failed: {err_msg}"}
                for p in tracked_paths
            ]

        reaped: list[Path] = []
        failed: list[dict[str, str]] = []

        for path in tracked_paths:
            rc, _out, err = await _run_git(
                "rm", "--", str(path), cwd=worktree_root, env=base_env
            )
            if rc != 0:
                failed.append({
                    "path": rel_id(path, worktree_root),
                    "reason": err.decode(errors="replace").strip() or "git-rm-failed",
                })
            else:
                reaped.append(path)

        # Gate the denorm write set on ACTUAL per-child delete outcomes, not
        # the pre-delete plan (module docstring § Lineage denormalization —
        # the defect this restructure closes). A child qualifies iff its own
        # tracked git rm just succeeded above, or it is an untracked child
        # the caller already confirmed deleted before this function was even
        # invoked. A child whose delete failed contributes NO entry — its
        # parent must never claim a still-present file "survives only in git
        # history."
        reaped_child_ids = {rel_id(p, worktree_root) for p in reaped}
        eligible_child_ids = reaped_child_ids | confirmed_untracked_child_paths
        gated_writes = [e for e in denorm_writes if e["child"] in eligible_child_ids]

        denorm_tracked_paths, denorm_untracked_paths, denorm_applied, denorm_write_failed = (
            await _write_denormalizations(worktree_root, gated_writes)
        )
        denorm_applied_by_parent: dict[str, list[dict[str, str]]] = {}
        for e in denorm_applied:
            denorm_applied_by_parent.setdefault(e["parent"], []).append(e)

        # Untracked-parent denorm writes have no commit to wait on — they
        # already landed on disk (same asymmetry DR-228 § D5 accepts for
        # untracked survivor deletes).
        denorm_written: list[dict[str, str]] = [
            e
            for p in denorm_untracked_paths
            for e in denorm_applied_by_parent.get(rel_id(p, worktree_root), [])
        ]

        staged_denorm: list[Path] = []
        for path in denorm_tracked_paths:
            rc, _out, err = await _run_git(
                "add", "--", str(path), cwd=worktree_root, env=base_env
            )
            if rc != 0:
                err_msg = err.decode(errors="replace").strip() or "git-add-denorm-failed"
                # Not yet committed — HEAD's version IS the pre-write content.
                revert_rc, _revert_out, revert_err = await _run_git(
                    "checkout", "HEAD", "--", str(path), cwd=worktree_root, env=_make_git_env()
                )
                reason = err_msg
                if revert_rc != 0:
                    # The revert itself failed — the ORIGINAL git-add failure
                    # remains the primary cause; this is appended context so
                    # the receipt names the path left in an inconsistent
                    # on-disk state, never silently swallowed.
                    revert_err_msg = (
                        revert_err.decode(errors="replace").strip() or "git-checkout-revert-failed"
                    )
                    _LOG.error(
                        "distill.apply_disposal: revert checkout failed for %s after "
                        "git-add-denorm failure: %s",
                        path, revert_err_msg,
                    )
                    reason = f"{err_msg} (revert-also-failed: {revert_err_msg})"
                failed.append({"path": rel_id(path, worktree_root), "reason": reason})
            else:
                staged_denorm.append(path)

        log_existed_before = log_path.exists()
        pre_append_content: bytes | None = (
            log_path.read_bytes() if log_existed_before else None
        )

        log_touched = False
        if log_rows:
            _log_append.append_rows(log_path, log_rows)
            log_touched = True
            rc, _out, err = await _run_git(
                "add", "--", str(log_path), cwd=worktree_root, env=base_env
            )
            if rc != 0:
                err_msg = err.decode(errors="replace").strip()
                _LOG.error("distill.apply_disposal: git add log_path failed: %s", err_msg)
                # Reverse everything reaped/staged so far (nothing committed yet).
                revert_failed_ids: set[str] = set()
                for p in reaped + staged_denorm:
                    revert_rc, _revert_out, revert_err = await _run_git(
                        "checkout", "HEAD", "--", str(p), cwd=worktree_root, env=_make_git_env()
                    )
                    if revert_rc != 0:
                        p_id = rel_id(p, worktree_root)
                        revert_failed_ids.add(p_id)
                        _LOG.error(
                            "distill.apply_disposal: revert checkout failed for %s after "
                            "log-stage failure: %s",
                            p, revert_err.decode(errors="replace").strip(),
                        )
                # Reverse log_path's own on-disk mutation too — append_rows
                # wrote it directly to the working tree (not through the
                # isolated temp index), so a failed `git add` leaves it
                # dangling with this run's rows and no landed delete unless
                # it is restored here explicitly (do not assume log_path is
                # already tracked — restore from captured pre-append state
                # either way).
                if log_existed_before:
                    log_path.write_bytes(pre_append_content)
                else:
                    try:
                        log_path.unlink()
                    except OSError:
                        pass
                # denorm_written here is untracked-parent-only (no git
                # operation backs those writes, so nothing above reverted
                # them) — they remain genuinely landed even though this
                # commit round aborted.
                return [], denorm_written, denorm_write_failed, [
                    {
                        "path": rel_id(p, worktree_root),
                        "reason": (
                            f"log-stage-failed: {err_msg}"
                            + (
                                " (revert-also-failed: working tree left inconsistent)"
                                if rel_id(p, worktree_root) in revert_failed_ids
                                else ""
                            )
                        ),
                    }
                    for p in reaped + staged_denorm
                ] + failed

        commit_paths = [str(p) for p in reaped] + [str(p) for p in staged_denorm]
        if log_touched:
            commit_paths.append(str(log_path))

        if not commit_paths:
            return [], denorm_written, denorm_write_failed, failed

        rc, _out, err = await _run_git(
            "-c", "commit.gpgsign=false", "commit", "-m", subject, "--", *commit_paths,
            cwd=worktree_root, env=base_env,
        )
        if rc != 0:
            err_msg = err.decode(errors="replace").strip()
            _LOG.error("distill.apply_disposal: git commit failed: %s", err_msg)
            main_env_restore = _make_git_env()
            revert_failed_paths: set[str] = set()
            for p_str in commit_paths:
                revert_rc, _revert_out, revert_err = await _run_git(
                    "checkout", "HEAD", "--", p_str, cwd=worktree_root, env=main_env_restore
                )
                if revert_rc != 0:
                    revert_failed_paths.add(p_str)
                    _LOG.error(
                        "distill.apply_disposal: revert checkout failed for %s after "
                        "commit failure: %s",
                        p_str, revert_err.decode(errors="replace").strip(),
                    )
            commit_failed = [
                {
                    "path": rel_id(p, worktree_root),
                    "reason": (
                        f"commit-failed: {err_msg}"
                        + (
                            " (revert-also-failed: working tree left inconsistent)"
                            if str(p) in revert_failed_paths
                            else ""
                        )
                    ),
                }
                for p in reaped + staged_denorm
            ]
            # staged_denorm (tracked-parent) entries never landed — their
            # denorm_written contribution (deferred until commit success, see
            # docstring) is correctly absent. denorm_written here is still
            # untracked-parent-only and unaffected by this commit failure —
            # no git operation backs those writes, so they remain landed.
            return [], denorm_written, denorm_write_failed, failed + commit_failed

        # Non-fatal index resync (mirrors rm_and_commit's own posture).
        # MUST run before the claim-release call below: the commit just
        # above landed via the isolated private index (idx_path), so the
        # MAIN .git/index is still stale relative to HEAD until this call
        # brings it in line — `release_committed_claims` reads `git status
        # --porcelain` against the main index, and a stale main index
        # reports every reaped path as dirty (`AD <path>`, added-in-index/
        # deleted-in-worktree), tripping the function's fail-safe RETAIN
        # and silently no-op'ing the release (latent-bug fix, C3c: caught
        # by this route's own claim-release test — see
        # test_distill_apply_disposal_claim_release.py).
        #
        # CORRECTED BURST-SOURCE ACCOUNTING (docs/plans/2026-08-13-commit-
        # seams-inherit-lock-reap-and-retry.md C6): the plan's table listed
        # this module as "two `add`s + one `commit`" (3 acquisitions/call).
        # That is not what this function does. Every `read-tree`/`rm`/`add`
        # (denorm+log)/`commit` call above runs with `base_env` --
        # `_make_git_env(idx_path=idx_path)`, i.e. `GIT_INDEX_FILE`
        # redirected to a private temp index -- so NONE of them ever takes
        # the shared `.git/index.lock`, the same private-index escape hatch
        # `scoped_git_commit`'s agree branch already gets credit for in the
        # plan's Problem section. This resync block, which runs WITHOUT
        # `idx_path` (against the REAL `.git/index`), is the ONLY step in
        # this whole function that ever touches the shared lock -- and it
        # was previously one `git update-index` subprocess PER reaped /
        # staged_denorm / log path (an O(N) burst the table never counted).
        #
        # COUNT (AC-7): collapsed to ONE `update-index` invocation covering
        # the whole batch -- `git update-index` applies whichever of
        # `--remove`/`--add` most recently preceded a filename to each
        # subsequent filename argument, so a single process call can stage
        # both halves. `--` is deliberately NOT used here (unlike the
        # single-path calls it replaces): a trailing `--` ends option
        # parsing outright, which would make a second `--add`/`--remove`
        # flag impossible to express in the same invocation; every path
        # here is a worktree-relative repo path, never a caller-controlled
        # arbitrary string, so the usual "a dash-prefixed path could be
        # misread as a flag" hazard does not apply.
        #
        # RETRY (AC-8): composed locally via
        # `coordinator_core.ops.fleet._common._update_index_with_retry` --
        # the SAME exponential-backoff helper `archive_and_commit`/
        # `rm_and_commit` already use for their own main-index resync
        # (imported here, not reimplemented; already in this module's
        # neighbourhood via the existing `_make_git_env`/`main_worktree_root`
        # import from the same `_common` module). `git_native` (C1's retry
        # layer) was rejected as the route: it is a SYNCHRONOUS
        # `subprocess.run` wrapper, and this whole module is async-only by
        # its own module-docstring negative-spec ("does NOT use blocking
        # `subprocess.run` for any git call — every git subprocess is
        # `asyncio.create_subprocess_exec` + await, D4") -- repointing at it
        # would mean either blocking this coroutine's event-loop turn or
        # wrapping it in its own `asyncio.to_thread` shim, whereas
        # `_update_index_with_retry` is already async and already retried on
        # the fleet-derived schedule `git_lock_retry.DEFAULT_BACKOFF_
        # SCHEDULE_S` itself cites as precedent.
        main_env = _make_git_env()
        resync_argv: list[str] = ["git", "update-index"]
        if reaped:
            resync_argv += ["--remove"] + [str(p) for p in reaped]
        resync_add_paths: list[Path] = list(staged_denorm)
        if log_touched:
            resync_add_paths.append(log_path)
        if resync_add_paths:
            resync_argv += ["--add"] + [str(p) for p in resync_add_paths]

        if len(resync_argv) > 2:
            resync_err = await _update_index_with_retry(
                resync_argv, cwd=worktree_root, env=main_env
            )
            if resync_err is not None:
                # Review: code-reviewer P2 (2026-08-13) — `git update-index`
                # applies its argv positionally and can partially succeed
                # before failing on a later path, so the batch below is NOT
                # a confirmed-affected list — it is everything that WAS IN
                # the failing call, some of which may already be correctly
                # staged. Naming a single culprit would require re-running
                # per-path (which would undo the acquisition win this batch
                # collapse exists for — see the COUNT/RETRY comment above),
                # so this message says exactly what it knows: the batch that
                # was in flight, and git's own stderr (resync_err, which
                # names the offending path when git supplies one) — never
                # "these paths are dirty."
                _LOG.error(
                    "distill.apply_disposal: main-index resync FAILED after "
                    "retry — batch attempted (not all necessarily affected; "
                    "git update-index applies positionally and may have "
                    "partially succeeded before failing) was %s. git's own "
                    "error (may name the specific offending path): %s",
                    [rel_id(p, worktree_root) for p in reaped + resync_add_paths],
                    resync_err,
                )

        # Commit succeeded — tracked-parent denorm entries finally land too.
        for p in staged_denorm:
            denorm_written.extend(denorm_applied_by_parent.get(rel_id(p, worktree_root), []))

        # Post-commit claim release (C3, AC1): same worktree, this
        # session's own sid, explicit pathspec (commit_paths, the exact
        # scope of the commit above) — run only now, after the main-index
        # resync loop above, so `git status --porcelain` reads a clean
        # main index (see that loop's comment for why ordering matters
        # here). Never fails an already-landed commit. Offloaded via
        # `asyncio.to_thread` — this module's own D4 mandate (module
        # docstring, DR-228) is "never blocking subprocess.run";
        # `release_committed_claims` issues a synchronous `git status
        # --porcelain` subprocess, so it must not be called in place on
        # this coroutine's event-loop turn.
        try:
            release_paths = [rel_id(Path(p), worktree_root) for p in commit_paths]
            await asyncio.to_thread(
                session_scope.release_committed_claims,
                session_core.resolve_session_id(str(worktree_root)),
                release_paths,
                str(worktree_root),
            )
        except Exception:
            _LOG.debug(
                "distill.apply_disposal: release_committed_claims failed "
                "post-commit; claim(s) retained",
                exc_info=True,
            )

        return (
            [rel_id(p, worktree_root) for p in reaped],
            denorm_written,
            denorm_write_failed,
            failed,
        )
    finally:
        try:
            os.unlink(idx_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# apply_disposal_manifest — top-level orchestration (pure gates + async act)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyDisposalResult:
    """Outcome of one distill.apply_disposal act: which paths were actually
    deleted (deleted_tracked/deleted_untracked, rel-posix ids), which rows
    were skipped and why (already_deleted/newly_blocked), any hard failures
    (git-level errors) that left a survivor un-deleted, and the lineage-
    denormalization outcome (denorm_written/denorm_skipped — see module
    docstring § Lineage denormalization).

    A denorm entry is only ever counted in `denorm_written` if its OWN CHILD
    actually got deleted — an entry whose child's git rm (tracked) or unlink
    (untracked) failed is never written to the parent at all, regardless of
    what the pre-delete plan (`compute_apply_plan`'s `denorm_writes`)
    contained (see module docstring § Lineage denormalization). On top of
    that per-child gate: an entry that lands on a TRACKED parent is only
    counted once the tracked-delete/log commit itself succeeds — a denorm
    write whose commit failed is reverted on disk (see
    `_delete_tracked_and_append_log`) and therefore correctly absent from
    this list. An UNTRACKED parent's write has no commit to wait on and is
    counted as soon as it is written (still gated on its child's own delete
    outcome, per the above).
    """

    run_id: str
    deleted_tracked: list[str]
    deleted_untracked: list[str]
    already_deleted: list[dict[str, str]]
    newly_blocked: list[dict[str, str]]
    failed: list[dict[str, str]]
    denorm_written: list[dict[str, str]] = field(default_factory=list)
    denorm_skipped: list[dict[str, str]] = field(default_factory=list)


async def apply_disposal_manifest(
    worktree_root: Path,
    manifest: dict[str, Any],
    *,
    harvest_committed_sha: str,
) -> ApplyDisposalResult:
    """Full apply_disposal act over an already-loaded, already-validated
    manifest dict: gates 2-5, then per-row TOCTOU re-verify + commutative
    tracked/untracked delete + one scoped commit.

    Raises ApplyDisposalError if any of gates 2-5 fail — no file is touched
    in that case.
    """
    verify_stamp_and_throttle(manifest)
    await verify_drain_ordering(worktree_root, harvest_committed_sha)

    plan = compute_apply_plan(worktree_root, manifest)

    # Partition survivors by git-tracked status FIRST — this ordering (child
    # tracked-status, then untracked delete, then the gated denorm write
    # inside _delete_tracked_and_append_log) is what lets the denorm write
    # set be gated on ACTUAL per-child delete outcomes rather than this
    # pre-delete plan (module docstring § Lineage denormalization; the
    # defect this restructure closes). `plan.denorm_writes` itself stays the
    # untouched, unfiltered pre-delete plan — filtering happens downstream,
    # never here.
    tracked_rows: list[dict[str, Any]] = []
    untracked_rows: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

    status_map = await _is_tracked_batch(
        worktree_root, [worktree_root / row["path"] for row in plan.survivors]
    )
    for row in plan.survivors:
        status = status_map[worktree_root / row["path"]]
        if status == "tracked":
            tracked_rows.append(row)
        elif status == "untracked":
            untracked_rows.append(row)
        else:
            failed.append({
                "path": row["path"],
                "reason": "tracked-status-indeterminate: git ls-files rc!=0/1",
            })

    # Untracked deletes happen next, ahead of the tracked commit — same
    # ordering DR-228 § D5 already accepts (no git history to preserve, no
    # commit to fold into). Crucially, this also FINALIZES which untracked
    # children actually got deleted, so the denorm gate below (applied
    # inside _delete_tracked_and_append_log) never has to guess.
    deleted_untracked: list[str] = []
    confirmed_untracked_rows: list[dict[str, Any]] = []
    for row in untracked_rows:
        abs_path = worktree_root / row["path"]
        # Deliberately left per-item, NOT hoisted into `_is_tracked_batch`
        # (amplification hitlist, 2026-08-19): this recheck exists to
        # narrow the TOCTOU window immediately before THIS row's unlink;
        # batching it ahead of the loop would widen that window for every
        # row after the first, trading a real safety property for a spawn
        # count -- a legitimate REFUSAL, not a missed batching opportunity.
        recheck = await _is_tracked(worktree_root, abs_path)
        if recheck != "untracked":
            failed.append({
                "path": row["path"],
                "reason": f"tracked-status-changed-before-unlink: now {recheck}",
            })
            continue
        try:
            abs_path.unlink()
            deleted_untracked.append(row["path"])
            confirmed_untracked_rows.append(row)
        except OSError as exc:
            failed.append({"path": row["path"], "reason": f"unlink-failed: {exc}"})

    log_path = worktree_root / CANONICAL_LOG_RELPATH
    log_rows: list[dict[str, str]] = [
        {
            "path": row["path"],
            "disposition": "EPHEMERAL",
            "fate": f"disposed ({row['artifact_class']})",
            "run_id": manifest["run_id"],
        }
        for row in sorted(
            tracked_rows + confirmed_untracked_rows, key=lambda r: r["path"]
        )
    ]

    # Lineage denormalization (module docstring § Lineage denormalization):
    # `plan.denorm_writes` is the full pre-delete plan. Only the entries
    # whose child is a confirmed-deleted untracked survivor, or a tracked
    # survivor about to be git-rm'd, are even candidates — a tracked
    # candidate's entry is still gated a SECOND time, inside
    # _delete_tracked_and_append_log, on whether its own git rm actually
    # succeeds, immediately before that entry's parent file is written.
    confirmed_untracked_child_paths = {row["path"] for row in confirmed_untracked_rows}
    tracked_child_paths = {row["path"] for row in tracked_rows}
    candidate_denorm_writes = [
        e for e in plan.denorm_writes
        if e["child"] in confirmed_untracked_child_paths or e["child"] in tracked_child_paths
    ]

    tracked_paths = [worktree_root / row["path"] for row in tracked_rows]
    subject = (
        f"distill: apply disposal for run {manifest['run_id']} "
        f"({len(tracked_paths)} tracked file(s))"
    )
    committed_ids, denorm_written, denorm_write_failed, delete_failed = (
        await _delete_tracked_and_append_log(
            worktree_root, tracked_paths, log_path, log_rows, subject,
            denorm_writes=candidate_denorm_writes,
            confirmed_untracked_child_paths=confirmed_untracked_child_paths,
        )
    )
    failed.extend(denorm_write_failed)
    failed.extend(delete_failed)

    deleted_tracked = [rid for rid in committed_ids]

    # If the commit failed, confirmed_untracked_rows were already physically
    # unlinked (D5's named asymmetry — see module docstring) but their log
    # rows did NOT land; they are neither "deleted_tracked" (they were never
    # tracked) nor cleanly logged. They remain reported in deleted_untracked
    # (the unlink genuinely happened) — this is the accepted residual.

    return ApplyDisposalResult(
        run_id=manifest["run_id"],
        deleted_tracked=deleted_tracked,
        deleted_untracked=deleted_untracked,
        already_deleted=plan.already_deleted,
        newly_blocked=plan.newly_blocked,
        failed=failed,
        denorm_written=denorm_written,
        denorm_skipped=plan.denorm_skipped,
    )


# ---------------------------------------------------------------------------
# Ceremony receipt (receipt_emit pattern — atomic mkstemp+replace, own
# lightweight shape; this is NOT the wsc PipelineContext receipt, which is a
# different ceremony's D-node op_tail concept)
# ---------------------------------------------------------------------------


def write_apply_receipt(
    worktree_root: Path, result: ApplyDisposalResult, *, emitted_at: str | None = None
) -> Path:
    """Write a JSON receipt for one apply_disposal act to
    state/ceremony/distill-apply-disposal/<run_id>-<emitted_at>.json,
    atomically (mkstemp + os.replace, same discipline as C9/C12/C13)."""
    ts = emitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    receipt_dir = worktree_root / "state" / "ceremony" / "distill-apply-disposal"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    target = receipt_dir / f"{result.run_id}-{ts}.json"

    body = {
        "schema_version": _schema.SCHEMA_VERSION,
        "run_id": result.run_id,
        "emitted_at": ts,
        "deleted_tracked": result.deleted_tracked,
        "deleted_untracked": result.deleted_untracked,
        "already_deleted": result.already_deleted,
        "newly_blocked": result.newly_blocked,
        "failed": result.failed,
        "denorm_written": result.denorm_written,
        "denorm_skipped": result.denorm_skipped,
    }

    fd, tmp_path = tempfile.mkstemp(dir=str(receipt_dir), suffix=".tmp")
    try:
        try:
            os.write(fd, json.dumps(body, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp_path, str(target))
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return target


@register_op("distill.apply_disposal")
async def _handler(params: dict, repo_root: Path | None = None) -> dict:
    """distill.apply_disposal handler.

    Params:
        run_id (str, REQUIRED) — locates the stamped disposal-manifest at
            state/scratch/artifact-distillation/<run_id>/disposal-manifest.json
            (the SAME well-known path C12/C13 use — this op never accepts a
            bare manifest_path).
        harvest_committed_sha (str, REQUIRED) — the sha the drain-ordering
            gate verifies is an ancestor of HEAD AND touches the canonical
            log or wiki tree.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the
    originating worktree (_OP_KEY_SCOPE="common_dir"). Fails loud when
    repo_root is None (no silent meta-repo fallback, matching C12/C13's AC5
    precedent).

    Returns a dict with keys: run_id, deleted_tracked, deleted_untracked,
    already_deleted, newly_blocked, failed, receipt_path.
    """
    if repo_root is None:
        raise ValueError(
            "distill.apply_disposal requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be "
            "'common_dir' and _origin_worktree must be present in the "
            "JSON-RPC envelope. No silent fallback to meta-repo."
        )

    run_id = params.get("run_id")
    if not run_id:
        raise ValueError("distill.apply_disposal requires an explicit run_id param.")

    harvest_committed_sha = params.get("harvest_committed_sha")
    if not harvest_committed_sha:
        raise ValueError(
            "distill.apply_disposal requires an explicit harvest_committed_sha "
            "param (drain-ordering gate, DR-228 § D2b(vii))."
        )

    worktree_root = main_worktree_root(repo_root)
    manifest_path = manifest_path_for_run(worktree_root, run_id)

    try:
        manifest = load_disposal_manifest(manifest_path)
        result = await apply_disposal_manifest(
            worktree_root, manifest, harvest_committed_sha=harvest_committed_sha
        )
    except (DisposalStampError, ApplyDisposalError) as exc:
        raise ValueError(str(exc)) from exc

    receipt_path = write_apply_receipt(worktree_root, result)

    return {
        "run_id": result.run_id,
        "deleted_tracked": result.deleted_tracked,
        "deleted_untracked": result.deleted_untracked,
        "already_deleted": result.already_deleted,
        "newly_blocked": result.newly_blocked,
        "failed": result.failed,
        "denorm_written": result.denorm_written,
        "denorm_skipped": result.denorm_skipped,
        "receipt_path": rel_id(receipt_path, worktree_root),
    }
