"""
coordinator_core.ops.fleet.delete_superseded_decisions — fleet.delete_superseded_decisions op.

Purpose: delete a `status: superseded` decision record from `docs/decisions/`
once nothing on a live surface still cites it. This is the fourth
status-driven sweep of its kind, after `archive_actioned_memos`,
`archive_terminal_handoffs` and `archive_sizings` — same `_handler` envelope,
lock and `record_sweep_outcome` receipt shape (composed from
`archive_actioned_memos.py`, not transcribed), same shared
`validate_params` dry_run/candidate_ids contract plus a required `cap`
(mirroring `archive_actioned_memos`'s own no-unbounded-default cap axis). The
unit of work is a DELETE, not a move, so the act path lands through
`_common.rm_and_commit` — the DR-211 D3/D4 delete seam `reap_integrated_findings`
already uses — rather than `archive_and_commit`.

DR-211 D1/D2 mapping: `fleet.` prefix puts this op under D1's ratified
`fleet.*` archival-writer category. D2's five conditions: idempotent (an
already-deleted record is a no-op — source-gone act-time skip); commutative
(set-difference delete over the candidate list); git-reversible (the entire
argument for deleting — the record survives in git history); re-verified at
act time (status re-checked before the delete); reachable over UDS only,
like every sibling op.

Terminality: `_common.parse_frontmatter_status(path) == "superseded"`, read
over `docs/decisions/*.md` (flat glob, the same one
`block_duplicate_decision_record_id` uses). Never writes or infers a status
(the DR-293 never-infer boundary `archive_sizings` states) — read-then-delete
or refuse-to-delete only.

Successor: the victim's `superseded_by:` field
(`_common.parse_frontmatter_field`), or else any record whose `supersedes:`
names the victim's id, collected in the same scan pass. May be absent (as it
is for DR-316 at authoring time).

Citation-stranding refusal — ONE batched search, never a per-record spawn:
a single `git grep -n -w -E 'DR-(<id1>|<id2>|…)' -- <live pathspecs>` spawn
over every candidate at once, parsed in-process, and skipped entirely when
the candidate set is empty. The live pathspecs
(`_LIVE_SURFACE_PATHSPECS`) are `coordinator_core`, `coordinator`, `scripts`,
`docs/decisions`, `docs/reference`, `docs/wiki`, `CLAUDE.md`. Everything
else — `archive/`, `state/`, `docs/plans/`, `docs/research/`,
`docs/problems/`, `tasks/`, generated indexes — counts as history and is not
searched, matching the DR-405 renumbering precedent: "Citations … in
archived and frozen artifacts are deliberately left stale: they describe
what was true when written." A hit on id X is IGNORED when it is in X's own
file, when it is in X's successor's file (that file IS the live pointer), or
when the same line also names X's successor id (known false-negative: a line
asserting the victim OVER its successor is still exempt by this heuristic —
accepted, not hardened against, per C2's own review note). Any other hit
refuses X, and the refusal names every `path:line`. A victim with no
successor can only be deleted once nothing on a live surface names it at
all.

Max-id floor: `dr_allocator.allocate_dr_number` returns
`max(on-disk id) + 1`. Deleting the highest-numbered record in the namespace
would let the next allocation issue the same id again, so a candidate whose
numeric id equals the corpus maximum (computed from the ids the scan already
holds — no extra spawn or allocator import) is refused with reason
`max-id-floor`.

Message register: the act commit is subject-only
(`fleet: delete N superseded decision record(s)`), landed via
`rm_and_commit(worktree_root, paths, subject)` — `subject=` only, never a
`body:` kwarg (`_common.py` is not edited by this op). The git-history
recovery pointer ("Removed from the working tree; stays in git history:
git log --diff-filter=D -- docs/decisions/<file>") is carried once per
deleted entry in the result payload's `acted[]` item and once in the
`record_sweep_outcome` receipt's `detail`, never in the commit body.

No occasion is wired — reachable on demand only
(`python -m coordinator_core.invoke fleet.delete_superseded_decisions '<params>'`),
mirroring `fleet.archive_terminal_sizings` before its close-verb caller.

Spec: docs/plans/2026-09-11-delete-superseded-drs-and-put-the-prune-rule-to-the-pm.md (C1)
Spec backlinks:
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1/D2/D3/D4)
  - DR-405: renumbering precedent for excluding history surfaces from citation search
  - DR-293: never-infer boundary (status is read, never written)

Negative-spec:
  - Does NOT read a candidate's body beyond its frontmatter `status`/
    `superseded_by`/`supersedes` fields — no full-file read for the scan.
  - Does NOT spawn a `git grep` per candidate — exactly one batched spawn
    over the whole candidate set, and none at all when the set is empty.
  - Does NOT auto-rewrite a stranding citation. It refuses and names the
    citation; a person edits it.
  - Does NOT wire any occasion (SessionStart, commit pipeline,
    housekeeping.cycle) — on-demand invocation only.
  - Does NOT accept a `force` flag, wire param, or env override that deletes
    past a citation refusal or the max-id floor.
  - Does NOT edit `coordinator_core/ops/fleet/_common.py` — composes
    `rm_and_commit`/`parse_frontmatter_status`/`parse_frontmatter_field`
    unmodified.
  - Does NOT harden the same-line-names-successor exemption against a line
    that asserts the victim's ruling OVER its successor — a known,
    accepted false-negative (C2's own review note), not an oversight.
  - Does NOT accept an absent `cap` — no unbounded default, mirroring
    `archive_actioned_memos`'s own binding cap-axis decision.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    parse_frontmatter_field,
    parse_frontmatter_status,
    rel_id,
    rm_and_commit,
    validate_params,
)
from coordinator_core.ops.fleet._sweep_receipt import record_sweep_outcome
from coordinator_core.win_portability import no_console_creationflags

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

_OP_KEY = "fleet.delete_superseded_decisions"

_FAMILY = "decision-record"

_LIVE_SURFACE_PATHSPECS: Tuple[str, ...] = (
    "coordinator_core",
    "coordinator",
    "scripts",
    "docs/decisions",
    "docs/reference",
    "docs/wiki",
    "CLAUDE.md",
)

_REASON_MAX_ID_FLOOR = "max-id-floor"

_DR_ID_RE = re.compile(r"^DR-(\d+)-")

_DR_ID_ANY_RE = re.compile(r"DR-(\d+)")

_RECOVERY_SENTENCE_TEMPLATE = (
    "Removed from the working tree; stays in git history: "
    "git log --diff-filter=D -- {path}"
)


def _decisions_dir(worktree_root: Path) -> Path:
    return worktree_root / "docs" / "decisions"


def _dr_number(filename: str) -> Optional[int]:
    m = _DR_ID_RE.match(filename)
    if not m:
        return None
    return int(m.group(1))


class _Candidate:
    __slots__ = ("path", "rel", "dr_id", "number", "successor")

    def __init__(self, path: Path, rel: str, dr_id: Optional[int], successor: Optional[str]):
        self.path = path
        self.rel = rel
        self.dr_id = dr_id
        self.successor = successor


def _scan_superseded(worktree_root: Path) -> Tuple[List[_Candidate], int]:
    decisions_dir = _decisions_dir(worktree_root)
    candidates: List[_Candidate] = []
    max_id = 0
    if not decisions_dir.is_dir():
        return candidates, max_id

    all_records: List[Tuple[Path, str, Optional[str], Optional[str]]] = []
    for path in sorted(decisions_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        num = _dr_number(path.name)
        if num is not None and num > max_id:
            max_id = num
        status = parse_frontmatter_status(path)
        superseded_by = parse_frontmatter_field(path, "superseded_by")
        supersedes = parse_frontmatter_field(path, "supersedes")
        all_records.append((path, status or "", superseded_by, supersedes))

    supersedes_of: Dict[str, str] = {}
    for path, _status, _sb, supersedes in all_records:
        if supersedes:
            supersedes_of[str(supersedes).strip()] = path.name

    for path, status, superseded_by, _supersedes in all_records:
        if status.strip().lower() != "superseded":
            continue
        num = _dr_number(path.name)
        successor = superseded_by
        if not successor:
            stem_id = path.name.split("-", 2)
            token = f"DR-{stem_id[1]}" if len(stem_id) > 1 else None
            if token and token in supersedes_of:
                successor = supersedes_of[token]
        candidates.append(
            _Candidate(path=path, rel=rel_id(path, worktree_root), dr_id=num, successor=successor)
        )
    return candidates, max_id


def _successor_filename(successor: Optional[str]) -> Optional[str]:
    if not successor:
        return None
    return successor


async def _live_citations(
    worktree_root: Path,
    candidates: List[_Candidate],
) -> Dict[str, List[str]]:
    if not candidates:
        return {}

    import asyncio

    ids = []
    for c in candidates:
        if c.dr_id is not None:
            ids.append(str(c.dr_id))
    if not ids:
        return {}

    pattern = "DR-(" + "|".join(ids) + ")"
    argv = [
        "git", "grep", "-n", "-w", "-E", pattern, "--",
    ] + list(_LIVE_SURFACE_PATHSPECS)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(worktree_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **no_console_creationflags(),
    )
    out, stderr = await proc.communicate()
    if proc.returncode not in (0, 1):
        _LOG.warning(
            "fleet.delete_superseded_decisions: git grep failed (rc=%s): %s",
            proc.returncode, stderr.decode(errors="replace").strip(),
        )
        return {}

    id_to_candidate = {str(c.dr_id): c for c in candidates if c.dr_id is not None}
    hits: Dict[str, List[str]] = {}
    id_hit_re = re.compile(r"\bDR-(\d+)\b")

    for line in out.decode(errors="replace").splitlines():
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        hit_path, hit_lineno, content = parts[0], parts[1], parts[2]

        matched_ids = set(id_hit_re.findall(content))
        for mid in matched_ids:
            candidate = id_to_candidate.get(mid)
            if candidate is None:
                continue
            if hit_path == candidate.rel or Path(hit_path).name == candidate.path.name:
                continue
            successor = _successor_filename(candidate.successor)
            succ_num: Optional[int] = None
            if successor:
                m = _DR_ID_ANY_RE.search(successor)
                if m:
                    succ_num = int(m.group(1))
            if succ_num is not None and _dr_number(Path(hit_path).name) == succ_num:
                continue
            if succ_num is not None and f"DR-{succ_num}" in content:
                continue
            hits.setdefault(candidate.rel, []).append(f"{hit_path}:{hit_lineno}")

    return hits


def _handle_preview(worktree_root: Path, cap: int) -> Tuple[dict, List[dict]]:
    import asyncio

    candidates, max_id = _scan_superseded(worktree_root)
    scan_skipped: List[dict] = []

    if not candidates:
        return build_dry_run_result("already-terminal", []), scan_skipped

    floor_refused = {c.rel for c in candidates if c.dr_id == max_id}
    for rel in floor_refused:
        scan_skipped.append({"id": rel, "reason": _REASON_MAX_ID_FLOOR})

    survivors = [c for c in candidates if c.rel not in floor_refused]

    hits = asyncio.run(_live_citations(worktree_root, survivors))
    for rel, locs in hits.items():
        scan_skipped.append({
            "id": rel,
            "reason": f"live-citation: {', '.join(locs)}",
        })

    clean = [c for c in survivors if c.rel not in hits]
    clean = clean[:cap]
    deferred = survivors[len(clean):] if len(survivors) > cap else []

    wire_candidates = []
    for c in clean:
        wire_candidates.append({
            "id": c.rel,
            "title": c.path.stem,
            "status": "superseded",
            "family": _FAMILY,
            "terminal_since": None,
            "note": f"successor={c.successor!r}" if c.successor else "no successor",
        })

    result = build_dry_run_result("already-terminal", wire_candidates)
    return result, scan_skipped


def _handle_act(worktree_root: Path, candidate_ids: List[str], cap: int) -> dict:
    import asyncio

    decisions_dir = _decisions_dir(worktree_root)
    candidates, max_id = _scan_superseded(worktree_root)
    by_rel = {c.rel: c for c in candidates}

    skipped: List[dict] = []
    to_delete: List[_Candidate] = []

    for cid in candidate_ids:
        target = worktree_root / cid
        if not target.exists():
            skipped.append({"id": cid, "reason": "already-deleted"})
            continue

        c = by_rel.get(cid)
        if c is None:
            status = parse_frontmatter_status(target)
            skipped.append({
                "id": cid,
                "reason": f"terminality-drift: status is now {status!r}",
            })
            continue

        if c.dr_id == max_id:
            skipped.append({"id": cid, "reason": _REASON_MAX_ID_FLOOR})
            continue

        to_delete.append(c)

    if to_delete:
        hits = asyncio.run(_live_citations(worktree_root, to_delete))
        for rel, locs in hits.items():
            skipped.append({"id": rel, "reason": f"live-citation: {', '.join(locs)}"})
        to_delete = [c for c in to_delete if c.rel not in hits]

    to_delete = to_delete[:cap]

    acted: List[dict] = []
    failed: List[dict] = []

    if to_delete:
        n = len(to_delete)
        subject = f"fleet: delete {n} superseded decision record(s)"
        reaped, raw_failed = asyncio.run(
            rm_and_commit(worktree_root, [c.path for c in to_delete], subject)
        )
        reaped_by_id = {r["id"]: r for r in reaped}
        for c in to_delete:
            if c.rel in reaped_by_id:
                acted.append({
                    "id": c.rel,
                    "deleted": True,
                    "recovery": _RECOVERY_SENTENCE_TEMPLATE.format(path=c.rel),
                })
            else:
                failure_reason = next(
                    (f["reason"] for f in raw_failed if f["id"] == c.rel), "delete-failed"
                )
                failed.append({"id": c.rel, "reason": failure_reason})

    return build_act_result("already-terminal", acted, skipped, failed)


@register_op(_OP_KEY)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.delete_superseded_decisions — delete status:superseded decision
    records once no live surface still cites them.

    dry_run:true  → preview: enumerate superseded records, minus max-id-floor
                    and citation-stranding refusals, cap-slotted.
    dry_run:false → act: re-verify each candidate_id, delete via
                    rm_and_commit, one commit for the whole batch.

    repo_root arg is the git common dir (_OP_KEY_SCOPE = "common_dir").
    Worktree root is derived via main_worktree_root(common_dir) — NOT
    params.repo_root, which is the optional D3 consistency check only.
    """
    parsed = validate_params(params)
    if isinstance(parsed, dict):
        record_sweep_outcome(
            None, _OP_KEY, "failed", detail="validate_params rejected the call",
        )
        return parsed

    mode, dry_run, candidate_ids = parsed

    cap = params.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        record_sweep_outcome(
            repo_root, _OP_KEY, "failed",
            detail=f"cap is required and must be a positive int, got {cap!r}",
        )
        return build_setup_error_result(
            mode, dry_run,
            f"cap is required and must be a positive int, got {cap!r} — "
            f"no unbounded default (see archive_actioned_memos's cap-axis decision)",
        )

    if repo_root is None:
        _LOG.error("fleet.delete_superseded_decisions: repo_root handler arg is None")
        return build_setup_error_result(mode, dry_run, "repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree_root = main_worktree_root(common_dir)

    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        record_sweep_outcome(common_dir, _OP_KEY, "failed", detail=mismatch)
        return build_setup_error_result(mode, dry_run, mismatch)

    try:
        if dry_run:
            result, scan_skipped = _handle_preview(worktree_root, cap)
            n = len(result.get("candidates") or [])
            outcome = "nothing-to-do" if not n else "applied"
            detail = None
            if scan_skipped:
                detail = "; ".join(f"{s['id']}: {s['reason']}" for s in scan_skipped[:8])
            record_sweep_outcome(common_dir, _OP_KEY, outcome, count=n, detail=detail)
            return result

        if candidate_ids is None:
            record_sweep_outcome(
                common_dir, _OP_KEY, "failed",
                detail="candidate_ids resolved to None on the act path after "
                "validate_params accepted it",
            )
            return build_setup_error_result(
                mode, dry_run,
                "candidate_ids resolved to None on the act path after "
                "validate_params accepted it — contract violation, refusing",
            )

        result = _handle_act(worktree_root, candidate_ids, cap)
        acted = result.get("acted") or []
        n_acted = len(acted)
        if result.get("failed"):
            record_sweep_outcome(
                common_dir, _OP_KEY, "failed", count=n_acted,
                detail=f"{len(result['failed'])} item(s) failed to delete",
            )
        elif n_acted:
            detail = "; ".join(a["recovery"] for a in acted[:4])
            record_sweep_outcome(common_dir, _OP_KEY, "applied", count=n_acted, detail=detail)
        else:
            record_sweep_outcome(common_dir, _OP_KEY, "nothing-to-do", count=0)
        return result
    except Exception as exc:  # noqa: BLE001 — a sweep must record its own failure, never vanish
        record_sweep_outcome(common_dir, _OP_KEY, "failed", detail=str(exc))
        raise
