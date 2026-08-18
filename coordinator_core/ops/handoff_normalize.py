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
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from coordinator_core.frontmatter.baton_class import (
    canonical_kind,
    kind_values_for_canonical,
)
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    read_fm_nested_field,
    rebuild,
    replace_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
    unquote_yaml_scalar,
    write_fm_nested_field,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field
from coordinator_core.session.claimed_plan import resolve_claimed_plan_path
from coordinator_core.session.core import resolve_session_id
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


#: Kinds excluded from the batch-sweep carry (AC6, 2026-08-18 spinoff-deliverable-id
#: plan, C4). Sourced from `baton_class.py` — the repo's single owning table for
#: `kind` membership — via the same `kind_values_for_canonical()` helper C2
#: (`deliverable_cascade.py`) and C3 (`handoff_transition.py`) use for this identical
#: policy, matching `deliverable_carry._ROADMAP_STUB_KINDS`'s own sourcing shape. A
#: `kind: spinoff` record must remain the sole bearer of its own `deliverable_id` —
#: see the plan's "Where the wrong id actually comes from" section — so it is never
#: entitled to a claimed plan's carried value, independent of `claimed_by`.
_CARRY_EXCLUDED_KINDS = frozenset(kind_values_for_canonical("spinoff"))


def _carry_value_for_file(
    content: str, carried_deliverable_id: Optional[str], session_id: str
) -> Optional[str]:
    """Narrow the batch-invariant carried_deliverable_id to THIS file only.

    Break-class fix (measured 2026-08-12, see
    archive/specs/2026-08/2026-08-01-deliverable-id-carry-onto-executing-handoff.md):
    `_resolve_claimed_plan_deliverable_id` is resolved ONCE per `handoff.normalize`
    invocation (I/O fix, correct) but the *executing* handoff's carried value must
    NOT be applied uniformly to every file the batch sweep touches — only the file
    this session is itself the `claimed_by` holder of may receive it. Every other
    key-absent file (unclaimed, or claimed by a different session) must see `None`
    so it falls through to the mint-from-slug path instead of being silently
    stamped with an unrelated session's claimed-plan identity.

    Second break-class fix (AC6, 2026-08-18): a `kind: spinoff` record must never
    receive a claimed plan's `deliverable_id` through this door either, even when
    `claimed_by` matches — a spinoff is not the same deliverable as the plan that
    spun it off (see the plan's "Why minting fresh does not violate carry-not-remint"
    section), and the caller's own key-absent gate (`_normalize_one_text` step 5)
    is the one live producer of the inherited-id defect this closes.

    Returns `carried_deliverable_id` iff `session_id` is non-empty, this file's
    frontmatter `claimed_by` equals `session_id`, AND this file's `kind` is not in
    `_CARRY_EXCLUDED_KINDS`. Returns `None` in every other case, INCLUDING when
    `session_id` is unresolvable (`""`) — fail toward not-stamping, never toward
    stamping.

    Negative-spec:
      - Does NOT change `_normalize_one_text`'s own carry-vs-mint branch — that
        function's caller-supplied-parameter contract is unchanged and still
        shared byte-for-byte with `handoff_author_fork.py` /
        `queue_scaffold_baton.py`, which legitimately carry unconditionally
        because they are authoring THIS session's own new artifact.
      - Does NOT touch any kind other than the ones `_CARRY_EXCLUDED_KINDS`
        names — every other kind's carry behaviour is unchanged (AC6).
    """
    if not carried_deliverable_id or not session_id:
        return None
    split = split_frontmatter(content)
    if split is None:
        return None
    claimed_by = read_fm_field(split.fm_text, "claimed_by")
    if claimed_by != session_id:
        return None
    # Canonicalize before the membership test, matching `deliverable_cascade`'s
    # leg (d) and `handoff_transition`'s backstop exactly. A raw compare agrees
    # with those two only while `spinoff` has no pre-rename alias; the moment
    # one is added to `baton_class._PRE_RENAME_ALIASES`, a raw compare would
    # let the aliased spelling through this door while the other two refuse it.
    kind = canonical_kind(read_fm_field_unquoted(split.fm_text, "kind"))
    if kind in _CARRY_EXCLUDED_KINDS:
        return None
    return carried_deliverable_id


# ---------------------------------------------------------------------------
# Summary cap (ported from memo_transition._normalize_oversize_summary /
# _normalize_block_scalar_summary — 2026-08-13, unwritable-handoff-records)
# ---------------------------------------------------------------------------

# Handoff `summary:` cap — matches schema_validate._cf_summary_length_cap's
# literal 140.  Distinct constant from memo_transition's
# `ops.fleet._memo_summary._SUMMARY_MAX_CHARS` (120) — the two gates cap
# different record kinds at different limits; this module owns its own value
# rather than importing the memo one, which would silently couple the two
# caps.
_SUMMARY_MAX_CHARS = 140

# Matches the bare block-scalar indicator token read_fm_field returns for a
# `summary: |` / `summary: >` line (optionally with chomping `+`/`-` and/or an
# explicit indentation-indicator digit, e.g. `|-`, `>+`, `|2`, `|2-`) — never a
# real single-line value.  Based on memo_transition.py's own
# `_BLOCK_SCALAR_INDICATOR_RE` (same shape, different module — no shared
# import to avoid an ops-module-to-ops-module dependency), widened here to
# also match a legal trailing `# comment` on the indicator line itself.
# Review: code-reviewer (P3) — `read_fm_field` returns the indicator line
# verbatim (only outer whitespace trimmed, comment NOT stripped), so
# `summary: |-  # comment` used to fail this anchored-to-end-of-string match,
# fall through to the plain-scalar `else` branch, and be measured/truncated
# as the literal 13-character string `|-  # comment` instead of being
# recognized as a block scalar at all.
_BLOCK_SCALAR_INDICATOR_RE = re.compile(r'^[|>][+\-0-9]*(?:[ \t]+#.*)?$')

# Placeholder-shaped `summary:` values a scaffolder emits when the operator
# never hand-edits the field. A placeholder is PRESENT (not absent) and under
# the 140-char cap, so the pre-existing "backfill only when absent" branch
# below silently carried it forever — the record ends up committed to
# `state/` with the literal placeholder text as its summary. Treating a
# literal match as absent routes it through the existing H1-derivation
# backfill instead of inventing a second normalization path.
#
# Literal is duplicated (not imported) from
# `coordinator/bin/coordinator-doc-new.py::_scaffold_spinoff`'s
# `placeholder_summary` local by necessity, not convenience: `coordinator/bin/`
# scripts import FROM `coordinator_core` (the engine), never the reverse — see
# `coordinator_core/data_root.py`'s docstring on the same asymmetry for
# `coordinator/bin/lib/`. `coordinator-doc-new.py` is also outside this
# module's writable scope for this change (docs/plans/2026-08-14-placeholder-
# summaries-and-the-drift-guards-uncounted-callers.md § C1 `writes:`), so
# hoisting a shared constant either direction is not available at this call
# site; if a scaffolder ever adds a second placeholder-summary kind, add its
# literal here too rather than generalizing to a "looks unfilled" heuristic
# (see the plan's Anti-scope).
_PLACEHOLDER_SUMMARIES = frozenset(
    {
        "PLACEHOLDER — replace with one-line spinoff summary (≤140 chars)",
    }
)


def _replace_block_scalar_span(fm_text: str, key: str, new_line: str) -> Optional[str]:
    """Replace a block-scalar ``key:`` line PLUS all its indented continuation lines.

    Exact port of `memo_transition._replace_block_scalar_span` — locates the span
    from the ``key: |``/``key: >`` line through the last contiguous line that is
    either blank or indented (YAML requires block-scalar continuation lines to be
    indented relative to the key, which at frontmatter top level is column 0 — so
    "indented or blank" is exactly the continuation-line test).  Returns ``None``
    if the span cannot be located (defensive — should not happen given the caller
    already confirmed ``read_fm_field`` saw a block-scalar indicator for this key).
    """
    text = fm_text if fm_text.endswith('\n') else fm_text + '\n'
    pattern = re.compile(
        # Review: code-reviewer (P3) — the indicator line may carry a legal
        # trailing `# comment` (e.g. `summary: |-  # note`); the optional
        # `(?:#.*)?` tail mirrors the widened `_BLOCK_SCALAR_INDICATOR_RE`
        # match above so a comment-bearing indicator line is still located
        # (and replaced — including its comment) rather than silently
        # leaving the span unfound.
        r'^' + re.escape(key) + r':(?=[ \t]|\r?$)[ \t]*[|>][+\-0-9]*[ \t]*(?:#.*)?\r?\n'
        r'(?:(?:[ \t]+.*)?\r?\n)*',
        re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return None
    if m.group(0).partition('\n')[0].endswith('\r') and not new_line.endswith('\r\n'):
        new_line = new_line[:-1] + '\r\n' if new_line.endswith('\n') else new_line + '\r\n'
    return text[: m.start()] + new_line + text[m.end():]


def _normalize_block_scalar_summary(
    fm_text: str, file_path: Path, *, label: str = "handoff.normalize"
) -> str:
    """Truncate an over-cap block-scalar ``summary: |`` / ``summary: >`` field.

    ``label`` names the calling seam in the stderr warning so a reader can tell a
    ``handoff.normalize`` sweep from a ``handoff.transition`` claim-time truncation;
    it changes nothing else.

    Exact port of `memo_transition._normalize_block_scalar_summary`, capped to
    this module's `_SUMMARY_MAX_CHARS` (140, not the memo side's 120).  Decodes
    the full value via ``yaml.safe_load`` (the frontmatter text is a valid flow
    mapping), flattens embedded newlines to spaces, truncates using the same
    ``value[:CAP - 1] + "…"`` shape the plain-scalar path uses, and splices the
    entire key-line-plus-continuation-block span with one quoted single-line
    ``summary: "…"`` line.

    Length gate mirrors ``schema_validate._cf_summary_length_cap`` exactly — that
    validator measures ``len(str(summary))`` on the yaml.safe_load-decoded value
    (embedded newlines counted as 1 char each, not flattened), so this helper
    gates on the same decoded length before deciding to act.

    Idempotent: an at-or-under-cap block scalar (decoded length) is left
    byte-identical, no warning emitted.

    Negative-spec: does NOT run when ``yaml.safe_load`` fails to parse ``fm_text``
    — an unparseable frontmatter is left untouched.
    """
    try:
        parsed = yaml.safe_load(fm_text) or {}
    except Exception:  # noqa: BLE001
        return fm_text

    value = parsed.get("summary")
    if value is None:
        return fm_text
    value = str(value)
    if len(value) <= _SUMMARY_MAX_CHARS:
        return fm_text

    original_len = len(value)
    flattened = " ".join(value.split())
    truncated = flattened[: _SUMMARY_MAX_CHARS - 1] + "…"
    print(
        f"{label}: WARNING — {file_path}: summary: (block scalar) exceeded "
        f"{_SUMMARY_MAX_CHARS} chars (was {original_len}); flattened and truncated to fit the cap",
        file=sys.stderr,
    )
    new_line = f"summary: {serialize_yaml_scalar(truncated, numeric_quoting=True)}\n"
    replaced = _replace_block_scalar_span(fm_text, "summary", new_line)
    return replaced if replaced is not None else fm_text


def normalize_present_summary(
    fm_text: str, file_path: Path, *, label: str = "handoff.normalize"
) -> tuple[str, Optional[str]]:
    """Truncate a PRESENT, over-cap ``summary:`` to the 140-char cap, ahead of any gate.

    The one shared implementation of the handoff-side summary-cap normalization,
    used by both writer seams that need it: ``_normalize_one_text``'s step 4 and
    ``handoff_transition._claim``'s pre-validation pass. Extracted rather than
    re-inlined because the 2026-08-13 plan's anti-scope names a third truncation
    variant as the failure mode ("Do not hand-roll a second truncation shape").

    Handles both on-disk shapes: a block-scalar ``summary: |`` / ``summary: >``
    (routed to ``_normalize_block_scalar_summary``) and a plain scalar, which is
    measured on the ``yaml.safe_load``-decoded value — the same value
    ``schema_validate._cf_summary_length_cap`` measures — and rewritten as
    ``value[:_SUMMARY_MAX_CHARS - 1] + "…"``. The trailing ``…`` is the visible
    marker that a reader can tell truncation happened; it matches
    ``memo_transition._normalize_oversize_summary`` byte-for-byte.

    Returns ``(fm_text, change)``: ``change`` is a human-readable drift line when a
    truncation occurred, ``None`` when nothing was touched. Idempotent — an
    at-or-under-cap or absent ``summary:`` returns the input byte-identical with
    ``None``, so a second pass emits neither drift nor a warning.

    Negative-spec: does NOT backfill an ABSENT ``summary:`` (that is
    ``_normalize_one_text``'s H1-derivation branch, which is a normalize-sweep
    concern, not a gate-adjacent one). Does NOT relax
    ``schema_validate._cf_summary_length_cap`` or any other cross-field rule —
    every caller runs this AHEAD of the gate and the gate stays strict.
    """
    summary_raw = read_fm_field(fm_text, 'summary')
    if summary_raw is None:
        return fm_text, None

    if _BLOCK_SCALAR_INDICATOR_RE.match(summary_raw):
        new_fm_text = _normalize_block_scalar_summary(fm_text, file_path, label=label)
        if new_fm_text == fm_text:
            return fm_text, None
        return new_fm_text, 'summary: (block scalar) truncated to fit 140-char cap'

    # Review: code-reviewer (P1, break-class) — measure the SAME decoded value
    # `schema_validate._cf_summary_length_cap` measures (`len(str(summary))` on
    # the `yaml.safe_load`-decoded value), not the raw on-disk text.
    # `unquote_yaml_scalar(read_fm_field(...))` diverges from that decoded value
    # in two ways: it does not strip a trailing `# comment` (only
    # `read_fm_field_unquoted` does that), so a comment tail was counted toward —
    # and could be sliced into — the measured/truncated value; and its own
    # negative-spec says it does not process double-quoted backslash escapes, so
    # a `\n`/`\"` sequence is measured at its longer raw-literal length rather
    # than its decoded length. Either divergence could falsely trip (or miss) the
    # >140 branch, or slice mid-escape. `yaml.safe_load` — the same decode the
    # sibling block-scalar path already uses — is the one source of truth for
    # what the gate sees.
    try:
        parsed_summary = yaml.safe_load(fm_text).get('summary')
    except Exception:  # noqa: BLE001
        parsed_summary = None
    summary_val = None if parsed_summary is None else str(parsed_summary)
    if summary_val is None or len(summary_val) <= _SUMMARY_MAX_CHARS:
        return fm_text, None

    original_len = len(summary_val)
    truncated = summary_val[: _SUMMARY_MAX_CHARS - 1] + '…'
    print(
        f"{label}: WARNING — {file_path}: summary: exceeded "
        f"{_SUMMARY_MAX_CHARS} chars (was {original_len}); truncated to fit the cap",
        file=sys.stderr,
    )
    return (
        replace_fm_field(fm_text, 'summary', truncated, numeric_quoting=True),
        f'summary: truncated (was {original_len} chars) to fit {_SUMMARY_MAX_CHARS}-char cap',
    )


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
    content: str,
    file_path: Path,
    carried_deliverable_id: Optional[str] = None,
    minted_by: Optional[str] = None,
    producer: Optional[dict] = None,
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

    `producer` (optional) is the ALREADY-RESOLVED result of
    `coordinator_core.session.producer_resolve.resolve_producer_for_creation`
    (producer-axis-claude-klabauter-engine-half) — same caller-supplied-parameter
    discipline as `carried_deliverable_id`/`minted_by`: only the two creation
    doors resolve and pass it; the batch sweep passes nothing, and this
    function never backfills an absent `producer` onto an existing record.

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

    # ── 4. summary: backfill when absent; cap when present ─────────────────
    # Break-class fix (2026-08-13, unwritable-handoff-records-fail-loudly § C1):
    # the >140 truncation used to live ONLY inside the absence branch below, so
    # a present, already-over-cap summary fell straight through untouched and
    # was then refused forever by schema_validate._cf_summary_length_cap on
    # every subsequent write.  Ported from memo_transition._normalize_oversize_
    # summary / _normalize_block_scalar_summary (see the module-level helpers
    # above): truncate ahead of the gate, warn, never reject.  Truncation shape
    # unified to `[:_SUMMARY_MAX_CHARS - 1] + "…"` (the memo side's shape) for
    # both this present-value path and the backfill path below — the prior
    # `[:137] + '...'` three-ASCII-dot shape is retired.
    summary_raw = read_fm_field(fm_text, 'summary')
    summary_is_placeholder = (
        summary_raw is not None
        and unquote_yaml_scalar(summary_raw) in _PLACEHOLDER_SUMMARIES
    )
    if summary_raw is None or summary_is_placeholder:
        # Extract text from the first H1 in the body; fall back to `title:` field.
        summary_text = ''
        h1_match = re.search(r'^#\s+(.+)$', split.body_with_leading_newline, re.MULTILINE)
        if h1_match:
            summary_text = h1_match.group(1).strip()
        else:
            # Latent-bug fix (in-scope, this branch): `read_fm_field` returns the
            # value VERBATIM including any YAML quote characters — correct for a
            # presence-test or verbatim-rewrite caller, wrong here since this value
            # is composed into a NEW `summary:` field. A quoted `title:` (every
            # `coordinator-doc-new`-scaffolded record — see `_yaml_quote(title)`
            # in `_scaffold_spinoff`) used to backfill `summary` with the literal
            # quote characters still embedded. `read_fm_field_unquoted` is the
            # documented comparison/rewrite-safe sibling; see its docstring.
            summary_text = read_fm_field_unquoted(fm_text, 'title') or ''
        # Strip inline markdown (bold, code, links) — mirrors JS strip chain.
        summary_text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', summary_text)   # [text](url) → text
        summary_text = re.sub(r'`([^`]+)`', r'\1', summary_text)               # `code` → code
        summary_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', summary_text)         # **bold** → bold
        summary_text = re.sub(r'\*([^*]+)\*', r'\1', summary_text)             # *italic* → italic
        summary_text = summary_text.strip()
        if len(summary_text) > _SUMMARY_MAX_CHARS:
            summary_text = summary_text[: _SUMMARY_MAX_CHARS - 1] + '…'
        if summary_text:
            if summary_is_placeholder:
                fm_text = replace_fm_field(fm_text, 'summary', summary_text)
            else:
                fm_text = insert_fm_field(fm_text, 'summary', summary_text)
            short = summary_text[:60]
            ellipsis = '…' if len(summary_text) > 60 else ''
            origin = '(placeholder)' if summary_is_placeholder else '(absent)'
            changes.append(f'summary: {origin} → "{short}{ellipsis}"')
    else:
        # Present value (plain or block scalar): the shared cap normalizer, which
        # `handoff_transition._claim` also calls ahead of its own validation gate.
        # At-or-under-cap present value: carry unchanged — no drift, no warning.
        fm_text, summary_change = normalize_present_summary(fm_text, file_path)
        if summary_change is not None:
            changes.append(summary_change)

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

    # ── 7. minted_by: stamp the CALLER-SUPPLIED github alias at creation ──────
    # C5 (person-identity-primitive-first-slice): value is the casefolded
    # `github` alias from resolve_operating_person's bundle (§ The `minted_by`
    # representation decision) — NOT a person_id UUID, independent of C4's
    # registry by design. Unresolvable identity omits `minted_by` ENTIRELY — no
    # null, no "unknown" sentinel (DEC-41, extended).
    #
    # CALLER-SUPPLIED, never read from session state here. This function is
    # reached by BOTH the creation doors (handoff_author_fork /
    # queue_scaffold_baton, authoring THIS session's own new artifact) and the
    # `handoff.normalize` batch sweep, which walks every file in handoffs_dir.
    # Resolving the operating human inside this function would stamp the
    # SWEEPING operator's handle onto every handoff lacking the key — and since
    # `minted_by` is new, that is the entire corpus, not the handful a partially
    # populated field would expose. That is the same break-class defect fixed
    # for `carried_deliverable_id` on 2026-08-12; see `_carry_value_for_file`'s
    # negative-spec, which names this contract explicitly. Sweep callers pass
    # nothing and stamp nothing.
    if minted_by and read_fm_field(fm_text, 'minted_by') is None:
        fm_text = insert_fm_field(fm_text, 'minted_by', minted_by)
        changes.append(f'minted_by: (absent) → {minted_by}')
    # If present: carry unchanged — not a drift condition; no changes entry.

    # ── 8. producer: stamp the CALLER-SUPPLIED creation-seam record ──────────
    # producer-axis-claude-klabauter-engine-half: {typed_command, op_identity} resolved
    # ONCE per creation call via `coordinator_core.session.producer_resolve
    # .resolve_producer_for_creation` and handed in here — same caller-supplied
    # discipline as `minted_by` / `carried_deliverable_id` above, and for the
    # identical reason: this function is reached by BOTH the creation doors
    # (handoff_author_fork.py / queue_scaffold_baton.py, authoring THIS
    # session's own new artifact, each resolving its own distinct op_identity
    # at its own call site) and the `handoff.normalize` batch sweep, which
    # walks every pre-existing file in handoffs_dir. The sweep passes no
    # `producer` and this function never infers one for an existing record —
    # back-stamping the corpus with the sweeping session's value is the exact
    # defect fixed for `carried_deliverable_id` at 60e17407c (see
    # `_carry_value_for_file`'s docstring) and this axis must not re-find it.
    if producer is not None and read_fm_nested_field(fm_text, 'producer') is None:
        block_lines = [
            f"  op_identity: {serialize_yaml_scalar(producer.get('op_identity'))}",
            f"  typed_command: {serialize_yaml_scalar(producer.get('typed_command'))}",
        ]
        fm_text = write_fm_nested_field(fm_text, 'producer', "\n".join(block_lines) + "\n")
        changes.append(
            f"producer: (absent) → op_identity={producer.get('op_identity')}, "
            f"typed_command={producer.get('typed_command')}"
        )
    # If present: carry unchanged — not a drift condition; no changes entry.

    if not changes:
        return None  # already clean — idempotent

    rebuilt = rebuild(split, fm_text)
    return {'rebuilt': rebuilt, 'changes': changes}


def _normalize_one(
    file_path: Path,
    carried_deliverable_id: Optional[str] = None,
    session_id: str = "",
) -> Optional[Dict]:
    """Read file and compute its normalized content.

    Thin wrapper around _normalize_one_text: reads the file and delegates all
    normalization logic to the pure helper.  Used by the dry-run path (write=False)
    where no lock is needed.

    `carried_deliverable_id` is the batch-invariant resolved value (see
    `_resolve_claimed_plan_deliverable_id`); `session_id` is the current
    session's resolved id (see `coordinator_core.session.core.resolve_session_id`).
    Both are narrowed per-file via `_carry_value_for_file` — the executing
    handoff's carried value applies ONLY to the file this session itself holds
    `claimed_by` on, never to unrelated key-absent files the sweep also touches
    (break-class fix, 2026-08-12 — see `_carry_value_for_file`'s docstring).
    Sole caller is this module's own dry-run handler branch.

    Returns {'rebuilt': str, 'changes': [str]} when drift is detected, None when
    already clean, or _NO_FRONTMATTER when the file lacks a valid frontmatter block.
    Raises OSError on I/O failure — caller logs and appends to errors list.
    """
    content = file_path.read_text(encoding="utf-8")
    local_carry = _carry_value_for_file(content, carried_deliverable_id, session_id)
    return _normalize_one_text(content, file_path, local_carry)


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

    # Break-class fix (2026-08-12): the batch-invariant carried_deliverable_id
    # above must NOT be applied uniformly across the sweep — only the file THIS
    # session itself claims (`claimed_by` == session_id) may receive it. Resolved
    # once here (not per file — the session id is invariant for the whole call,
    # same rationale as the carried_deliverable_id hoist above); narrowed
    # per-file via `_carry_value_for_file` at each call site below. Unresolvable
    # (`""`) fails toward not-stamping any file — see resolve_session_id's
    # "always returns successfully" contract.
    session_id = resolve_session_id()

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
                local_carry = _carry_value_for_file(old_text, carried_deliverable_id, session_id)
                norm_result = _normalize_one_text(old_text, _fp, local_carry)
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
                result = await asyncio.to_thread(
                    _normalize_one, file_path, carried_deliverable_id, session_id
                )
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
