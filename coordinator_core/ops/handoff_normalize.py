"""
coordinator_core.ops.handoff_normalize — JSON-RPC "handoff.normalize" operation.

Purpose: Port of coordinator/bin/normalize-handoff-frontmatter.js into the
coordinator_core resident service.  Applies six normalizations to active handoff
frontmatter files in state/handoffs/*.md (NEVER archive/handoffs/ — archived files
are immutable history).

Six normalizations (exact port of JS):
  1. created: strip ISO time component; keep bare YYYY-MM-DD
  2. pickup_ready: unquote "true"/"false" string to bare bool
  3. category: backfill when absent via title keyword heuristic (_match_category)
  4. summary: backfill from first H1 in body; truncate to ≤140 chars; strip inline markdown
  5. deliverable_id: carry if present (D1 carry rule); mint dlv-<slug>-<6hex> if absent
  6. initiative: present-as-null when absent (D9 discipline)

All six normalizations append-to-end (NOT anchored insert) — matches the 2-arg
insertFmField in the JS source.  Key order is preserved byte-for-byte outside the
changed lines.

Params:
    write (bool, default False): write changes to disk. False = dry-run only.

Returns a simple dict envelope (NOT the fleet {mode,dry_run,candidate_ids} shape):
    exit_code  (int)  — 0 ok / 1 per-file errors present / 2 indeterminate
    applied    (bool) — True if any files were written to disk
    dry_run    (bool) — True if this was a dry-run (write=False or write absent)
    changed    (list) — [{file: str, changes: [str]}] for files with drift detected
    errors     (list) — [{file: str, error: str}]     for files that failed processing

CONVENTION OF RECORD — the `{applied, dry_run, changed}` envelope (named 2026-07-24).
This module is the reference implementation of the single-file/scoped
frontmatter-mutation envelope, and it is deliberately reusable by citation: a new op
of that family should state that it follows this convention rather than restate a
field shape of its own.  It is distinct from, and intentionally not unifiable with,
the fleet batch-archival wire contract `{mode, dry_run, candidate_ids}`
(`coordinator_core/ops/fleet/_common.py`), which is candidate-oriented and frozen —
see the Negative-spec line below.  Second adopter: `deliverable.spine_backfill`
(`docs/plans/2026-07-19-strang-10-family-d-deliverable-spine-strangle.md` § C1).
Promotion threshold, stated so it is not re-litigated per op: extract a shared
helper or TypedDict only once a THIRD single-file-mutation op needs this exact
shape.  Two consumers sharing a convention by citation is sufficient; two consumers
sharing code is premature — `coordinator_core/ops/` already carries at least five
independently-evolved, deliberately-divergent mutation envelopes, and a generic
`ops/_common.py` envelope module was evaluated against that landscape and rejected.

Self-registration: importing this module fires @register_op("handoff.normalize")
as a side-effect.  Add this module to coordinator_core/ops/__init__.py to trigger
registration at start_server() time.

P9 WORKTREE DERIVATION: _OP_KEY_SCOPE keys this op "common_dir", so repo_root
arrives as <worktree>/.git.  All state/handoffs/ paths are built from
main_worktree_root(repo_root) — NEVER from repo_root directly (which would scan
.git/state/, always empty).

repo_root REQUIRED (op-family path-containment sweep, 2026-07-08 § 1c): the
command-type invoker always supplies --repo for a "common_dir"-scoped op, so
repo_root is None only when the handler is called without a socket-authoritative
common_dir — the handler rejects up front, mirroring
handoff_transition.py/handoff_stamp.py's repo_root-required gate.  The prior
params.root fallback (Path(params_root).resolve() used directly as the SCAN
ROOT for a glob-and-mutate sweep, with no containment check) has been removed —
it was unreachable in production and was the "odd one out" in the containment
sweep (redirects the whole scan root rather than a single file path).

Spec backlink: coordinator/bin/normalize-handoff-frontmatter.js (exact port)
Spec backlinks (deliverable-id / D1 / D9):
  docs/plans/2026-07-03-fleet-deliverable-spine-identity-and-facets.md § C3c
  docs/plans/2026-05-29-handoff-tracker-renderer.md § Chunk 5

Negative-spec (hard-won):
  - Does NOT operate on archive/handoffs/ — archived files are immutable history.
  - Does NOT git-commit.  Pure frontmatter file mutations only.
  - Does NOT re-mint deliverable_id when already present (D1 carry rule: never clobber).
  - Does NOT read ctx.repo_root (None in global service); uses the repo_root arg.
  - Does NOT use the fleet {mode, dry_run, candidate_ids} envelope.
  - Does NOT walk subdirectories of state/handoffs/ — flat glob only (*.md), matching
    the JS walkHandoffsDir flat constraint (example-game-repo legacy archive subdir excluded).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field
from coordinator_core.session.claimed_plan import resolve_claimed_plan_path
from coordinator_core.wire_paths import rel_id

_LOG = logging.getLogger(__name__)

# Sentinel: _normalize_one returns this when a file has no valid YAML frontmatter.
# Distinct from None (which means "already clean — no changes needed").
# Review: code-reviewer (F7) — distinguish no-frontmatter from already-clean so the
# handler can surface skipped files in the errors list rather than silently dropping them.
_NO_FRONTMATTER = object()


# ---------------------------------------------------------------------------
# Deliverable-id helpers (C3c — spine identity threading)
# ---------------------------------------------------------------------------


def _derive_slug_from_path(file_path: Path) -> str:
    """Extract slug from a handoff file path (basename without .md extension).

    Example: Path("state/handoffs/2026-07-03-my-handoff.md") → "2026-07-03-my-handoff"

    Mirrors deriveSlugFromPath() in normalize-handoff-frontmatter.js.
    """
    return file_path.stem


def _mint_deliverable_id_from_slug(slug: str) -> str:
    """Mint a dlv-<slug>-<6hex> deliverable_id.

    Mirrors mintDeliverableIdFromSlug() in normalize-handoff-frontmatter.js:
        entropy = slug|epochMs|pid|random (0–65535)
        hex6    = SHA-1(entropy)[0:6]

    The id is opaque downstream; no consumer parses the suffix.  The entropy
    inputs match the JS original (same field order, same numeric ranges) so
    the distribution and collision probability are equivalent.

    Negative-spec: does NOT implement the carry path (caller's responsibility)
    or the stub path (dlv-<stub_id> — only coordinator-doc-new uses that).
    """
    epoch_ms = int(time.time() * 1000)
    pid = os.getpid()
    rand = random.randint(0, 65535)
    entropy = f"{slug}|{epoch_ms}|{pid}|{rand}"
    hex6 = hashlib.sha1(entropy.encode()).hexdigest()[:6]
    return f"dlv-{slug}-{hex6}"


def _resolve_claimed_plan_deliverable_id(worktree_root: Optional[Path]) -> Optional[str]:
    """Carry the running session's claimed plan's deliverable_id, if resolvable.

    DR-207 DD#1 "second door" close: before minting a fresh deliverable_id from a
    handoff's own filename, check whether this session holds an active plan claim
    and, if so, carry that plan's `deliverable_id` verbatim instead of minting a
    new one — a downstream artifact must never re-mint an identity its parent
    already owns. Uses the same `resolve_claimed_plan_path` two-tier resolver C1
    wires into `resolve_lineage`, so both authoring doors share one resolver.

    Returns None — never raises — when `worktree_root` is unavailable, no plan is
    claimed, or the claimed plan's `deliverable_id` field is absent/empty/literal
    `null`. Every such case is the legitimate "nothing to carry" state; the caller
    falls back to today's unchanged mint-from-slug path in every one of them.

    Negative-spec: does NOT raise on a claimed plan with no deliverable_id
    (unlike deliverable_carry.resolve_deliverable_and_initiative's
    DroppedDeliverableJoinError guard) — that fail-loud discipline is scoped to
    the /handoff authoring cascade (C1's resolve_lineage), not this backfill
    normalizer, which must keep degrading to mint-from-slug exactly as it always
    has whenever nothing is carryable.
    """
    if worktree_root is None:
        return None
    plan_rel = resolve_claimed_plan_path(worktree_root)
    if not plan_rel:
        return None
    plan_path = worktree_root / plan_rel
    deliverable_id = read_frontmatter_field(str(plan_path), "deliverable_id")
    return deliverable_id or None


# ---------------------------------------------------------------------------
# Category heuristic (exact port of matchCategory from JS)
# ---------------------------------------------------------------------------


def _match_category(title: str) -> str:
    """Best-effort keyword match on a handoff title.

    Returns one of the schema-valid category enum values, or 'uncategorized'
    when no keyword fires.

    HARD CONSTRAINT: every return value MUST be a member of the category enum in
    schemas/handoff.yaml — [roadmap, infra, bug, docs, research, refactor, uncategorized].

    Intentionally conservative — only unambiguous category signals are mapped;
    kind/lifecycle words (spinoff, recovery, review, release) are NOT categories
    and fall through to 'uncategorized', the safe default.

    Exact port of matchCategory() from normalize-handoff-frontmatter.js.
    """
    t = (title or '').lower()
    if re.search(r'\broadmap\b|\bsprint\b', t):
        return 'roadmap'
    if re.search(r'\brefactor\b|\bcleanup\b|\bconsolidat', t):
        return 'refactor'
    if re.search(r'\bbug\b|\bhotfix\b|\bregression\b', t):
        return 'bug'
    if re.search(r'\bresearch\b|\bspike\b|\binvestigat', t):
        return 'research'
    if re.search(r'\bdoc(?:s|umentation)?\b|\bwiki\b', t):
        return 'docs'
    if re.search(r'\binfra(?:structure)?\b|\binstall\b|\bhook\b|\bpipeline\b|\bci\b', t):
        return 'infra'
    return 'uncategorized'


# ---------------------------------------------------------------------------
# Per-file normalizer
# ---------------------------------------------------------------------------


def _normalize_one_text(
    content: str, file_path: Path, carried_deliverable_id: Optional[str] = None
) -> Optional[Dict]:
    """Compute the normalized content for a single handoff file (pure — caller provides content).

    Returns {'rebuilt': str, 'changes': [str]} when drift is detected, or None
    when the file is already clean (idempotent: second run always returns None).
    Returns the _NO_FRONTMATTER sentinel when content has no valid YAML frontmatter block.

    Extracted from _normalize_one so that callers supplying pre-read content (e.g.
    inside a locked_rmw mutate closure) can invoke the six normalizations without a
    second disk read.

    `carried_deliverable_id` (optional) is the ALREADY-RESOLVED result of
    `_resolve_claimed_plan_deliverable_id` (DR-207 DD#1 second door) — the caller
    resolves it once per invocation (not per file) and passes the value straight
    through, since a session's claimed plan is invariant for the whole call. Every
    caller — `handoff.normalize`'s own handler, `handoff_author_fork.py`, and
    `queue_scaffold_baton.py` — resolves this once in its own enclosing scope and
    passes the result here; a caller that omits it (or has no worktree_root to
    resolve from) keeps the prior mint-from-slug-only behaviour unchanged.

    Exact port of normalizeOne() from normalize-handoff-frontmatter.js.
    Only the six listed fields are touched — key order and all other frontmatter
    content are preserved byte-for-byte outside the mutated lines.

    Negative-spec:
      - Does NOT perform I/O — caller is responsible for read and write.
      - Does NOT resolve the claimed plan itself — caller resolves once and passes
        the value in, avoiding a per-file re-resolution in a batch caller.
      - Returns _NO_FRONTMATTER (not None) when frontmatter block is absent.
    """
    split = split_frontmatter(content)
    if split is None:
        # Review: code-reviewer (F7) — return sentinel instead of None so the handler can
        # surface this file in errors (IPC envelope has structured errors; silent drop is
        # the wrong observability contract unlike the JS CLI which prints to stdout).
        return _NO_FRONTMATTER

    fm_text = split.fm_text
    changes: List[str] = []

    # ── 1. created: strip ISO time component ──────────────────────────────
    # Matches `2026-05-28T11:16:48Z` or `2026-05-28T11:16:48` etc.
    created_raw = read_fm_field(fm_text, 'created')
    if created_raw:
        iso_match = re.match(r'^(\d{4}-\d{2}-\d{2})[T ]', created_raw)
        if iso_match:
            bare = iso_match.group(1)
            fm_text = replace_fm_field(fm_text, 'created', bare)
            changes.append(f'created: "{created_raw}" → "{bare}"')

    # ── 2. pickup_ready: unquote boolean strings ───────────────────────────
    # read_fm_field returns raw text including any YAML quote characters, so
    # `pickup_ready: "true"` yields raw value `"true"` (with double-quote chars).
    pickup_raw = read_fm_field(fm_text, 'pickup_ready')
    if pickup_raw is not None:
        if pickup_raw in ('"true"', "'true'"):
            fm_text = replace_fm_field(fm_text, 'pickup_ready', 'true')
            changes.append(f'pickup_ready: {pickup_raw} → true')
        elif pickup_raw in ('"false"', "'false'"):
            fm_text = replace_fm_field(fm_text, 'pickup_ready', 'false')
            changes.append(f'pickup_ready: {pickup_raw} → false')
        # bare `true`/`false` already correct — no-op

    # ── 3. category: backfill when absent ─────────────────────────────────
    category_raw = read_fm_field(fm_text, 'category')
    if category_raw is None:
        title = read_fm_field(fm_text, 'title') or ''
        cat = _match_category(title)
        fm_text = insert_fm_field(fm_text, 'category', cat)
        changes.append(f'category: (absent) → {cat}')

    # ── 4. summary: backfill when absent ──────────────────────────────────
    summary_raw = read_fm_field(fm_text, 'summary')
    if summary_raw is None:
        # Extract text from the first H1 in the body; fall back to `title:` field.
        summary_text = ''
        h1_match = re.search(r'^#\s+(.+)$', split.body_with_leading_newline, re.MULTILINE)
        if h1_match:
            summary_text = h1_match.group(1).strip()
        else:
            summary_text = read_fm_field(fm_text, 'title') or ''
        # Strip inline markdown (bold, code, links) — mirrors JS strip chain.
        summary_text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', summary_text)   # [text](url) → text
        summary_text = re.sub(r'`([^`]+)`', r'\1', summary_text)               # `code` → code
        summary_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', summary_text)         # **bold** → bold
        summary_text = re.sub(r'\*([^*]+)\*', r'\1', summary_text)             # *italic* → italic
        summary_text = summary_text.strip()
        if len(summary_text) > 140:
            summary_text = summary_text[:137] + '...'
        if summary_text:
            fm_text = insert_fm_field(fm_text, 'summary', summary_text)
            short = summary_text[:60]
            ellipsis = '…' if len(summary_text) > 60 else ''
            changes.append(f'summary: (absent) → "{short}{ellipsis}"')

    # ── 5. deliverable_id: carry if present; mint if absent (D1 carry rule) ──
    # Carry rule (D1): never re-mint an id that already exists — carrying preserves
    # the join key across downstream artifacts.  Mint rule: absent → dlv-<slug>-<6hex>.
    # The carry path emits NO changes entry (not drift — identity preservation).
    #
    # DR-207 DD#1 "second door" close: an absent field on THIS handoff does not
    # mean no parent id is discoverable — this session's claimed plan may already
    # own one.  Check that BEFORE falling back to mint-from-slug (C2).
    deliverable_id_raw = read_fm_field(fm_text, 'deliverable_id')
    if deliverable_id_raw is None:
        if carried_deliverable_id:
            fm_text = insert_fm_field(fm_text, 'deliverable_id', carried_deliverable_id)
            changes.append(
                f'deliverable_id: (absent) → {carried_deliverable_id} [carry-from-claimed-plan]'
            )
            _LOG.info(
                "handoff.normalize: carry path — carried deliverable_id %s from claimed plan for %s",
                carried_deliverable_id, file_path,
            )
        else:
            slug = _derive_slug_from_path(file_path)
            minted = _mint_deliverable_id_from_slug(slug)
            fm_text = insert_fm_field(fm_text, 'deliverable_id', minted)
            changes.append(f'deliverable_id: (absent) → {minted} [mint-from-slug]')
            _LOG.info(
                "handoff.normalize: mint-from-slug path — minted deliverable_id %s for %s",
                minted, file_path,
            )
    # If present: carry unchanged — not a drift condition; no changes entry emitted.

    # ── 6. initiative: present-as-null when absent (D9 discipline) ────────────
    # D9: key-present-carrying-null (not key-absent) so rag/tc-5 inserts a typed
    # null in every column without absent-vs-null ambiguity.  null = not yet assigned.
    initiative_raw = read_fm_field(fm_text, 'initiative')
    if initiative_raw is None:
        fm_text = insert_fm_field(fm_text, 'initiative', None)
        changes.append('initiative: (absent) → null')
    # If present (including the literal "null" value): carry — no drift.

    if not changes:
        return None  # already clean — idempotent

    rebuilt = rebuild(split, fm_text)
    return {'rebuilt': rebuilt, 'changes': changes}


def _normalize_one(file_path: Path, carried_deliverable_id: Optional[str] = None) -> Optional[Dict]:
    """Read file and compute its normalized content.

    Thin wrapper around _normalize_one_text: reads the file and delegates all
    normalization logic to the pure helper.  Used by the dry-run path (write=False)
    where no lock is needed.

    `carried_deliverable_id` is threaded straight through to `_normalize_one_text`
    — see its docstring for the deliverable_id carry-from-claimed-plan check it
    enables, and for why it is a pre-resolved value rather than a worktree_root.

    Returns {'rebuilt': str, 'changes': [str]} when drift is detected, None when
    already clean, or _NO_FRONTMATTER when the file lacks a valid frontmatter block.
    Raises OSError on I/O failure — caller logs and appends to errors list.
    """
    content = file_path.read_text(encoding="utf-8")
    return _normalize_one_text(content, file_path, carried_deliverable_id)


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("handoff.normalize")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "handoff.normalize" handler.

    Port of coordinator/bin/normalize-handoff-frontmatter.js.  Applies six
    normalizations to all active handoff files in state/handoffs/*.md.

    Params:
        write (bool, default False): write changes to disk.  False = dry-run.

    Returns:
        exit_code  (int)  — 0 ok / 1 per-file errors present / 2 indeterminate
        applied    (bool) — True if any files were written to disk
        dry_run    (bool) — True if this was a dry-run
        changed    (list) — [{file: str, changes: [str]}] files with drift
        errors     (list) — [{file: str, error: str}]     files that failed

    Negative-spec:
      - Does NOT commit.  Pure in-place frontmatter file writes only.
      - Does NOT read ctx.repo_root (None in global service); uses repo_root arg.
      - Does NOT glob subdirectories of state/handoffs/ — flat *.md only.
      - Does NOT operate on archive/handoffs/ — archived files are immutable.
      - Does NOT accept a params.root scan-root override (op-family path-containment
        sweep, 2026-07-08 § 1c) — repo_root is required; a caller-supplied scan root
        with no containment check is exactly the gap this rejects.
    """
    write: bool = bool(params.get("write", False))

    # P9: repo_root arrives as <worktree>/.git (common_dir); main_worktree_root = common_dir.parent.
    # repo_root is required (op-family path-containment sweep, 2026-07-08 § 1c) — mirrors
    # handoff_transition.py / handoff_stamp.py's repo_root-required gate.  "common_dir" key
    # scope guarantees the command-type invoker always supplies --repo, so this is not
    # reachable in production; it exists as a fail-loud contract for direct-call callers.
    if repo_root is None:
        return {
            "exit_code": 2,
            "applied": False,
            "dry_run": not write,
            "changed": [],
            "errors": [],
            "error": (
                "handoff.normalize: repo_root is required "
                "(no founding root available — handler called without socket-authoritative common_dir)"
            ),
        }

    worktree_root = main_worktree_root(repo_root)

    handoffs_dir = worktree_root / "state" / "handoffs"

    if not handoffs_dir.is_dir():
        # No handoffs dir — not a fatal error; return clean empty result.
        _LOG.debug("handoff.normalize: state/handoffs not found at %s — nothing to do", handoffs_dir)
        return {
            "exit_code": 0,
            "applied": False,
            "dry_run": not write,
            "changed": [],
            "errors": [],
        }

    changed: List[dict] = []
    errors: List[dict] = []

    # Review: code-reviewer (F1) — resolved ONCE per invocation, not per file: the
    # claimed plan (and its deliverable_id) is invariant for the whole batch, so
    # re-resolving it inside the per-file loop was a real (if bounded) I/O cost
    # across a ~141-file corpus.  Threaded straight into every call site below.
    carried_deliverable_id = _resolve_claimed_plan_deliverable_id(worktree_root)

    # Flat glob — no subdirectory recursion (mirrors walkHandoffsDir constraint in JS).
    for file_path in sorted(handoffs_dir.glob("*.md")):
        rel = rel_id(file_path, worktree_root)

        if write:
            # Write path: use locked_rmw for an atomic flock-protected RMW cycle (C1).
            # asyncio.to_thread offloads the blocking I/O + flock off the event loop
            # (DR-212 D3).  N independent locks on disjoint files — no batch lock.
            #
            # Closure box captures the normalization result so the caller can append
            # to changed[] after the locked write completes.  The idempotent (already
            # clean) path returns old_text unchanged; locked_rmw sees byte-identical
            # content and skips the write entirely (no mtime churn).
            _norm_box: list = [None]

            def _mutate(old_text: str, _fp: Path = file_path) -> str:
                norm_result = _normalize_one_text(old_text, _fp, carried_deliverable_id)
                _norm_box[0] = norm_result
                if norm_result is None:
                    return old_text  # already clean; byte-identical → no write
                if norm_result is _NO_FRONTMATTER:
                    raise MutateAbort(
                        f"no valid YAML frontmatter block — skipped (immutable)"
                    )
                return norm_result["rebuilt"]

            try:
                await asyncio.to_thread(
                    locked_rmw, file_path, _mutate, repo_root=repo_root or worktree_root
                )
            except LockTimeout as exc:
                _LOG.warning("handoff.normalize: lock timeout on %s: %s", rel, exc)
                errors.append({"file": rel, "error": f"lock timeout: {exc}"})
                continue
            except MutateAbort as exc:
                msg = str(exc.args[0]) if exc.args else "mutate aborted"
                _LOG.warning("handoff.normalize: %s — %s", rel, msg)
                errors.append({"file": rel, "error": msg})
                continue
            except OSError as exc:
                _LOG.warning("handoff.normalize: I/O error on %s: %s", rel, exc)
                errors.append({"file": rel, "error": f"I/O error: {exc}"})
                continue

            norm_result = _norm_box[0]
            if norm_result is None:
                continue  # already clean — no changes to record
            changed.append({"file": rel, "changes": norm_result["changes"]})

        else:
            # Dry-run path: read-only, no lock needed.
            # Review: code-reviewer (F1) — asyncio.to_thread wraps _normalize_one (which
            # calls read_text) to satisfy DR-212 D3 async-loop mandate; prevents event-loop
            # stall under the batch-normalize path which may read N files in one call.
            try:
                result = await asyncio.to_thread(_normalize_one, file_path, carried_deliverable_id)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("handoff.normalize: error processing %s: %s", rel, exc)
                errors.append({"file": rel, "error": str(exc)})
                continue

            # Review: code-reviewer (F7) — surface no-frontmatter files in errors rather
            # than dropping them silently.
            if result is _NO_FRONTMATTER:
                _LOG.warning("handoff.normalize: no valid YAML frontmatter in %s — skipped", rel)
                errors.append({"file": rel, "error": "no valid YAML frontmatter block — skipped (immutable)"})
                continue

            if result is None:
                continue  # already clean

            changed.append({"file": rel, "changes": result["changes"]})

    applied = write and len(changed) > 0
    exit_code = 1 if errors else 0

    return {
        "exit_code": exit_code,
        "applied": applied,
        "dry_run": not write,
        "changed": changed,
        "errors": errors,
    }
