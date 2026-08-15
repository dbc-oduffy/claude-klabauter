"""
coordinator_core.ops.ceremony.receipt_emit — Ceremony evidence receipt emitter.

Purpose: Writes state/ceremony/<ceremony>/<sid-short>-<emitted-at>.json from a
PipelineContext.  Historically called by the two-phase ceremony.wsc_resolve
(phase-1) / ceremony.wsc_commit (phase-2) pipeline; both ops' registrations
were removed 2026-07-29 (kill-list op removal) — the single-pass ceremony.wsc_tail
op is the live caller today. NOT an op — no @register_op call here.

Session-keyed tracked shard (C3, 2026-07-08-concurrency-safe-strangled-op-writes.md
§ "The session-keyed shard shape"): the receipt used to live at the fixed singleton
path state/ceremony/<ceremony>-receipt.json, so two concurrent /workstream-complete
runs clobbered each other's receipt.  It is now re-keyed on `sid` — one shard file
per session, filename `<sid-short>-<emitted_at>.json` — mirroring the append-only
review-trail `<TIMESTAMP>-<SID_SHORT>` shape.  Kept under TRACKED state/ (not
`.git/coordinator-sessions/<sid>/`) because the receipt is committed ceremony
*evidence* with a durable-history lifetime, not ephemeral per-session resumability
state — see `~/.claude/docs/wiki/session-state-contract.md` (per-session-file
convention: "separate file per session, never a shared singleton"; this module's
different *location* for the receipt reflects its different *lifetime*, not a
departure from that rule).

Key rules (HARD, from the design §receipt):
  - op_tail is a DERIVED VIEW over the D-node ledger — computed at emit time via
    compute_op_tail(ctx.nodes, phase=tail_phase).  NOT stored independently.
  - Atomic write: mkstemp in the same dir → os.replace.  No partial files.
  - Graceful-absent: read_receipt returns NOT_YET_RUN_SENTINEL when the file is
    absent; callers MUST test is_not_yet_run() rather than catching exceptions.
  - Creates state/ceremony/<ceremony>/ if absent (mkdir parents=True, exist_ok=True).
  - Phase-1 (resolve) and phase-2 (commit) write to the SAME sid-keyed shard —
    phase-2 overwrites phase-1 in place (same sid → same shard path, resolved via
    resolve_latest_receipt_path()). Historical: this described the retired
    wsc_resolve/wsc_commit pipeline; wsc_tail.py's single-pass call writes once.
  - NO op registration — this module is called by wsc_tail, not registered as
    a standalone JSON-RPC op.

Public API:
  emit_receipt(ctx, out_path=None, *, repo_root, sid, tail_phase, receipt_phase, emitted_at)
    → tuple[Path, dict]               # (receipt_path, computed_op_tail)
  read_receipt(path) → dict           # NOT_YET_RUN_SENTINEL on absent file
  is_not_yet_run(receipt) → bool
  default_receipt_path(repo_root, ceremony, sid, emitted_at) → Path
    # deterministic write-path for a given (ceremony, sid, emitted_at) triple
  resolve_latest_receipt_path(repo_root, ceremony, sid) → Optional[Path]
    # most-recent-for-this-sid shard lookup; None when no shard exists yet for sid
  NOT_YET_RUN_SENTINEL                # constant sentinel dict

Spec backlink:
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § Design → receipt
  docs/plans/2026-07-08-concurrency-safe-strangled-op-writes.md § C3, § "The
    session-keyed shard shape (ceremony receipt, C3)"
  coordinator_core/ops/ceremony/receipt_schema.py — node shapes + compute_op_tail
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Generator-provenance declaration: emit_receipt() creates a NEW tracked
# JSON shard per (ceremony, sid, emitted_at) under
# state/ceremony/<ceremony>/<sid-short>-<emitted_at>.json -- an unbounded,
# session-keyed, data-dependent output set with no fixed artifact path (see
# default_receipt_path/resolve_latest_receipt_path above), the case
# generator_provenance.py reserves for MUTATES rather than GENERATES.
MUTATES = ["state/ceremony/**/*.json"]

from coordinator_core.engine_version import resolve_engine_dirty, resolve_engine_sha
from coordinator_core.ops.ceremony.pipeline_context import PipelineContext
from coordinator_core.ops.ceremony.receipt_schema import (
    CEREMONY_RECEIPT_DIR,
    COVERAGE_GATE_RELPATH,
    compute_op_tail,
    make_receipt,
)

# ---------------------------------------------------------------------------
# Sentinel — returned by read_receipt when the file is absent
# ---------------------------------------------------------------------------

NOT_YET_RUN_SENTINEL: dict[str, Any] = {"pipeline_not_yet_run": True}
"""Sentinel dict returned by read_receipt() when no receipt file exists.

Absence of the receipt file means "pipeline not yet run", not an error.
Use is_not_yet_run(receipt) to test for this sentinel; the sentinel shape
is stable through is_not_yet_run() but the dict's other keys may evolve.
"""


def is_not_yet_run(receipt: dict[str, Any]) -> bool:
    """Return True when receipt is the NOT_YET_RUN_SENTINEL (file was absent)."""
    return bool(receipt.get("pipeline_not_yet_run"))


# ---------------------------------------------------------------------------
# Session-keyed shard path helpers (C3 — tracked shard, not a singleton)
# ---------------------------------------------------------------------------

_SID_SHORT_LEN = 12
"""Length of the filesystem-safe sid slug embedded in the shard filename.

Filename-legibility truncation only.  Two distinct hazards this bounds:

  1. Long-sid truncation collision: two distinct high-entropy sids that share
     an identical first-12-char slug prefix.  Vanishingly unlikely (session
     ids are high-entropy) but a known limitation of this truncation.
  2. Short-sid cross-match (CLOSED, review-integrator 2026-07-08 Finding 1):
     a sid whose slug is SHORTER than 12 chars (e.g. "a1") used to be a
     literal filename prefix of another sid's shard (e.g. "a1-x" -> shard
     "a1-x-<ts>.json"), so the unanchored glob f"{prefix}-*.json" would
     cross-match the wrong session's shard.  resolve_latest_receipt_path()
     now filters glob candidates so only a shard whose sid-short segment is
     an EXACT match (not merely a filename prefix) is returned — see that
     function's docstring for the anchoring mechanism.

receipt_schema.make_receipt (Finding 5, review-integrator 2026-07-08) now
also carries a `sid` field in the receipt body, so a read-back can verify
the resolved shard belongs to the expected sid as defense-in-depth on top
of the filename-level anchoring above (originally verified via
_build_ctx_from_params in the now-deleted wsc_commit.py — see wsc_tail.py's
own receipt-read call sites for today's equivalent).
"""

_SID_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _sid_short(sid: str) -> str:
    """Return a filesystem-safe, length-bounded slug of *sid* for filenames."""
    slug = _SID_SLUG_RE.sub("-", sid).strip("-")
    return (slug or "sid")[:_SID_SHORT_LEN]


def default_receipt_path(
    repo_root: Path,
    ceremony: str = "wsc",
    *,
    sid: str,
    emitted_at: str,
) -> Path:
    """Return the deterministic session-keyed shard path for a ceremony receipt.

    Path: <repo_root>/state/ceremony/<ceremony>/<sid-short>-<emitted_at>.json

    Re-keyed (C3, 2026-07-08-concurrency-safe-strangled-op-writes.md) from the
    prior fixed singleton `state/ceremony/<ceremony>-receipt.json`, which two
    concurrent ceremonies (distinct sessions) would silently clobber.  The
    shard is a pure function of (ceremony, sid, emitted_at) — WRITE callers
    (emit_receipt) use this directly since they control emitted_at at write
    time.  READ callers that only have `sid` (the phase-1→phase-2 cross-process
    fallback) MUST use resolve_latest_receipt_path() instead — that is a glob
    lookup, not a call to this function, because phase-2 computes a DIFFERENT
    wall-clock timestamp than phase-1 and would miss phase-1's shard if it
    tried to reconstruct the filename from `sid` alone.

    Negative-spec: does NOT create the directory — callers (emit_receipt) are
    responsible for directory creation before writing.
    """
    emitted_at_slug = emitted_at.replace(":", "").replace("-", "")
    filename = f"{_sid_short(sid)}-{emitted_at_slug}.json"
    return repo_root / CEREMONY_RECEIPT_DIR / ceremony / filename


def resolve_latest_receipt_path(
    repo_root: Path,
    ceremony: str,
    sid: str,
) -> Optional[Path]:
    """Resolve the most-recent tracked shard for (ceremony, sid), or None.

    This is the READ-side counterpart to default_receipt_path(): it resolves
    "the shard for my sid" via a most-recent-for-my-sid glob over the shard
    directory, rather than recomputing a filename from a wall-clock timestamp
    (phase-2 would compute a DIFFERENT emitted_at than phase-1 and silently
    miss phase-1's shard if it tried filename reconstruction instead of a
    glob).  Both phase-1 (wsc_resolve) and phase-2 (wsc_commit) call this to
    resolve "my own" receipt shard from `sid` alone.

    Returns None when no shard exists yet for this sid — callers combine this
    with read_receipt()/is_not_yet_run() for the graceful-absent contract
    (a not-yet-emitted receipt still reads as absent, not an error).

    Ordering: shard filenames sort lexicographically by emitted_at slug
    (zero-padded ISO 8601 digits-only), so max() over the glob matches is the
    most-recent shard without needing to parse timestamps.

    Anchoring (review-integrator 2026-07-08 Finding 1): the raw glob
    f"{prefix}-*.json" is an UNANCHORED prefix match — when a sid's slug is
    shorter than _SID_SHORT_LEN (e.g. "a1"), it is also a literal filename
    prefix of a DIFFERENT sid's shard (e.g. "a1-x" -> "a1-x-<ts>.json"), so
    the raw glob would cross-match the wrong session's shard.  Candidates are
    filtered post-glob so only filenames where the segment immediately
    following "{prefix}-" is the emitted_at timestamp slug (digits/`T`/`Z`
    only, per default_receipt_path's emitted_at_slug encoding) — never
    another slug/hyphen character — are treated as this sid's own shard.
    """
    shard_dir = repo_root / CEREMONY_RECEIPT_DIR / ceremony
    if not shard_dir.is_dir():
        return None
    prefix = _sid_short(sid)
    candidates = sorted(shard_dir.glob(f"{prefix}-*.json"))
    matches = [p for p in candidates if _is_exact_sid_shard(p.name, prefix)]
    return matches[-1] if matches else None


_TIMESTAMP_SLUG_RE = re.compile(r"^[0-9TZ]+$")
"""Matches an emitted_at_slug produced by default_receipt_path (digits + T/Z only,
e.g. "20260708T100000Z") — used to anchor the shard-filename prefix match so a
short sid slug is never mistaken for a filename-prefix of a different sid's shard."""


def _is_exact_sid_shard(filename: str, prefix: str) -> bool:
    """Return True when *filename* is this sid's OWN shard, not a cross-match.

    filename is expected to be "<sid-short>-<emitted_at_slug>.json".  A raw
    glob on f"{prefix}-*.json" also matches a DIFFERENT sid's shard when
    *prefix* is a literal filename prefix of that sid's own sid-short segment
    (short-sid hazard — Finding 1).  Anchor by requiring the remainder after
    "{prefix}-" to be a well-formed emitted_at timestamp slug, not another
    sid-short segment's tail.
    """
    remainder = filename[len(prefix) + 1:]  # strip "{prefix}-"
    ts_slug = remainder[: -len(".json")] if remainder.endswith(".json") else remainder
    return bool(_TIMESTAMP_SLUG_RE.match(ts_slug))


# ---------------------------------------------------------------------------
# emit_receipt — main public API
# ---------------------------------------------------------------------------


def emit_receipt(
    ctx: PipelineContext,
    out_path: Optional[Path] = None,
    *,
    repo_root: Optional[Path] = None,
    sid: Optional[str] = None,
    tail_phase: str = "archival",
    receipt_phase: str = "phase-1",
    emitted_at: Optional[str] = None,
) -> tuple[Path, dict[str, Any]]:
    """Derive op_tail from ctx.nodes and write the receipt atomically.

    Both phase-1 (resolve) and phase-2 (commit) call this function with
    different receipt_phase values; phase-2 overwrites the phase-1 shard IN
    PLACE at the same path, provided the SAME `sid` is passed both times (the
    load-bearing C3 contract — see resolve_latest_receipt_path()).

    Parameters
    ----------
    ctx:
        The resolved PipelineContext (populated by wsc_resolve or wsc_commit).
        ctx.nodes is the source of truth for op_tail derivation.
    out_path:
        Explicit output path.  When None, resolves via `sid`/`repo_root` (see
        below).  Explicit out_path bypasses the sid-keyed resolution entirely
        (used by tests exercising the raw write path).
    repo_root:
        Required when out_path is None.  Path to the git working tree root.
    sid:
        Required when out_path is None.  The ceremony session id.  Resolution:
        if a shard already exists for (ceremony, sid) — i.e. a phase-1 receipt
        was already written this session — REUSE that exact path (phase-2
        overwrite-in-place).  Otherwise mint a fresh deterministic shard path
        via default_receipt_path(repo_root, ctx.ceremony, sid=sid,
        emitted_at=<this call's timestamp>).
    tail_phase:
        The ceremony-specific label for the tail phase (e.g. ``"archival"``
        for wsc, ``"consume"`` for pickup).  Passed to compute_op_tail.
    receipt_phase:
        Top-level receipt phase: ``"phase-1"`` (resolve) or ``"phase-2"``
        (commit).  Phase-2 overwrites phase-1 in place (same shard, same sid).
    emitted_at:
        ISO 8601 UTC timestamp string.  When None, uses the current UTC wall
        clock formatted as ``%Y-%m-%dT%H:%M:%SZ``.  Recorded inside the
        receipt body regardless of whether it also feeds the shard filename
        (it does only for a FRESH shard — see `sid` above).

    Returns
    -------
    tuple[Path, dict]
        ``(path, computed_op_tail)`` — the path the receipt was written to and
        the op_tail dict that was embedded in the receipt.

    Review: code-reviewer F7 — returning computed_op_tail alongside the path
    guarantees callers receive exactly the op_tail that was written to the receipt
    (computed once from ctx.nodes), eliminating the double-computation pattern that
    allowed the response op_tail to silently diverge from the persisted receipt.

    Atomic-write contract:
        mkstemp in the same directory as *out_path* → os.replace.  The
        target is never observed in a partially-written state by concurrent
        readers.  The temp file is cleaned up on any write failure.

    op_tail derivation:
        op_tail = compute_op_tail(ctx.nodes, phase=tail_phase).  The ledger
        (ctx.nodes) is the single source of truth; op_tail is NOT stored
        independently of the ledger.

    Directory creation:
        state/ceremony/<ceremony>/ (or out_path.parent) is created with
        parents=True, exist_ok=True before the write.
    """
    # --- compose timestamp (needed before path resolution — a fresh shard's
    #     filename embeds this same emitted_at) ---
    ts = emitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- resolve output path ---
    if out_path is None:
        if repo_root is None:
            raise ValueError(
                "Either out_path or repo_root must be supplied to emit_receipt"
            )
        if not sid:
            raise ValueError(
                "sid must be supplied to emit_receipt when out_path is not given "
                "(session-keyed shard resolution requires sid — see C3)"
            )
        # Same-sid overwrite-in-place: reuse the existing shard if phase-1
        # already wrote one this session; otherwise mint a fresh shard path.
        existing = resolve_latest_receipt_path(repo_root, ctx.ceremony, sid)
        out_path = existing or default_receipt_path(
            repo_root, ctx.ceremony, sid=sid, emitted_at=ts
        )

    # --- ensure directory exists ---
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # --- derive op_tail from the D-node ledger (NOT stored independently) ---
    op_tail = compute_op_tail(ctx.nodes, phase=tail_phase)

    # --- resolve engine self-version (C2) — None when unresolvable, graceful-absent ---
    engine_sha = resolve_engine_sha()
    # engine_sha alone does not prove the code that ran matches that commit's
    # tree (this working tree is dirty essentially continuously) — resolve the
    # dirty discriminator alongside it so a receipt can carry both.
    engine_dirty = resolve_engine_dirty()

    # --- build receipt dict ---
    receipt = make_receipt(
        ceremony=ctx.ceremony,
        phase=receipt_phase,
        emitted_at=ts,
        scope_mode=ctx.scope_mode,
        nodes=ctx.nodes,
        op_tail=op_tail,
        coverage_pointer=COVERAGE_GATE_RELPATH,
        # Review: code-reviewer 2026-07-08 Finding 5 — thread sid (already available
        # here from emit_receipt's own param) into the receipt body so a read-back
        # site can verify the resolved shard's body-level sid matches the sid it
        # asked for (defense-in-depth on top of Finding 1's filename anchoring).
        sid=sid,
        # op-spec §3 (Option B) — thread the declared-membership list through so
        # BOTH phase-1 (wsc_resolve) and phase-2 (wsc_commit) persist it; phase-2's
        # ctx is rebuilt via PipelineContext.from_dict(), which round-trips this
        # field, so the same line covers both callers.
        applicable_node_ids=ctx.applicable_node_ids,
        # C2 — thread the resolved engine self-version SHA through so both
        # phase-1 and phase-2 receipts carry the SHA of the coordinator_core
        # copy that produced them.  None (unresolvable) is graceful-absent —
        # make_receipt only sets the key when engine_sha is truthy.
        engine_sha=engine_sha,
        # thread the resolved engine dirty-flag through so both phase-1 and
        # phase-2 receipts carry it.  None (unresolvable) is graceful-absent —
        # make_receipt only sets the key when engine_dirty is not None
        # (False is a meaningful, real finding and must still be emitted).
        engine_dirty=engine_dirty,
        # C3 — thread the C1 scoping verdict through so both phase-1 and
        # phase-2 receipts carry it; phase-2's ctx is rebuilt via
        # PipelineContext.from_dict(), which round-trips these fields, so the
        # same line covers both callers (same pattern as applicable_node_ids
        # above).  ctx.scoping_method == "" (pre-init/unset) is graceful-
        # absent — make_receipt only sets the key when scoping_method is
        # truthy.  foreign_commit_count is always threaded (not gated on
        # truthiness) so a genuine 0-foreign-commits verdict is distinguished
        # from "not resolved" via key presence, not via a falsy-0 omission.
        scoping_method=ctx.scoping_method or None,
        foreign_commit_count=(
            ctx.foreign_commit_count if ctx.scoping_method else None
        ),
    )

    # --- atomic write: mkstemp in same dir → os.replace ---
    _atomic_write_json(out_path, receipt)

    return out_path, op_tail


# ---------------------------------------------------------------------------
# _atomic_write_json — internal atomic writer
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write *data* as JSON to *path* atomically via mkstemp + os.replace.

    The temporary file is created in the same directory as *path* so that
    os.replace is a same-filesystem rename (atomic on POSIX).  On any I/O
    failure the temp file is cleaned up and the exception propagates to the
    caller.

    Negative-spec: directory creation is the caller's responsibility — this
    function does NOT call mkdir.
    """
    dir_path = path.parent
    fd, tmp_str = tempfile.mkstemp(
        dir=str(dir_path),
        prefix=f".{path.stem}.tmp.",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_str, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_str)
        raise


# ---------------------------------------------------------------------------
# read_receipt
# ---------------------------------------------------------------------------


def read_receipt(path: Path) -> dict[str, Any]:
    """Read a receipt from disk, returning the sentinel on absence.

    Returns
    -------
    dict
        The parsed receipt dict when the file exists and contains valid JSON.
        NOT_YET_RUN_SENTINEL (a copy) when *path* does not exist.

    Raises
    ------
    ValueError
        When *path* exists but cannot be parsed as JSON (corrupt file).

    Caller contract:
        - Absent file = "pipeline not yet run" — use is_not_yet_run(receipt)
          to test for this condition, NOT a raw dict comparison.
        - A returned dict that is NOT the sentinel is a receipt that was at
          some point written by emit_receipt and is schema-valid JSON.
    """
    if not path.exists():
        return dict(NOT_YET_RUN_SENTINEL)  # return a copy — sentinel is immutable

    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"receipt_emit.read_receipt: corrupt receipt at {path}: {exc}"
        ) from exc
