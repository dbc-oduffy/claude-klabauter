"""
coordinator_core.ops.backfill_reference_edges — "fleet.backfill_reference_edges" op.

Purpose: a ONE-SHOT corpus backfill that stamps the `references:` inbound
edge (plan.schema.json / handoff.schema.json, both bumped in this chunk) onto
every LIVE handoff and LIVE plan whose body demonstrably cites another
artifact by filename — the exact substring predicate
`coordinator_core.ops.fleet.archive_plans._collect_live_reference_text`
already uses to decide "still needed". This op reverse-engineers that
predicate into a stamped, ID-resolved fact so the guard can eventually stop
re-scanning prose (docs/plans/2026-08-18-a-session-always-has-a-baton.md
§ D-D, chunk C7).

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md, chunk
C7 ("IN THIS CHUNK ... ship a one-shot backfill op that stamps existing live
handoffs and plans").

GATING THIS OP DOES NOT SATISFY ALONE: chunk C7's own gating note (revised
per director review F4) requires BOTH a corpus backfill (this op, covering
history) AND an emission path so every newly-authored referrer stamps its
own edge at authoring time (covering the future) before
`_collect_live_reference_text`/`scan_incomplete` can be deleted (chunk C12).
Running this op does not, by itself, license that deletion.

Referrer set: every handoff in state/handoffs/ whose status is NOT in
{claimed, consumed, superseded} (dual-tolerant, DR-084), and every plan in
docs/plans/ whose status is NOT in PLAN_TERMINAL_STATUS, excluding review
sidecars (<stem>.<tag>.md) — the identical referrer set
`_collect_live_reference_text` scans, so this backfill's write scope matches
exactly what the guard will read.

Target catalog: every `plan_id` (docs/plans/*.md) and `handoff_id`
(state/handoffs/*.md) in the corpus, regardless of status — a live referrer
may legitimately cite a now-terminal plan (that is precisely the case the
live-reference guard exists to protect), so the catalog is not status-scoped.
A target with no stamped id (pre-C2 records, or non-corpus files) can never
receive an edge — it is filename-cited but id-less, so there is nothing to
stamp; such citations are silently uncovered by this backfill and remain
covered by the (untouched, authoritative) substring scan.

Match predicate: `target_filename in referrer_body_text` — the SAME substring
test `_collect_live_reference_text`'s callers use
(`path.name in live_ref_text`), applied per-referrer/per-target instead of
against one concatenated blob. A referrer never matches itself.

Idempotent (AC — re-running must not duplicate edges): `_stamp_references`
re-reads the referrer's live `references:` value INSIDE the locked_rmw
closure and only adds ids that are not already members of the merged set;
a referrer whose full computed edge set already equals its on-disk value is
a byte-identical no-op write (locked_rmw skips writing an unchanged body).
The pre-lock scan (`_find_new_edges`) is a fast-path filter only — the
in-lock recheck is what makes the write itself idempotent under concurrent
writers.

dry-run / act split: `dry_run` (default True — safe default, matches the
fleet.* confirm-then-act convention) reports what WOULD be stamped
(`would_stamp`) without writing; `dry_run: false` performs the writes and
reports `stamped`. This is a corpus mutation, so no path defaults to writing.

Negative-spec:
  - Does NOT touch `_collect_live_reference_text`, `scan_incomplete`, or
    either of its fail-closed branches in archive_plans.py — those stay
    authoritative regardless of this op having run (chunk C7 is explicitly
    NOT the deletion chunk; that is C12, separately gated).
  - Does NOT stamp a filename-only match with no resolvable id — a citation
    of an id-less artifact is left for a future minting pass, not fabricated.
  - Does NOT scan archive/ — only the LIVE corpus (docs/plans/,
    state/handoffs/) is read, matching the referrer scope above.
  - Does NOT batch writes into one commit or call git at all — each
    referrer's frontmatter is written independently via the existing
    locked_rmw primitive, mirroring
    coordinator_core.ops.fleet.backfill_memo_disposition's per-file write
    shape. Committing the result is the caller's concern, not this op's.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

from coordinator_core.dag import _read_meta
from coordinator_core.frontmatter.primitives import (
    rebuild,
    serialize_yaml_scalar,
    split_frontmatter,
    write_fm_nested_field,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import PLAN_TERMINAL_STATUS
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root, parse_frontmatter_status

_LOG = logging.getLogger(__name__)

# Dual-tolerant read (DR-084): "consumed" is the pre-rename value, kept as a
# fallback — mirrors archive_plans._RETIRED_HANDOFF_STATUSES exactly.
_RETIRED_HANDOFF_STATUSES: frozenset = frozenset({"claimed", "consumed", "superseded"})


def _is_sidecar(path: Path) -> bool:
    """Return True if path is a review sidecar (<plan-stem>.<tag>.md).

    Mirrors archive_plans._is_sidecar exactly (a sidecar filename has a dot
    in the stem after stripping .md; a primary plan's stem does not).
    """
    return "." in path.stem


def _catalog_targets(worktree_root: Path) -> Dict[str, Tuple[Path, str]]:
    """Map every stamped id (plan_id / handoff_id) in the corpus to
    (its file path, its filename) — ANY status, both docs/plans/ and
    state/handoffs/. See module docstring "Target catalog".
    """
    catalog: Dict[str, Tuple[Path, str]] = {}
    for rel_dir, id_field in (("docs/plans", "plan_id"), ("state/handoffs", "handoff_id")):
        d = worktree_root / rel_dir
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError as exc:
            _LOG.warning(
                "backfill_reference_edges: cannot scan %s for targets — %s", d, exc,
            )
            continue
        for path in entries:
            if path.suffix != ".md" or not path.is_file():
                continue
            if rel_dir == "docs/plans" and _is_sidecar(path):
                continue
            meta = _read_meta(str(path))
            artifact_id = meta.get(id_field)
            if isinstance(artifact_id, str) and artifact_id:
                catalog[artifact_id] = (path, path.name)
    return catalog


def _live_referrers(worktree_root: Path) -> List[Path]:
    """Return every LIVE handoff/plan path — the same referrer set
    archive_plans._collect_live_reference_text scans. See module docstring
    "Referrer set".
    """
    referrers: List[Path] = []

    handoffs_dir = worktree_root / "state" / "handoffs"
    if handoffs_dir.is_dir():
        try:
            handoff_entries = sorted(handoffs_dir.iterdir())
        except OSError as exc:
            _LOG.warning(
                "backfill_reference_edges: cannot scan %s for referrers — %s",
                handoffs_dir, exc,
            )
            handoff_entries = []
        for hpath in handoff_entries:
            if hpath.suffix != ".md" or not hpath.is_file():
                continue
            if parse_frontmatter_status(hpath) in _RETIRED_HANDOFF_STATUSES:
                continue
            referrers.append(hpath)

    plans_dir = worktree_root / "docs" / "plans"
    if plans_dir.is_dir():
        try:
            plan_entries = sorted(plans_dir.iterdir())
        except OSError as exc:
            _LOG.warning(
                "backfill_reference_edges: cannot scan %s for referrers — %s",
                plans_dir, exc,
            )
            plan_entries = []
        for lpath in plan_entries:
            if lpath.suffix != ".md" or not lpath.is_file():
                continue
            if _is_sidecar(lpath):
                continue
            if parse_frontmatter_status(lpath) in PLAN_TERMINAL_STATUS:
                continue
            referrers.append(lpath)

    return referrers


def _find_new_edges(referrer_path: Path, catalog: Dict[str, Tuple[Path, str]]) -> List[str]:
    """Return the sorted list of target ids referrer_path's body cites by
    filename substring but does not already carry in its own `references:`
    list — the set this backfill would add for this referrer.

    Best-effort: an unreadable referrer contributes no edges rather than
    aborting the whole sweep (mirrors _collect_live_reference_text's
    per-file OSError tolerance).
    """
    try:
        text = referrer_path.read_text(errors="replace")
    except OSError as exc:
        _LOG.debug("backfill_reference_edges: could not read %s: %s", referrer_path, exc)
        return []

    meta = _read_meta(str(referrer_path))
    existing = meta.get("references") or []
    if isinstance(existing, str):
        existing = [existing]
    existing_set: Set[str] = set(existing) if isinstance(existing, list) else set()

    new_ids: Set[str] = set()
    for target_id, (target_path, target_filename) in catalog.items():
        if target_path == referrer_path:
            continue  # never self-reference
        if target_id in existing_set:
            continue
        if target_filename in text:
            new_ids.add(target_id)
    return sorted(new_ids)


def _stamp_references(referrer_path: Path, new_ids: List[str], repo_root: Path) -> None:
    """Merge new_ids into referrer_path's `references:` list inside one
    locked_rmw closure.

    Idempotent (see module docstring): re-reads the LIVE frontmatter under
    the lock, computes the union with new_ids, and writes only if that union
    differs from what is already on disk — a second call with the same
    new_ids is a no-op.
    """

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {referrer_path}")

        try:
            fm_dict = yaml.safe_load(split.fm_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"YAML parse error in {referrer_path}: {exc}") from exc

        current = fm_dict.get("references") or []
        if isinstance(current, str):
            current = [current]
        if not isinstance(current, list):
            raise MutateAbort(
                f"{referrer_path}: existing 'references' field is not a list ({current!r})"
            )
        current_set = set(current)

        merged = sorted(current_set | set(new_ids))
        if merged == sorted(current_set):
            return old_text  # already up to date under the lock -- no write

        block_text = "".join(f"  - {serialize_yaml_scalar(v)}\n" for v in merged)
        fm_text = write_fm_nested_field(split.fm_text, "references", block_text)
        return rebuild(split, fm_text)

    locked_rmw(referrer_path, _mutate, repo_root=repo_root)


def _run_backfill(worktree_root: Path, dry_run: bool) -> dict:
    """Scan the live corpus and either report (dry_run) or apply
    (not dry_run) every new `references:` edge. Returns
    {"stamped": [...], "would_stamp": [...], "skipped": [...], "failed": [...]}.
    """
    catalog = _catalog_targets(worktree_root)
    referrers = _live_referrers(worktree_root)

    stamped: List[dict] = []
    would_stamp: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []

    for referrer_path in referrers:
        new_ids = _find_new_edges(referrer_path, catalog)
        if not new_ids:
            skipped.append({"id": referrer_path.name, "reason": "no-new-edges"})
            continue

        entry = {"id": referrer_path.name, "references_added": new_ids}
        if dry_run:
            would_stamp.append(entry)
            continue

        try:
            _stamp_references(referrer_path, new_ids, worktree_root)
        except MutateAbort as exc:
            failed.append({
                "id": referrer_path.name,
                "reason": str(exc.args[0]) if exc.args else "mutate-abort",
            })
            continue
        except LockTimeout as exc:
            failed.append({"id": referrer_path.name, "reason": str(exc)})
            continue
        except FileNotFoundError:
            failed.append({"id": referrer_path.name, "reason": "not-found"})
            continue

        stamped.append(entry)

    return {
        "stamped": stamped,
        "would_stamp": would_stamp,
        "skipped": skipped,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


def _coerce_dry_run(raw: object) -> bool:
    """Coerce a wire-supplied dry_run value to bool. Defaults True (safe
    default — see module docstring "dry-run / act split")."""
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("false", "0", "no")


@register_op("fleet.backfill_reference_edges")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "fleet.backfill_reference_edges" handler.

    Params:
        dry_run (bool, optional, default True) — True: report would_stamp
            without writing. False: perform the writes and report stamped.

    repo_root arrives as the git common dir (same handler-arg convention as
    the fleet.* op family); the worktree is derived via main_worktree_root,
    never taken from params.
    """
    dry_run = _coerce_dry_run(params.get("dry_run"))

    if repo_root is None:
        _LOG.error("fleet.backfill_reference_edges: repo_root handler arg is None")
        return {
            "exit_code": 1,
            "dry_run": dry_run,
            "stamped": [],
            "would_stamp": [],
            "skipped": [],
            "failed": [],
            "error": "repo_root is None",
        }

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    result = await asyncio.to_thread(_run_backfill, worktree, dry_run)
    return {"exit_code": 0, "dry_run": dry_run, **result}
