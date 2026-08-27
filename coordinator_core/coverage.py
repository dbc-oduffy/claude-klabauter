"""
coverage.py

Port of: review-coverage-core.sh (DoE c187f5b9, 2026-07-21) — SAFE_RANGE validator,
JSON/JSONL trail parser, verdict filter (pending excluded; ok/warn/blocked/
waived/absent included), per-record git rev-list union → reviewed_set, batched
git calls.
Port of: review-coverage-gate.sh (DoE d9ac8232, 2026-07-19) — arg-parse,
DAG-vs-flat mode, fixpoint, segment attribution -M100%, asymmetric scope
filter, INDETERMINATE-fails-verdict, verdict-line emit.

Asymmetric scope filter:
    chain_set is OPTIONALLY path-scoped via scope_paths.
    reviewed_set is NEVER path-scoped — all sessions' trail records credit toward
    coverage regardless of which paths those records were scoped to.

Inverted-intuition warning for the (now-removed) DAG-mode fixpoint
(`_derive_dag_chain_set`, cut 2026-08-19 — orphaned by K-007, see
state/kill-ledger.md): EXCLUDING a node from closing_set was the FAIL-OPEN
direction, not the conservative one. A blocker there was a deferral, not a
rejection — the excluded ancestor's commits simply never entered
chain_commits, so they were never counted, never flagged uncovered, and never
waived. Deferral in a coverage gate is subtraction from the denominator:
"blocked" reads as "stricter" in almost every other gate shape, but there a
wider blocker set shrank closing_set, which shrank chain_commits, which
tilted the verdict toward COVERED. Retained here as design history for any
future DAG-mode replacement; there is no closing_set in this module today.

Three fidelity guards that a naive port silently drops — all three implemented here:
    (1) Session-Id UUID-shape validation BEFORE git grep interpolation.
        A Session-Id containing regex metacharacters (e.g. '.*') would over-match
        commits belonging to other sessions → false COVERED verdict. Guard: validate
        UUID shape; malformed → INDETERMINATE (never FALSE COVERED). This guard
        (`_UUID_RE`) lived in the now-removed DAG-mode segment-attribution section
        (`_derive_dag_chain_set`, cut 2026-08-19 — see state/kill-ledger.md); the
        flat-mode path below has no equivalent interpolation site.
    (2) exit-0-with-empty-stdout → INDETERMINATE.
        A has-live-children check that signals "blocked" but cannot enumerate blockers
        is a contract violation — treat as INDETERMINATE rather than vacuously coverable
        (would set all_stale=1 vacuously → false COVERED). This guard's Python-port
        implementation (`coordinator_core.archival.reverse_membership`) lived in the
        now-removed DAG-mode segment-attribution section (`_derive_dag_chain_set`, cut
        2026-08-19 — see state/kill-ledger.md); the flat-mode path below has no
        equivalent call site.
    (3) all-stale-blockers → coverable. If every blocker handoff's consuming session is
        non-live, the commit is not blocked by any active workstream → treat that
        ancestor as coverable. Implemented via _handoff_session_live() + live_sids
        frozenset (hoisted once for the fixpoint).

Verdict line (frozen CLI contract, AC11; extended by C10 with coverage_ratio):
    range=<range> chain_commits=N covered=M uncovered=K coverage_ratio=R.RR VERDICT={COVERED|WARN|INDETERMINATE}

C10 (docs/plans/2026-08-05-coverage-gate-planning-artifact-class.md): the
pre-C10 binary UNCOVERED token is gone. WARN replaces it below the
code-partition coverage-ratio threshold (DEFAULT_COVERAGE_RATIO_THRESHOLD,
~66%) and carries a remediation OFFER, never a block — see the "Main gate
function" section comment below for the full hard-block decision.

Exit-code mapping (returned as exit_code field for veneer/op layer):
    0 — COVERED or WARN (verdict resolved; WARN never halts — see the C10
        hard-block decision note below)
    2 — INDETERMINATE (gate halts; calling skill treats exit 2 as a halt)
    1 — usage / setup error (unsafe range, unresolvable merge-base, git rev-list failure)

Negative-spec:
    - Does NOT path-scope the reviewed_set — asymmetric by design (see the
      asymmetric-scope-filter note above).
    - Does NOT write any coordinator substrate (read-only gate; ipc.py negative-spec).
    - Does NOT use raw-PID liveness (liveness.py / D5 / RAW-PID-LIVENESS floor).
    - Does NOT import archival.reverse_membership — that seam (filled by C4 for the
      DAG-mode fixpoint) was removed along with `_derive_dag_chain_set` on 2026-08-19;
      the C4/Wave-2 staging note it carried no longer applies to this module.

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C3
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from coordinator_core.dag import CONTINUATION_EDGE_KINDS as _DAG_CONTINUATION_EDGE_KINDS
from coordinator_core.dag import walk_forward
from coordinator_core import session_attribution

# Canonical frontmatter-key resolution. `coordinator_core.frontmatter.primitives`
# is the single home for the `^key:` pattern — see the negative-specs on
# _parse_handoff_consumed_by / _parse_handoff_deliverable_id below for why a
# local `\s*`-padded fork is a break-class defect, not a style preference.
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.claim_state import resolve_claim_state

from coordinator_core.win_portability import no_console_creationflags

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: SAFE_RANGE — argument-injection validator.
#: Each side must START with an alphanumeric (blocks leading-dash argument injection,
#: e.g. "--output=/x..y" reaching `git rev-list` as a flag). Permits legitimate trail
#: shapes: hex SHAs, HEAD, and ^/~N ancestry suffixes.
SAFE_RANGE = re.compile(
    r"^[0-9A-Za-z_/.][0-9A-Za-z_/.~^]*\.\.\.?[0-9A-Za-z_/.][0-9A-Za-z_/.~^]*$"
)

#: A range-endpoint token whose BASE (before any ^/~N ops) is the literal
#: symbolic ref "HEAD" — case-sensitive, matching git's own ref spelling.
_STORED_HEAD_ENDPOINT_RE = re.compile(r"^HEAD(?:[~^][0-9]*)*$")

#: DR-234-class default: the code-partition coverage ratio (covered_code /
#: (covered_code + uncovered_code)) below which the gate WARNs instead of
#: reporting COVERED. A 100%-or-block
#: posture was ruled "absurd even for agents" (PM, 2026-08-06) — this is the
#: SINGLE named home for the threshold; every consumer (coverage.py's own
#: verdict logic, coordinator/bin/merge-gate-and-pr.py,
#: coordinator_core/workstream_complete/directives_review.py) reads this
#: constant rather than hardcoding its own copy. Overridable via the
#: COORDINATOR_COVERAGE_RATIO_THRESHOLD environment variable (a float string
#: in [0.0, 1.0]) for local experimentation — an invalid or out-of-range
#: override is ignored (falls back to the default) rather than raising, since
#: this constant is read at import time by non-daemon CLI callers too.
DEFAULT_COVERAGE_RATIO_THRESHOLD = 0.66


def _record_range_has_stored_head(sha_range: str) -> bool:
    """True if either endpoint of a STORED trail record's sha_range is a
    literal "HEAD" (with or without ^/~N ops), e.g. "0227ea17..HEAD".

    This is the read-side half of the sha_range false-COVERED defect
    (state/improvement-queue/2026-06-30-review-coverage-gate-false-covered-on-tr.yaml):
    "HEAD" is a live symbolic ref, not a fixed anchor. A record persisted with
    it (the write-side bug fixed in review_trail_write.py's
    ``_resolve_symbolic_range``) re-resolves against WHATEVER commit is HEAD at
    the moment the gate later runs — on a shared branch that is every commit
    landed since the record was written, none of which any reviewer opened.

    Excluding at Phase 1 classification (this function's call site) — rather
    than resolving "HEAD" against the record's own SHA, which build_reviewed_set
    has no way to know since no per-record anchor is persisted — is the
    correctness-preserving direction: the record drops out of the reviewed_set
    entirely (never counted), which can only make the gate MORE conservative
    (false UNCOVERED on a range that might genuinely have been reviewed at
    write time), never less (false COVERED). That asymmetry is deliberate: a
    coverage gate that occasionally under-credits is a nuisance a reviewer can
    re-run past; one that over-credits certifies work nobody opened. Existing
    on-disk records carrying a literal HEAD need a migration pass (rewrite the
    stored range to a concrete SHA) to regain their legitimate width — see
    the queue entry's "Migration assessment".

    Does NOT reject other symbolic refs (branch names, tags, "origin/main")
    — those are not the reproduced/observed shape (100% of the ~20 known-bad
    on-disk records use literal "HEAD" specifically; see DoE-claude
    state/review-trail/*.json), and rejecting them would risk excluding
    legitimately-scoped historical records this fix has no evidence exist.
    """
    sep = "..." if "..." in sha_range else (".." if ".." in sha_range else None)
    if sep is None:
        return bool(_STORED_HEAD_ENDPOINT_RE.match(sha_range))
    left, right = sha_range.split(sep, 1)
    return bool(_STORED_HEAD_ENDPOINT_RE.match(left) or _STORED_HEAD_ENDPOINT_RE.match(right))


#: Deliverable-Id shape pattern (fidelity guard 1, deliverable-attribution
#: variant). Mirrored the retired `_UUID_RE`'s purpose (Session-Id UUID-shape
#: validation; see kill-ledger for the DAG-fixpoint cut that removed it) — a
#: malformed deliverable_id containing
#: regex metacharacters interpolated raw into `git log --grep` would
#: over-match commits outside this deliverable → false COVERED. Matches the
#: two real mint shapes from coordinator_core.ops.mint_deliverable_id
#: ("dlv-<stub_id>" / "dlv-<slug>-<6hex>") — alphanumeric + hyphen + `.` body,
#: `dlv-` prefix required (schema cross-field rule). Widened
#: (docs/plans/2026-08-05-author-the-dlv-pattern-for-deliverable-i.md) to admit
#: `.` and to reject the scaffolder's placeholder slug — a live carrier
#: (dlv-first-class-consumer-install-5.8-dogfood-2d336d, example-retrieval-repo-ue-addon)
#: falsified the previous "matches the two real mint shapes" claim, which was
#: a blind spot, not cosmetic: a malformed id that this regex under-rejects
#: still over-matches `git log --grep` and yields a false COVERED. Now
#: mirrors handoff.schema.json's `deliverable_id` pattern exactly (same
#: negative lookahead, same body class) and is pinned against it by a shared
#: case table (coordinator_core/tests/
#: test_deliverable_id_pattern_parity.py) so the two cannot drift apart
#: again. Malformed → INDETERMINATE (never FALSE COVERED), same as a
#: malformed Session-Id.
_DELIVERABLE_ID_RE = re.compile(r"^dlv-(?!placeholder-replace-with)[0-9a-zA-Z][0-9a-zA-Z.-]*$")

#: Both were defined inside the block C5 deleted, but their consumer is
#: `_filter_shas_by_scope_paths` — scope filtering, on C5's explicit
#: NOT-DELETED list — so they are restored here rather than dropped. The
#: `cat-file --batch-check` line shape they parse is `<sha> <type>`, and the
#: match is fail-closed: a token that does not resolve to a full 40-hex sha of
#: a known object type is degraded out of the chunk, never fed to diff-tree.
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_GIT_OBJECT_TYPES = frozenset({"commit", "tree", "blob", "tag"})

# `_BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD` and `_add_commit_touched_file_count`
# (the leg-(b) bulk-sweep guard for the DAG-mode fixpoint's deliverable
# attribution) were removed 2026-08-19 along with `_derive_dag_chain_set`,
# their only caller — see state/kill-ledger.md.

#: Verdict filter:
#:   pending  → EXCLUDED (review not complete; counting pending as coverage would allow
#:              the gate to pass on un-reviewed commits — the latent gap this filter closes).
#:   ok / warn / blocked / waived / absent (no verdict field) → INCLUDED.
EXCLUDED_VERDICTS: FrozenSet[str] = frozenset({"pending"})

#: The CONTINUATION edge kinds — the lineage edges along which a review
#: obligation propagates. Derived from `dag.CONTINUATION_EDGE_KINDS` (the
#: SSOT; see that constant's docstring for why `forked_from` is deliberately
#: absent — schema rule A3a-3, frontmatter/schema_validate.py::
#: _cf_spinoff_predecessor_none) rather than restated — see
#: coordinator_core/tests/test_dag_edge_kind_ssot.py for the drift guard.
#: (Rule C2-4, _cf_origin_predecessor_none_invariant, reinforces the same
#: invariant from the origin-axis side but fires only when an `origin_*`
#: field is present — A3a-3 is the unconditional guarantee this constant
#: rests on.) Used by BOTH legs of _derive_dag_chain_set — Step 1's walk and
#: Step 2's blocker enumeration MUST agree on this set or an ancestor gets
#: deferred to a chain that is structurally incapable of ever claiming it
#: (false COVERED).
_CONTINUATION_EDGE_KINDS: FrozenSet[str] = _DAG_CONTINUATION_EDGE_KINDS

# ---------------------------------------------------------------------------
# Subprocess helper — no shell=True; portable CREATE_NO_WINDOW flag (AC9 safe)
# ---------------------------------------------------------------------------

_NO_CONSOLE: Dict[str, Any] = no_console_creationflags()

# Max concurrent `git rev-list` spawns for a per-range fan-out over distinct,
# independent read-only shell-outs. Imported by
# `coordinator_core.ops.review_coverage_core` (this module's own
# `build_reviewed_set`, which used to bound its own fan-out with this
# constant, was removed — see docs/plans/2026-08-27-the-reviewed-set-is-a-
# file-not-a-computation.md § C5). On Windows each git spawn costs ~90ms
# (CREATE_NO_WINDOW), so resolving N records serially is N×90ms. Bounded
# fan-out keeps wall-clock ~= one spawn without launching an unbounded swarm
# of git.exe at once.
_REVLIST_MAX_WORKERS = 16

def _run(
    cmd: List[str],
    cwd: Optional[str] = None,
    input_text: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Tuple[int, str, str]:
    """Run cmd; return (returncode, stdout.strip(), stderr). Never raises.

    `input_text`, when given, is fed to the child's stdin (used by callers
    like `git diff-tree --stdin`); otherwise stdin is DEVNULL as before — nt:
    inherited invalid stdin + CREATE_NO_WINDOW hangs _execute_child.

    `timeout`, when given, bounds the spawn; a `subprocess.TimeoutExpired`
    is caught by the blanket `except Exception` below and reported as an
    ordinary (1, "", str(exc)) failure — same fail-closed shape as any other
    spawn error, never raised to the caller.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            timeout=timeout,
            **_NO_CONSOLE,
        )
        return result.returncode, result.stdout.strip(), result.stderr
    except Exception as exc:
        return 1, "", str(exc)


# ---------------------------------------------------------------------------
# Trail file parsing
# Dual-shape: JSON (single object, pretty or compact) OR JSONL (one per line).
# ---------------------------------------------------------------------------


class _TrailParseError(Exception):
    """Raised when a trail file cannot be parsed as JSON or JSONL."""


def _parse_trail_file(path: str) -> List[dict]:
    """Parse a trail record file; return list of record dicts.

    Supports JSON (single object, pretty or compact) and JSONL (one object per
    line). Raises _TrailParseError if the file cannot be parsed in either form.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [json.load(fh)]
    except json.JSONDecodeError as json_err:
        # JSONL fallback — one object per line
        try:
            records: List[dict] = []
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    records.append(json.loads(line))
            return records
        except Exception as e:
            raise _TrailParseError(
                f"cannot parse trail file {path!r}: "
                f"failed as JSON ({json_err}) and JSONL ({e})"
            )
    except Exception as e:
        raise _TrailParseError(f"cannot open trail file {path!r}: {e}")


def _verdict_counts(rec: dict) -> bool:
    """True if this record's verdict allows it to count toward reviewed_set.

    Verdict filter:
        pending → EXCLUDED; ok/warn/blocked/waived/absent (None) → INCLUDED.
    """
    v = rec.get("verdict")
    if v is None:
        return True  # legacy record without verdict field — include
    return v not in EXCLUDED_VERDICTS


#: Scope values whose credited set is narrowed by foreign-session exclusion
#: (see `_narrow_foreign_session_scope`). Extended 2026-07-27 (C7,
#: docs/plans/2026-07-27-review-trail-scope-guard.md) from `{"session"}` alone
#: to all three scope values a review-trail record can carry — see that
#: function's docstring for the OLD-vs-NEW semantics this widening replaces.
_FOREIGN_STRIPPED_SCOPES: FrozenSet[str] = frozenset(
    {"session", "chain", "workstream-close-auto"}
)


class _ForeignSessionLookupError(RuntimeError):
    """Raised when the git log subprocess backing foreign-commit attribution
    (any scope in `_FOREIGN_STRIPPED_SCOPES`) fails (non-zero rc — see
    `session_attribution.GitLogFailed`, the underlying error this wraps).

    Review: code-reviewer — deliberately NOT swallowed to an empty result.
    An empty result reads to every caller as "no foreign commits found",
    which for a stripped-scope record's `shas - foreign` computation means
    FULL-WIDTH crediting — exactly the over-crediting bug this filter exists
    to close (see `_narrow_foreign_session_scope`'s own docstring: a concurrent
    peer session's interleaved commits get silently credited). Fail-open on
    a transient git error would revive that defect for the duration of the
    failure, invisibly. Both call sites catch this and route it through the
    existing `on_record_error` skip/raise machinery — "skip" excludes the
    WHOLE record's contribution (fail-closed / conservative, same posture as
    the stored-HEAD read-side fix), never falls back to crediting it.
    """


def _narrow_foreign_session_scope(
    sha_range: str,
    own_session_id: Optional[str],
    cwd: str,
    cache: Dict[Tuple[str, Optional[str]], FrozenSet[str]],
) -> FrozenSet[str]:
    """Within `sha_range`, return the commits whose OWN Session-Id git trailer is
    set and names a DIFFERENT session than `own_session_id` — i.e. commits
    provably NOT authored under the reviewing session, to be excluded from a
    trail record's credited set (any scope in `_FOREIGN_STRIPPED_SCOPES`).

    Root cause this guards (2026-07-26 incident): on a shared branch with
    genuinely CONCURRENT sessions (not a sequential predecessor chain — see
    coordinator/docs/wiki/workstream-complete-review.md § Multi-session
    shared-branch union-coverage hazard, which assumes non-overlapping
    sequential segments), a `scope="session"` record's sha_range is chosen from
    that session's OWN commit boundaries. `git rev-list <sha_range>` is real
    ancestry and is NOT wrong about reachability — but on a linear (no-merge)
    history, "reachable in this chronological window" also captures any OTHER
    concurrently-active session's commits that happen to land between this
    session's own two endpoints. `scope="session"` is documented (see
    workstream-complete-review.md § Marker trail mechanics) as narrower than
    `scope="chain"` specifically so a mid-chain session can record "I reviewed
    MY OWN diff" without over-claiming the whole chain — the sha_range-only
    check silently widened it back to the whole chronological window, crediting
    a peer session's un-reviewed commits as covered.

    Deliberately exclusion-based, not inclusion-based (i.e. NOT "keep only
    commits whose trailer equals own_session_id"): a commit with NO Session-Id
    trailer at all (untrailered authoring, or synthetic/legacy history) is left
    credited exactly as before — only a commit AFFIRMATIVELY attributed to a
    different session is stripped out. This keeps the fix targeted at the
    provable-contamination case the incident exhibits, without narrowing
    coverage on histories where trailer attribution is simply absent.

    OLD semantics (through 2026-07-27, pre-C7): `scope="chain"` (and any other
    scope value, including legacy/absent, and `scope="workstream-close-auto"`)
    was documented as architecturally entitled to the full window per the
    union-coverage rule above and was NEVER passed through this filter — only
    `scope="session"` was narrowed. That entitlement traced to a real
    architectural position (a chain record vouches for the whole reviewed
    chain, which by construction should have no foreign interleaving), but it
    was also the exact mechanism of the 2026-07-27 reported defect: a peer's
    chain-scope record vouched for a commit it never reviewed, and a write-side
    guard that carved chain out (to preserve this entitlement) would not have
    caught it, since the reported record was itself chain-scoped.

    NEW semantics (C7, docs/plans/2026-07-27-review-trail-scope-guard.md,
    AC12): the filter applies UNIFORMLY to `_FOREIGN_STRIPPED_SCOPES` —
    `session`, `chain`, AND `workstream-close-auto`. A legitimate chain record
    over a session's own attributed commits remains fully credited, exactly as
    before; what stops being credited is exactly a chain (or
    workstream-close-auto) record's claim over a commit provably attributable,
    via its own Session-Id trailer, to a DIFFERENT session. This is a
    retroactive read-side recomputation — see C8 for the corpus delta this
    produces on already-written records.

    Cached per (sha_range, own_session_id) — many trail records commonly share
    a range or session_id.

    Thin wrapper over `session_attribution.trailer_foreign_shas` (the shared
    two-consumer classification module — see docs/plans/2026-07-27-review-
    trail-scope-guard.md § C1): this function exists only to preserve
    coverage.py's own fail-closed posture, raising `_ForeignSessionLookupError`
    on a git failure rather than the shared module's generic
    `session_attribution.GitLogFailed`.

    OLD semantics (through 2026-07-27, C7): EVERY commit trailer-attributed
    to a different session is stripped, with no exception.

    NEW semantics (2026-07-28, amending C7 — archive/specs/2026-07/
    2026-07-27-review-trail-scope-guard.md § C7 amendment; superseded by
    the removal below): a foreign-attributed commit was, for a time, NOT
    stripped when it carried a permanent PM-vouch waiver written by
    `coordinator_core.ops.review_trail_write._guard_foreign_session_range`.

    AMENDED 2026-07-31 (docs/plans/2026-07-31-review-trail-chain-ancestry-
    discriminator.md § C1): a SECOND source, a per-SHA per-chain waiver
    minted by the coverage GATE at HALT, was introduced here and then
    REMOVED (state/kill-ledger.md K-005, 2026-08-16 — "waiver system dies"):
    the whole chain-ancestry-waiver mechanism (`coordinator_core.
    chain_ancestry_waivers`, the mint in the now-deleted
    `coordinator_core.ops.coverage_gate`, and this read-side crediting tail)
    is gone. A census of the live corpus found 1,893 waivers with
    `em_disposition` null on 1,889 of them and every waiver's own
    `reader_note` disclaiming that it certifies any review — the mechanism
    was auto-issuing its own bypass of the foreign-session-range refusal it
    exempted, ~126 times a day, with no human engagement. See
    docs/wiki/cost-budgets-and-the-kill-disposition.md.

    REMOVED (docs/plans/2026-08-08-vouch-free-review-coverage-gates.md §
    C2/C3): the PM-vouch waiver source (`_pm_vouched_waiver_shas`,
    `state/review-trail/pm-vouches/`) was deleted outright before this one.
    No waiver source of any kind remains: a foreign-attributed commit is
    unconditionally stripped, with no exception. The `reviewer_attestation`
    admission path that briefly qualified that (K-007, state/kill-ledger.md)
    admitted nothing in ~761 records and is gone — do not reintroduce a
    per-record exemption parameter here without a producer that populates it.
    """
    try:
        foreign = session_attribution.trailer_foreign_shas(
            sha_range, own_session_id, cwd, cache, run=_run,
        )
    except session_attribution.GitLogFailed as exc:
        raise _ForeignSessionLookupError(str(exc)) from exc
    return foreign


# ---------------------------------------------------------------------------
# Bookkeeping-vs-code partition (signal-honesty fix — coverage.py is an ORACLE,
# not a lock; see ops/ceremony/tail_ops.py:698's own disclaimer). A ceremony
# like /workstream-complete necessarily authors its own bookkeeping commits
# (completion entry, review-trail record, shipped_in stamp, boot sweep,
# pickup-assemble claim) AFTER the trail record that would cover them, which
# used to pin VERDICT=UNCOVERED forever on any workstream that ran the
# ceremony even when every genuinely-uncovered commit was ceremony
# bookkeeping, not code a reviewer could open. This partition keeps the
# verdict keyed on the CODE partition only, while still surfacing the
# bookkeeping partition (never silently dropping it).
# ---------------------------------------------------------------------------

#: Path prefixes whose commits are ceremony bookkeeping, never code a reviewer
#: opens (completion entries, review-trail records, shipped_in stamps, boot
#: sweep notes, pickup-assemble claims). Editable/greppable single source of
#: truth for the bookkeeping-vs-code partition below.
#: Review: code-reviewer — each prefix carries a trailing `/`, which is what
#: makes `str.startswith` safe here (a path-component match, not a substring
#: match): "statement.py".startswith("state/") is False. Without the trailing
#: slash this would false-classify e.g. "statement.py" as bookkeeping.
#:
#: Negative-spec: this prefix tuple is NOT the whole discriminator, and must
#: not be read as one. Path prefix alone cannot separate a handoff being
#: AUTHORED (trackable content) from ceremony exhaust that merely mutates the
#: same tree — both land under `state/`. The change-type leg lives in
#: `_handoff_authoring_shas` below; widening this tuple without consulting it
#: is how 87578a319 turned the gate vacuous. Do not "simplify" by dropping
#: `state/` from here and enumerating ceremony subpaths instead: that flips
#: `shipped_in`-stamp and pickup-assemble-claim commits back to CODE, which
#: is the permanent false-UNCOVERED tail
#: 87578a319 existed to remove.
_BOOKKEEPING_PATH_PREFIXES: Tuple[str, ...] = ("state/", "archive/", "tasks/", "cross-repo/")

#: The handoff corpus. A commit that *introduces* a file here is authoring a
#: handoff — the primary content the DAG coverage gate exists to track, and
#: the commit `_derive_dag_chain_set` attributes a node to (it resolves a
#: node's authoring commit with `git log --follow -M100% --diff-filter=A`,
#: then reads the `Session-Id` trailer off it). A commit that merely MUTATES a
#: file here is ceremony exhaust — the `shipped_in` stamp, the
#: pickup-assemble claim, the fleet archival move — which is what
#: `_BOOKKEEPING_PATH_PREFIXES` catches. See `_handoff_authoring_shas`.
_HANDOFF_AUTHORING_PATH_PREFIX = "state/handoffs/"

#: `git log --name-status` status letters that INTRODUCE the destination path
#: (add, copy, rename-in), as opposed to mutating or removing one. Renames and
#: copies are included fail-closed: a file appearing at a `state/handoffs/`
#: path by any means reads as content the gate should keep tracking, and
#: over-classifying as CODE is the loud direction.
_PATH_INTRODUCING_STATUSES = ("A", "C", "R")

#: Header line marker for _commit_touched_paths' batched `git log --format=`
#: output — a control byte that cannot appear in a legitimate path or SHA, so
#: it unambiguously distinguishes a per-commit header line from a --name-only
#: path line when parsing the concatenated multi-commit output stream.
_COMMIT_HEADER_SENTINEL = "\x02"


def _is_bookkeeping_path(path: str) -> bool:
    """True if `path` falls under a bookkeeping prefix (see _BOOKKEEPING_PATH_PREFIXES)."""
    return any(path.startswith(prefix) for prefix in _BOOKKEEPING_PATH_PREFIXES)


#: Path prefixes whose commits are PLANNING artifacts — a plan, a piece of
#: planning research/problem-framing prose, or a plan's own sidecar — never
#: code a reviewer opens, but ALSO never exempt from review the way EXHAUST
#: is (see AC9 / _classify_bookkeeping_shas' docstring). Deliberately its OWN
#: tuple, separate from `_BOOKKEEPING_PATH_PREFIXES` — see the negative-spec
#: above that constant and AC2: widening the bookkeeping tuple to cover this
#: class would exempt plan commits from review entirely, the vacuity
#: direction 87578a319 exists to prevent.
#:
#: Widened 2026-08-06 (EM ruling, docs/plans/2026-08-05-coverage-gate-
#: planning-artifact-class.md § Out of scope) from `docs/plans/` alone to the
#: four prefixes below — `docs/decisions/`, `docs/reference/`, and
#: `docs/wiki/` are deliberately EXCLUDED: they are doctrine/reference prose,
#: not planning artifacts, and stay reviewable exactly like code.
_PLANNING_ARTIFACT_PATH_PREFIXES: Tuple[str, ...] = (
    "docs/plans/",
    "docs/research/",
    "docs/problems/",
    "state/plan-sidecars/",
)


def _is_planning_artifact_path(path: str) -> bool:
    """True if `path` falls under a planning-artifact prefix (see
    _PLANNING_ARTIFACT_PATH_PREFIXES)."""
    return any(path.startswith(prefix) for prefix in _PLANNING_ARTIFACT_PATH_PREFIXES)


#: `git --numstat`'s rename-row notation — the CANONICAL definition, shared
#: by `review_brightline_gate.py` and `workstream_complete/__init__.py`
#: (both re-export `_resolve_numstat_row_path` below rather than each
#: keeping its own copy — see this module's own callers' path-predicate
#: bug this shared home fixes, docs/plans/2026-08-12-numstat-rename-rows-
#: leak-past-the-noise-fi.md). Homed here, not in either gate module,
#: because `review_brightline_gate.py` is imported BY
#: `workstream_complete/__init__.py` at module scope — a straight import
#: the other direction would be a true two-file cycle, not merely a
#: fragile ordering; this module is a dependency of both and a dependent
#: of neither.
_REVIEW_SCALE_BRACED_RENAME_RE = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")
_REVIEW_SCALE_BARE_RENAME_RE = re.compile(r"^(.*) => (.*)$")


def _resolve_numstat_row_path(path: str) -> str:
    """`git --numstat`'s rename row can name a path as `a/b/{old => new}.c`
    (compact form, shared prefix/suffix hoisted out of the braces) or as a
    bare `old/path => new/path` row (no shared prefix/suffix to hoist).
    Every path PREDICATE (`_is_noise_path`, `classify_surface`,
    `_is_planning_artifact_path`) needs the DESTINATION path, not the
    literal rename fragment, to classify the row correctly — the literal
    fragment starts `{state/handoffs => ` or `state/handoffs/x.md => `,
    which fails every one of those predicates' path-prefix/suffix checks
    even though the row's real destination is squarely inside them. LOC
    accounting is unaffected either way: the numstat added/deleted columns
    already sum the whole row regardless of what this function returns —
    this function's return value ONLY changes classification/noise
    outcomes, never the LOC sum itself."""
    braced = _REVIEW_SCALE_BRACED_RENAME_RE.match(path)
    if braced is not None:
        prefix, _old, new, suffix = braced.groups()
        return f"{prefix}{new}{suffix}"
    bare = _REVIEW_SCALE_BARE_RENAME_RE.match(path)
    if bare is not None:
        _old, new = bare.groups()
        return new
    return path


#: Chunk size for `_commit_touched_paths`' bare-SHA `git log --no-walk`
#: positional-args batching — mirrors `_TRAILER_LOOKUP_CHUNK`'s rationale
#: (keeps each spawn's argv comfortably under Windows' ~32K command-line
#: length ceiling; a 40-hex SHA plus separator is a few bytes, so 300 per
#: chunk leaves ample headroom). Unscoped whole-chain runs on this repo have
#: exceeded 1900 SHAs, which previously blew a single unchunked argv past the
#: ceiling ([WinError 206] "The filename or extension is too long").
_TOUCHED_PATHS_CHUNK = 300


def _commit_touched_paths(
    shas: List[str],
    cwd: str,
    cache: Dict[str, FrozenSet[str]],
) -> Tuple[Dict[str, FrozenSet[str]], Optional[str]]:
    """Return ({sha: frozenset(touched paths)}, diagnostic_note) for `shas`, via
    batched `git log --no-walk --name-only <shas>` calls chunked at
    `_TOUCHED_PATHS_CHUNK` per spawn (mirrors _bulk_trailer_lookup's
    batched-call + per-invocation-cache shape above — see that function's
    docstring for why bare-SHA positional args are safe to chunk arbitrarily,
    unlike ranges). A sha that git cannot resolve, or that resolves with zero
    touched paths (an empty/root commit), maps to an empty frozenset — the
    caller (_classify_bookkeeping_shas) treats that as fail-closed CODE, never
    bookkeeping, per requirement 2's "zero resolvable paths is NOT bookkeeping"
    rule.

    `diagnostic_note` is non-None when ANY chunk's `git log` call fails
    (rc != 0) — every uncached sha in a FAILED chunk degrades to an empty
    frozenset (fail-closed CODE); shas in chunks that succeeded still resolve
    normally. The note reports the total failed-commit count across all
    chunks so a large unscoped run (that previously died outright on a single
    oversized argv) degrades visibly per-chunk instead of either crashing or
    silently reading as "no bookkeeping found" for the whole set. Review:
    code-reviewer — a non-zero rc previously left the degradation
    undiagnosable in the same notes channel the bookkeeping-exclusion note
    already uses; batching preserves that same visibility per chunk.

    Cached per sha (not per sha-list) so repeated calls across overlapping
    uncovered-sha sets within one gate invocation reuse work.
    """
    uncached = [sha for sha in shas if sha not in cache]
    failed_count = 0
    first_err = ""
    if uncached:
        for i in range(0, len(uncached), _TOUCHED_PATHS_CHUNK):
            chunk = uncached[i : i + _TOUCHED_PATHS_CHUNK]
            rc, out, err = _run(
                [
                    "git", "log", "--no-walk",
                    f"--format={_COMMIT_HEADER_SENTINEL}%H", "--name-only",
                ]
                + chunk,
                cwd=cwd,
            )
            resolved: Dict[str, Set[str]] = {}
            if rc == 0:
                current_sha: Optional[str] = None
                for line in out.splitlines():
                    if line.startswith(_COMMIT_HEADER_SENTINEL):
                        current_sha = line[len(_COMMIT_HEADER_SENTINEL):].strip()
                        resolved.setdefault(current_sha, set())
                    elif current_sha is not None and line.strip():
                        resolved[current_sha].add(line.strip())
            else:
                failed_count += len(chunk)
                if not first_err:
                    first_err = err.strip() or "unknown error"
            for sha in chunk:
                cache[sha] = frozenset(resolved.get(sha, set()))
    note: Optional[str] = None
    if failed_count:
        note = (
            "coverage: bookkeeping classification skipped — git log failed "
            f"for {failed_count} commit(s): {first_err or 'unknown error'}"
        )
    return {sha: cache[sha] for sha in shas}, note


def _handoff_authoring_shas(shas: List[str], cwd: str) -> FrozenSet[str]:
    """Return the subset of `shas` that INTRODUCE a file under state/handoffs/.

    The change-type leg of the bookkeeping-vs-code partition. Path prefix alone
    cannot tell a handoff being authored from ceremony exhaust mutating the same
    tree; the discriminator is which commit put the file there. That is the same
    signal `_derive_dag_chain_set` uses to attribute a DAG node to its authoring
    commit (`git log --follow -M100% --diff-filter=A`), so a commit this returns
    is exactly one the gate may be asked to hang a `Session-Id` attribution off.

    Uses `--name-status` rather than `_commit_touched_paths`' `--name-only`, and
    is deliberately kept a separate call rather than folded into that helper:
    under rename detection `--name-status` reports BOTH sides of a rename where
    `--name-only` reports only the destination, so sharing one parse would flip
    ceremony archival moves (`docs/plans/X` -> `archive/specs/X`) from
    bookkeeping to mixed/CODE — a behaviour change nothing here wants.

    A failed batch returns the empty set. That degrades toward the pre-existing
    bookkeeping classification rather than toward CODE, which is why the caller
    only reaches this for shas ALREADY established as bookkeeping-by-path; a
    git failure therefore cannot manufacture a new exclusion, only fail to lift
    one.
    """
    if not shas:
        return frozenset()
    rc, out, _ = _run(
        [
            "git", "log", "--no-walk",
            f"--format={_COMMIT_HEADER_SENTINEL}%H", "--name-status",
        ]
        + shas,
        cwd=cwd,
    )
    if rc != 0:
        return frozenset()
    authoring: Set[str] = set()
    current_sha: Optional[str] = None
    for line in out.splitlines():
        if line.startswith(_COMMIT_HEADER_SENTINEL):
            current_sha = line[len(_COMMIT_HEADER_SENTINEL):].strip()
            continue
        if current_sha is None or not line.strip():
            continue
        fields = [f for f in line.split("\t") if f.strip()]
        if len(fields) < 2:
            continue
        status, dest = fields[0], fields[-1]
        if status.startswith(_PATH_INTRODUCING_STATUSES) and dest.startswith(
            _HANDOFF_AUTHORING_PATH_PREFIX
        ):
            authoring.add(current_sha)
    return frozenset(authoring)


def _classify_bookkeeping_shas(
    shas: List[str],
    cwd: str,
    cache: Dict[str, FrozenSet[str]],
) -> Tuple[FrozenSet[str], FrozenSet[str], Optional[str]]:
    """Partition `shas` into a THREE-way split: (exhaust_set, planning_set, note).

    A sha is EXHAUST (unchanged semantics) iff it has >=1 touched path AND
    EVERY touched path is under a bookkeeping prefix
    (_BOOKKEEPING_PATH_PREFIXES) AND it does not introduce a file under
    state/handoffs/ (see _handoff_authoring_shas). Fail-closed anti-exploit
    property (required, tested): a commit that touches both a bookkeeping path
    and any other path (mixed commit) classifies as CODE, not bookkeeping — a
    sha with zero resolvable paths also classifies as CODE (never vacuously
    bookkeeping).

    A sha is PLANNING (OD-1's composed rule, docs/plans/2026-08-05-coverage-
    gate-planning-artifact-class.md) iff: (a) >=1 touched path is a
    planning-artifact path (_PLANNING_ARTIFACT_PATH_PREFIXES) AND (b) every
    touched path is either a planning-artifact path or an existing bookkeeping
    path AND (c) it does not introduce a file under state/handoffs/. `planning_set`
    is DISJOINT from `exhaust_set` by construction: EXHAUST WINS ON OVERLAP — a
    sha whose every path is bookkeeping (state/plan-sidecars/ included, since
    that path already starts with the bookkeeping prefix "state/") is classified
    EXHAUST and never reaches the PLANNING branch, even though some of its paths
    are also planning-artifact paths. PLANNING is reachable only when >=1
    touched path lies OUTSIDE the bookkeeping prefixes.

    Every sha not returned in either set is, by construction, CODE.

    FAIL-CLOSED DIRECTION for PLANNING (stated explicitly, mirroring the
    EXHAUST negative-spec below): a fail-open PLANNING misclassification
    DOWNGRADES a review obligation from code review to plan review — it does
    NOT drop the review obligation the way a fail-open EXHAUST classification
    would. PLANNING is not exempt from review under AC9; only EXHAUST is.

    Negative-spec (the 87578a319 regression, closed for EXHAUST here and
    carried into PLANNING as its own hard requirement): a handoff-authoring
    commit writes `state/handoffs/<name>.md` and nothing else, so under a
    path-prefix rule ALONE every DAG chain classified 100% bookkeeping,
    `uncovered_shas` went permanently empty and VERDICT=COVERED fired whether or
    not any review happened. The change-type leg (`_handoff_authoring_shas`) is
    what makes the exclusion ceremony-shaped rather than corpus-shaped; do not
    drop it from EITHER predicate. A commit touching both `docs/plans/` and
    `state/handoffs/` (introducing the handoff) must classify CODE, not
    PLANNING — omitting this leg from the PLANNING predicate reopens
    87578a319's vacuity through the new door instead of the old one.

    Third return value is a diagnostic note (or None) propagated from
    _commit_touched_paths when the underlying git log batch failed.
    """
    touched, note = _commit_touched_paths(shas, cwd, cache)
    by_path: List[str] = []
    planning_by_path: List[str] = []
    for sha in shas:
        paths = touched.get(sha, frozenset())
        if not paths:
            continue
        if all(_is_bookkeeping_path(p) for p in paths):
            # EXHAUST wins on overlap — a bookkeeping-only commit (which may
            # include state/plan-sidecars/ paths) never reaches PLANNING.
            by_path.append(sha)
            continue
        if any(_is_planning_artifact_path(p) for p in paths) and all(
            _is_planning_artifact_path(p) or _is_bookkeeping_path(p) for p in paths
        ):
            planning_by_path.append(sha)
    # Second git call only for commits already classified-by-path AND touching
    # the handoff corpus at all — the common uncovered set never reaches it.
    candidates = [
        sha
        for sha in by_path + planning_by_path
        if any(
            p.startswith(_HANDOFF_AUTHORING_PATH_PREFIX)
            for p in touched.get(sha, frozenset())
        )
    ]
    authoring = _handoff_authoring_shas(candidates, cwd)
    exhaust_set = frozenset(sha for sha in by_path if sha not in authoring)
    planning_set = frozenset(sha for sha in planning_by_path if sha not in authoring)
    return exhaust_set, planning_set, note


#: `kind` values a "plan" bucket is credited against no differently from
#: "diff" — reserved for future kinds this chunk does not add. Kept as a
#: single-element tuple rather than a bare string comparison so a future kind
#: addition greps to one obvious spot.
_UNRESTRICTED_CREDIT_KINDS: Tuple[str, ...] = ("diff",)

#: `scope_kind` values Phase 1 resolves into a credit-bearing `kind` — "diff"
#: (legacy/explicit) and "plan"; "integration" is a recognized-but-skipped
#: shape (see the Phase 1 loop above) and never reaches here. Mirrors
#: review-trail.schema.json's `scope_kind` enum — a schema value outside this
#: set is a per-record zero-credit degrade (WARN, not a global fatal), never
#: an AssertionError; see `_credit_from_kind_partition`.
_RECOGNIZED_SCOPE_KINDS: Tuple[str, ...] = ("diff", "plan", "integration")


def _credit_from_kind_partition(
    reviewed_by_kind: Dict[str, Set[str]],
    cwd: str,
) -> Set[str]:
    """Collapse a per-kind reviewed partition into ONE credited set — the
    kind-aware crediting rule (C5, docs/plans/2026-08-05-coverage-gate-
    planning-artifact-class.md § C5), applied identically regardless of which
    Phase 2 strategy (single-graph-walk or per-range fan-out) produced the
    partition.

    "diff" (and any future unrestricted kind, see _UNRESTRICTED_CREDIT_KINDS)
    credits its resolved SHAs unconditionally, exactly as before this chunk.

    "plan" credits ONLY the subset of its resolved SHAs that
    `_classify_bookkeeping_shas` independently classifies PLANNING (i.e. a
    commit whose touched paths are entirely planning-artifact/bookkeeping
    paths with >=1 planning-artifact path, and that does not author a
    state/handoffs/ file) — reusing the already-landed C2 classifier rather
    than inventing a second notion of "planning commit". This is the fix for
    the naive "just delete the skip" shortcut (Anti-scope): that shortcut
    would credit a plan review's ENTIRE resolved range unconditionally,
    including any code commits it happens to span — worse than the false
    tail it replaces, and exploit-shaped (AC6). Filtering the plan bucket
    down to genuinely-planning commits means a plan review can never credit
    code, however its sha_range is drawn.

    Any other kind (there are none yet reachable here — "integration" is
    skipped in Phase 1 and never reaches Phase 2) credits nothing, fail-closed.

    2026-08-10: this used to `assert set(reviewed_by_kind) <= {"diff", "plan"}`
    — self-verifying, but fatal to the WHOLE gate the moment one review-trail
    record anywhere in the corpus carried an unrecognized scope_kind (the
    schema had no enum, so nothing stopped one being hand-authored). A single
    stray record could then block every PARTITION-MANDATORY close on a repo
    from ever reaching a VERDICT line. Per-record degrade replaces it: the
    WARN for an unrecognized kind is emitted upstream, at Phase 1
    classification (where the record's artifact/path are still in scope) —
    this function's fail-closed-by-omission (an unrecognized kind is simply
    never read from `reviewed_by_kind`) is left to do the crediting work
    silently, same as before the assertion existed.
    """
    credited: Set[str] = set()
    for kind in _UNRESTRICTED_CREDIT_KINDS:
        credited |= reviewed_by_kind.get(kind, set())

    plan_raw = reviewed_by_kind.get("plan", set())
    if plan_raw:
        _, planning_set, _note = _classify_bookkeeping_shas(list(plan_raw), cwd, {})
        credited |= (plan_raw & planning_set)

    return credited


# ---------------------------------------------------------------------------
# reviewed_set builder
# ---------------------------------------------------------------------------

#: Chunk size for `_bulk_trailer_lookup`'s positional-SHA `git log` batching —
#: keeps each spawn's argv comfortably under Windows' ~32K command-line
#: length ceiling (a 40-hex SHA plus separator is a few bytes; 300 per chunk
#: leaves ample headroom for the rest of the argv).
_TRAILER_LOOKUP_CHUNK = 300


def _bulk_trailer_lookup(shas: Set[str], cwd: str) -> Optional[Dict[str, str]]:
    """Return {sha: session_id} for every sha in `shas` that carries its own
    Session-Id git trailer, via `git log --no-merges` over BARE SHA
    positional args — never ranges. A bare SHA has no exclusion semantics
    (it is simply "show this commit"), so batching many of them into one
    spawn cannot silently drop a commit the way combining several `A..B`
    ranges into one `git log` invocation could (multi-range `git log`
    applies exclusions GLOBALLY across all ranges given, not per-range —
    unsafe for this use). Chunked at `_TRAILER_LOOKUP_CHUNK` per spawn to
    stay under Windows' argv length ceiling on a large `shas` set.

    Returns None (never a partial map) on ANY chunk failure — callers must
    treat that as "could not bulk-resolve" and fall back to their own
    precise per-item resolution rather than trusting a partial result,
    matching the fail-closed posture `session_attribution.GitLogFailed`
    already promises for the per-range trailer classifier this batches.
    """
    if not shas:
        return {}
    ordered = sorted(shas)
    result: Dict[str, str] = {}
    for i in range(0, len(ordered), _TRAILER_LOOKUP_CHUNK):
        chunk = ordered[i : i + _TRAILER_LOOKUP_CHUNK]
        rc, out, _err = _run(
            [
                "git", "log", "--no-merges",
                "--format=%H%x1f%(trailers:key=Session-Id,valueonly)",
            ] + chunk,
            cwd=cwd,
        )
        if rc != 0:
            return None
        for line in out.splitlines():
            if "\x1f" not in line:
                continue
            sha, trailer = line.split("\x1f", 1)
            sha = sha.strip()
            trailer = trailer.strip()
            if sha and trailer:
                result[sha] = trailer
    return result


def emit_unrecognized_kind_warning(unrecognized_kind_counts: Dict[str, int]) -> None:
    """Emit ONE aggregated stderr WARN for every unrecognized-`scope_kind`
    record a walk classified, naming the total count and every distinct kind
    seen. A no-op if the walk saw none. Imported by
    `coordinator_core.ops.review_coverage_core`'s own `build_reviewed_set`/
    `build_segments` (this module's own `build_reviewed_set` was removed —
    see docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md
    § C5) so both call sites emit identical text (the two were independently
    maintained until a reviewer flagged the drift risk). The shared copy
    lives HERE because the existing dependency runs
    review_coverage_core.py -> coverage.py; putting it the other way round
    would invert that edge and create an import cycle."""
    if not unrecognized_kind_counts:
        return
    total = sum(unrecognized_kind_counts.values())
    distinct = sorted(unrecognized_kind_counts)
    print(
        f"WARN: {total} record(s) with an unrecognized scope_kind — "
        f"each credits nothing: {', '.join(distinct)}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Handoff-level utilities for the DAG-mode fixpoint
#
# `_build_dag_index` (the in-memory handoff-path index for the archival seam)
# was removed 2026-08-19 along with `_derive_dag_chain_set`, its only caller —
# see state/kill-ledger.md. The functions below have their own live consumers
# and survive independently.
# ---------------------------------------------------------------------------


def _parse_handoff_consumed_by(
    handoff_path: str,
    *,
    common_dir: Optional[Path] = None,
    repo_root: Optional[str] = None,
) -> Optional[str]:
    """Ledger-first claim holder for a handoff — routes through
    ``coordinator_core.claim_state.resolve_claim_state`` (C1/C2, this plan).

    Ledger-first, frontmatter-mirror fallback: the ledger wins whenever it
    holds a live claim, regardless of what the tracked-frontmatter mirror
    says — see ``claim_state.ClaimState``'s own docstring for the
    branch-switch-revert incident this generalizes a fix for. Falls back to
    the mirror (dual-tolerant ``claimed_by``/``consumed_by``, same
    resolution order as before) only when the ledger has no live claim.

    ``common_dir``/``repo_root`` are optional pre-resolution hooks — pass
    ``common_dir`` on a hot path (this module's DAG fixpoint) to skip a
    redundant ``git_common_dir`` cached-dict lookup per call; omit both to
    let ``resolve_claim_state`` derive ``common_dir`` from ``handoff_path``'s
    own parent directory, matching this function's original behavior when
    called bare.

    No try/except here by design: the two callers below
    (``_get_handoff_consumed_by``, ``_handoff_session_live``) need DIFFERENT
    failure treatment — the former's contract is depended on verbatim by
    external call sites, the latter feeds the DAG-fixpoint Guard-2
    notes/indeterminate machinery — so each catches independently.

    Unreadable-file raise, restored (C2-fix): ``resolve_claim_state`` itself
    degrades an unreadable/missing handoff file to "no claim" (``holder is
    None``) rather than raising — deliberate on its side, since an absent
    claim and an unreadable file are indistinguishable to a bare accessor.
    But the two silent-fallback regression guards in
    ``test_coverage_dag_silent_fallback_guards.py`` depend on THIS function
    raising on an unreadable handoff file specifically — silent fallback
    inside the DAG fixpoint is the named hazard those guards exist to catch.
    So this leaf performs its own file-readability check first (matching the
    pre-ledger-first behavior verbatim: a bare ``open(...).read(4096)`` with
    no try/except, raising whatever ``OSError`` subtype the filesystem
    produces), and only once the file is known readable does it delegate the
    claim-answer itself to ``resolve_claim_state`` — ledger-first, as C2
    intends. The re-read here is intentionally redundant with
    ``resolve_claim_state``'s own internal read; that duplication is the
    price of keeping the raise/degrade discrimination in the one place that
    owns it (this leaf), per the EM ruling for this chunk.
    """
    with open(handoff_path, "r", encoding="utf-8", errors="replace") as fh:
        fh.read(4096)
    return resolve_claim_state(
        handoff_path, common_dir=common_dir, repo_root=repo_root
    ).holder


def _parse_handoff_deliverable_id(handoff_path: str) -> Optional[str]:
    """Raw frontmatter parse for deliverable_id — returns None on any read/parse
    failure, absence, or an explicit null (schema allows `deliverable_id: null`
    for pre-backfill artifacts — see handoff.schema.json's deliverable_id
    field). Conservative-None: a node with no resolvable deliverable_id falls
    back to the legacy Session-Id-only attribution (unchanged from today),
    never treated as an error.

    Mirrors _parse_handoff_consumed_by's key resolution and 4 KiB read cap —
    deliverable_id lives in the same frontmatter block.

    Negative-spec (break-class fix, 2026-07-28): this carried the identical
    ``^deliverable_id:\\s*…`` newline-crossing pad as its sibling above, with
    the same consequence — a present-but-empty ``deliverable_id:`` returned the
    FOLLOWING line's text (reproduced on LF and CRLF: ``'status: open'``).
    Because the key here is a LITERAL rather than an interpolated variable, this
    copy is invisible BY CONSTRUCTION to the structural gate
    ``coordinator_core/tests/test_no_forked_frontmatter_key_regex.py`` (see its
    § Known blind spots — flagging literal keys is what produces its measured
    76-hit false-positive flood). Fixing only the gate-visible sibling would
    have left that gate green over a still-broken file, so both were fixed
    together. Key resolution routes through the canonical
    ``coordinator_core.frontmatter.primitives``; do not re-fork it here.
    """
    try:
        with open(handoff_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(4096)
    except OSError:
        return None
    val = read_fm_field_unquoted(content, "deliverable_id")
    if val is None:
        return None
    val = val.strip()
    if not val or val.lower() in ("null", "none", "~"):
        return None
    return val


def _commit_deliverable_id_trailers(shas: List[str], cwd: str) -> Dict[str, str]:
    """Return {sha: Deliverable-Id trailer value ("" if absent)} for `shas`,
    via ONE batched `git log --no-walk --format=...` call — mirrors
    _commit_touched_paths' batched-call shape (:352) rather than one
    subprocess per commit.

    Its former caller, the leg-(b) legacy-history fallback in
    `_derive_dag_chain_set`, was removed 2026-08-19 (see state/kill-ledger.md);
    this helper is currently unreferenced. Retained as a standalone
    trailer-lookup utility for any future consumer needing a batched
    sha-to-Deliverable-Id map.

    On git failure (rc != 0), every sha maps to "" (absent) — fail-closed:
    an unreadable trailer is treated as "no trailer" rather than raising.
    """
    if not shas:
        return {}
    result: Dict[str, str] = {sha: "" for sha in shas}
    rc, out, _ = _run(
        [
            "git", "log", "--no-walk",
            f"--format={_COMMIT_HEADER_SENTINEL}%H\x1f%(trailers:key=Deliverable-Id,valueonly)",
        ]
        + shas,
        cwd=cwd,
    )
    if rc != 0:
        return result
    for line in out.splitlines():
        if not line.startswith(_COMMIT_HEADER_SENTINEL):
            continue
        rest = line[len(_COMMIT_HEADER_SENTINEL):]
        sha, _, value = rest.partition("\x1f")
        sha = sha.strip()
        if sha in result:
            result[sha] = value.strip()
    return result


def _get_handoff_consumed_by(
    handoff_path: str,
    *,
    common_dir: Optional[Path] = None,
    repo_root: Optional[str] = None,
) -> Optional[str]:
    """Extract the claimed_by session-id for a handoff — ledger-first (C2,
    this plan), via ``_parse_handoff_consumed_by`` / ``claim_state.
    resolve_claim_state``.

    DR-084 transitional ingest tolerance (C7) still applies on the
    frontmatter-mirror fallback leg: ``claimed_by`` wins over the retired
    ``consumed_by`` name (see ``_parse_handoff_consumed_by`` for the exit
    condition and precedence rationale) — but the ledger, when it holds a
    live claim, now wins over the mirror outright.
    Returns None for unclaimed/open handoffs (both sources) AND for
    read/parse failures (conservative — callers treat None as "live"). This
    is the canonical accessor: imported directly by ops/handoff_reconcile.py,
    ops/ceremony/{resolver,branch_resolution,commit_gates}.py, and
    ops/fleet/archive_handoffs.py, several of which compare the return value
    with `is None` / `== sid` — its Optional[str] contract must not change.
    ``common_dir``/``repo_root`` are optional hot-path pre-resolution hooks,
    threaded straight through to ``_parse_handoff_consumed_by``.
    """
    try:
        if common_dir is not None or repo_root is not None:
            return _parse_handoff_consumed_by(
                handoff_path, common_dir=common_dir, repo_root=repo_root
            )
        return _parse_handoff_consumed_by(handoff_path)
    except Exception as exc:
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        # Previously: except Exception: return None — silent, no note, no log.
        # Guard-2's capture-into-result.notes shape (:856/:864) is NOT reachable
        # here: this function has no result object, and its Optional[str]
        # contract is depended on verbatim by the 6 external call sites named
        # above — returning a tuple would silently break their `is None` /
        # `== sid` comparisons. Fallback per the design call: a stderr
        # diagnostic, RETURN VALUE unchanged (None → conservative live, same
        # fail-closed direction as before). The DAG-fixpoint's own call path
        # (_handoff_session_live below) uses _parse_handoff_consumed_by
        # directly and DOES get full Guard-2 notes+indeterminate treatment.
        print(
            f"_get_handoff_consumed_by: {handoff_path}: {type(exc).__name__}: {exc} "
            f"(non-fatal, conservative-live default)",
            file=sys.stderr,
        )
        return None
        # --- end Tier 2 ---


def _handoff_session_live(
    handoff_path: str,
    live_sids: FrozenSet[str],
    *,
    common_dir: Optional[Path] = None,
    repo_root: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """True if the session that claimed handoff_path is currently live.

    Ledger-first (C2, this plan) via ``_parse_handoff_consumed_by`` —
    calls it DIRECTLY, bypassing ``_get_handoff_consumed_by``, so this is
    the DAG-fixpoint's own call path onto the same C1 accessor.

    Conservative default: if session cannot be resolved (unclaimed or unreadable)
    → return True (do not wrongly treat as stale → do not wrongly cover).
    Second element of the returned tuple carries a Guard-2-shaped note when
    resolution failed due to a read/parse exception (as opposed to a
    legitimately-unclaimed handoff) — None when there is nothing to surface.
    ``common_dir``/``repo_root`` are optional hot-path pre-resolution hooks
    (this function runs per-handoff inside the fixpoint) — see
    ``_parse_handoff_consumed_by``.
    """
    try:
        if common_dir is not None or repo_root is not None:
            sid = _parse_handoff_consumed_by(
                handoff_path, common_dir=common_dir, repo_root=repo_root
            )
        else:
            sid = _parse_handoff_consumed_by(handoff_path)
    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # Previously (via _get_handoff_consumed_by's shared except Exception:
    # return None): a read/parse failure here was indistinguishable from a
    # legitimately-unconsumed handoff — silent, no note, no log. Matches
    # Guard-2 (:856/:864 in this file): capture type(exc).__name__: {exc} and
    # let the fixpoint call site fold it into result.notes + indeterminate=True.
    except Exception as exc:
        note = (
            f"{handoff_path}: _get_handoff_consumed_by raised "
            f"{type(exc).__name__}: {exc} — INDETERMINATE"
        )
        return True, note  # still conservative-live, but now flagged
    # --- end Tier 2 ---
    if sid is None:
        return True, None  # unclaimed / unresolved → conservative live
    return sid in live_sids, None


# ---------------------------------------------------------------------------
# DAG-mode scope_paths filter — chain_set narrowing by git's own pathspec
# matcher (parity with flat mode's `git rev-list -- scope_paths`). Its
# original caller, run_coverage_gate's DAG branch, was removed by K-001
# (2026-08-16, see state/kill-ledger.md).
# ---------------------------------------------------------------------------

#: Max SHAs per batched `git log --no-walk` scope-filter call. DAG-mode
#: chain_set is fixpoint-derived and can be arbitrarily large (unlike
#: _commit_touched_paths' caller-bounded uncovered-set), so this filter needs
#: its own chunking discipline. Conservative relative to typical OS argv
#: ceilings (~32K chars on Windows, far higher on macOS/Linux) given 40-char
#: SHAs plus scope_paths entries.
_SCOPE_FILTER_CHUNK_SIZE = 200


def _filter_shas_by_scope_paths(
    shas: List[str],
    scope_paths: List[str],
    cwd: str,
) -> Tuple[Optional[FrozenSet[str]], Optional[str]]:
    """Return (subset of `shas` that touch >=1 scope_paths path, diagnostic_note).

    Uses git's own pathspec matcher via batched `git diff-tree --stdin
    --name-only -r -- <scope_paths>` calls rather than a hand-rolled
    prefix/glob match in Python — scope_paths entries are git pathspecs
    (directory prefixes, globs, negation, magic prefixes) and reimplementing
    that semantics here would be subtly wrong.

    `git diff-tree` (not `git log --no-walk`) is load-bearing here: `--no-walk`
    disables git log's pathspec-based history simplification entirely (it is a
    walk-time feature), so `git log --no-walk <shas>... -- <scope_paths>`
    silently returns every given SHA unfiltered regardless of whether its diff
    touches the pathspec — a real defect this function used to carry (see
    test_coverage_dag_scope_paths.py, the tests this function exists to
    satisfy). `diff-tree --stdin` diffs each commit against its own parent
    individually and only ever prints a commit-id line for a commit whose
    per-commit diff matches the pathspec — but only for FULL 40-char object
    names fed on stdin. Review: code-reviewer — an ABBREVIATED sha is
    echoed back verbatim even on a no-match (out-of-scope) commit instead
    of being suppressed (probed live: full-sha/out-of-scope prints nothing;
    full-sha/in-scope prints the id + touched paths; abbreviated-sha/
    out-of-scope prints the bare abbreviation with no names). Left alone,
    that echoed abbreviation lands right back in `chunk_set` (fed in the
    same shape) and gets credited as "matched" — the same unfiltered-
    passthrough bypass the `--no-walk` defect above already illustrates,
    wearing a different command. This function therefore resolves every
    input token to its full object name via the existence-check
    `cat-file --batch-check` call below (which already returns one) BEFORE
    feeding anything to `diff-tree`, and matches diff-tree's output only
    against those full shas.

    Batched in chunks of _SCOPE_FILTER_CHUNK_SIZE SHAs fed over stdin per git
    invocation (argv-length ceiling avoided by using stdin rather than argv),
    not one call per SHA — mirrors _commit_touched_paths' batched-call shape.
    A commit-id line in the output is matched against the set of FULL shas
    resolved for THIS chunk (not treated as a bare hex-looking string, and
    never an unresolved abbreviation) before being credited, so an
    adversarial path that happens to be 40 hex characters cannot be misread
    as a commit boundary.

    Returns (None, note) on any git failure. Fail-closed: the caller must
    neither fall back to the unfiltered set (would silently defeat scoping)
    nor treat failure as an empty result (would silently drop the whole
    chain) — both are exploit-shaped. The caller instead threads the note into
    an INDETERMINATE verdict, consistent with this file's other "cannot safely
    determine this" guards (see _derive_dag_chain_set's Guard 2 and the
    `indeterminate` field on `_DagChainResult`).
    """
    matched: Set[str] = set()
    for i in range(0, len(shas), _SCOPE_FILTER_CHUNK_SIZE):
        chunk = shas[i : i + _SCOPE_FILTER_CHUNK_SIZE]

        # `git diff-tree --stdin` silently no-ops an unresolvable SHA (exit 0,
        # empty output, no stderr) rather than erroring — indistinguishable
        # from "resolved but touches nothing in scope_paths" without this
        # explicit existence check. Fail-closed requires catching that case
        # before it is misread as a scoping decision. This same call also
        # resolves each token's FULL object name (%(objectname)) — reused
        # below (Review: code-reviewer) so an abbreviated input sha is never
        # fed to diff-tree, and never matched against, in its abbreviated
        # form: diff-tree echoes an unmatched abbreviation back verbatim
        # instead of suppressing it, which would otherwise be misread as a
        # scope match.
        rc_check, out_check, err_check = _run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            cwd=cwd,
            input_text="\n".join(chunk) + "\n",
        )
        if rc_check != 0 or any(
            line.strip().endswith(" missing") for line in out_check.splitlines()
        ):
            return None, (
                "coverage: DAG-mode scope_paths filter failed — could not "
                f"resolve one or more of {len(chunk)} commit(s): "
                f"{err_check.strip() or 'unresolvable SHA'}"
            )

        # Map each input token to its resolved full object name — positional,
        # mirroring `_batch_check_hex_tokens`' own empirically-verified
        # order-preserving contract for this exact git subcommand (see that
        # function's docstring). Any line that doesn't cleanly resolve to a
        # full 40-char sha degrades that token out of this chunk (fail-closed
        # — never fed to diff-tree unresolved, never matched).
        full_shas: List[str] = []
        for token, line in zip(chunk, out_check.splitlines()):
            parts = line.split()
            if len(parts) == 2 and parts[1] in _GIT_OBJECT_TYPES and _FULL_SHA.match(parts[0]):
                full_shas.append(parts[0])
        chunk_set = set(full_shas)
        if not full_shas:
            continue

        rc, out, err = _run(
            ["git", "diff-tree", "--stdin", "--name-only", "-r", "--"]
            + scope_paths,
            cwd=cwd,
            input_text="\n".join(full_shas) + "\n",
        )
        if rc != 0:
            return None, (
                "coverage: DAG-mode scope_paths filter failed — git diff-tree "
                f"errored for a batch of {len(chunk)} commit(s): "
                f"{err.strip() or 'unknown error'}"
            )
        for line in out.splitlines():
            sha = line.strip()
            if sha in chunk_set:
                matched.add(sha)
    return frozenset(matched), None


# ---------------------------------------------------------------------------
# DAG-mode chain_set derivation — fixpoint over coverable ancestors
# ---------------------------------------------------------------------------


@dataclass
class _DagNodeAttribution:
    """One coverable ancestry node's identity + the commit segment attributed
    to it — computed exactly once, inline, by _derive_dag_chain_set's Step 3
    (segment attribution), and carried forward on _DagChainResult.node_attribution.

    Exists so a renderer can label an uncovered commit with its originating
    baton and print the ancestry chain without recomputing which commits
    belong to which node — see the module's "Shape to avoid" guidance on
    _derive_dag_chain_set: the per-node segment resolution happens ONCE,
    here, never copy-pasted into a caller. The original renderer,
    run_coverage_gate's DAG-mode UNCOVERED notes, was removed by K-001
    (2026-08-16, see state/kill-ledger.md).

    path:            Absolute handoff path of this ancestry node (a
                     _DagChainResult.node_attribution key, and a member of
                     ordered_ancestry / closing_set).
    session_id:      The Session-Id this node's segment was resolved from
                     (closing_session_id shortcut, or the node's own
                     add-commit trailer). Empty string only for a node whose
                     segment attribution was skipped entirely (see the
                     "ancestor add-commit untrailered" branch below) — such a
                     node still gets an entry here (for ancestry-chain
                     rendering) but with an empty `shas`.
    deliverable_id:  This node's handoff `deliverable_id` frontmatter field,
                     or None when absent (plain Session-Id attribution).
    shas:            The commit SHAs attributed to this node — the plain
                     Session-Id segment, or (when deliverable_id is set) the
                     union of legs (a) and (b) from the deliverable-attribution
                     rule. Empty for a node whose attribution was skipped.
    authored_session_id: the node's own add-commit Session-Id trailer,
                     resolved the same way the `else` branch always has,
                     even for the `closing_abs` node where `session_id`
                     above may take the closing_session_id shortcut. None
                     when not resolved (e.g. the `else` branch already
                     performed this exact derivation, so this simply mirrors
                     `session_id` there).
    attribution_disagrees: True only when `authored_session_id` was
                     resolved, is non-empty, and differs from the running
                     session — i.e. the closing_abs node was authored by a
                     DIFFERENT session than the one closing it: the
                     "picked-up handoff" case.

    READER: `_render_dag_ancestry_notes` — when `attribution_disagrees` is
    True for a node, its rendered ancestry line appends a "picked up
    (authored by <authored_session_id>)" marker, surfacing the DR-286
    detected-pickup case in the operator-facing artifact rather than only in
    a transient `result.notes` line.

    An earlier revision of this docstring said "OBSERVATION ONLY (C1)" and
    "Does not affect `session_id`, `shas`, or any verdict". That was true of
    C1 alone and is now FALSE: on the claim-gated pickup path (DR-286),
    `authored_session_id`'s underlying resolution becomes the attributed
    `sid`, so `session_id`, `shas` and the verdict all can change on exactly
    the condition `attribution_disagrees` records. A code reviewer and a
    review integrator disagreed about whether these fields were dead; the
    integrator was right, and this block exists so the next reader does not
    have to re-run that argument.
    """

    path: str
    session_id: str = ""
    deliverable_id: Optional[str] = None
    shas: FrozenSet[str] = field(default_factory=frozenset)
    authored_session_id: Optional[str] = None
    attribution_disagrees: bool = False


@dataclass
class _DagChainResult:
    """Internal result of the now-removed `_derive_dag_chain_set` (cut
    2026-08-19, orphaned by K-007 — see state/kill-ledger.md). This dataclass
    has no production constructor left in this tree; it is retained solely
    because `coordinator_core/tests/test_review_brightline_gate.py` still
    imports the name (unused by any test there) — deleting it would break an
    out-of-scope test file's collection. Do not build new functionality on
    this type without re-checking that import first.

    shas:              Flat union of every coverable node's attributed commits
                       — unchanged pre-existing contract; callers that only
                       read this field keep working unchanged.
    ordered_ancestry:  walk_forward's own `orderedPaths` (first-encounter
                       order, closing handoff first, roots last) — threaded
                       through verbatim rather than left to go out of scope
                       after Step 1, so a renderer can present the walked
                       chain without re-walking the DAG.
    node_attribution:  {node_path: _DagNodeAttribution} for every node in
                       closing_set (the coverable set the Step 2 fixpoint
                       converged on) — the per-node segment data Step 3
                       already computes but previously discarded at this
                       dataclass's return boundary. A node absent from
                       closing_set (blocked by a live predecessor) never
                       reaches Step 3 and has no entry here, by construction —
                       its commits are also absent from `shas`.
    """

    shas: List[str] = field(default_factory=list)
    indeterminate: bool = False
    notes: List[str] = field(default_factory=list)
    ordered_ancestry: List[str] = field(default_factory=list)
    node_attribution: Dict[str, _DagNodeAttribution] = field(default_factory=dict)
    #: C2 (AC3): commits seen in a coverable node's Session-Id segment but
    #: EXCLUDED from that node's leg (b) because the add-commit that would
    #: have seeded leg (b) was judged a bulk sweep (the threshold constant
    #: this compared against, `_BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD`, was
    #: removed 2026-08-19 with its only caller — see state/kill-ledger.md).
    #: Never a member of `shas` —
    #: these are "in range, unattributable to this chain", not "this
    #: chain's inheritance". Reported so a closer sees they exist and are
    #: someone else's, rather than the report silently shrinking. Additive
    #: field — existing callers reading only `shas`/`node_attribution` are
    #: unaffected.
    unattributable_shas: List[str] = field(default_factory=list)
    #: walk_forward's own `terminatedEarly` discriminator ('' | 'lineage-cycle'
    #: | 'missing-link'), formerly threaded through from `dag.walk_forward`
    #: via the now-removed `_derive_dag_chain_set` (cut 2026-08-19, see
    #: state/kill-ledger.md). No current reader in this tree.
    terminated_early: str = ""


# `_DagChainSetContext` (the optional cross-call cache for `_derive_dag_chain_set`)
# and `_resolve_closing_handoff_disk_path` (its closing-handoff archive-path
# resolver) were removed 2026-08-19 along with `_derive_dag_chain_set`, their
# only caller — see state/kill-ledger.md.


# ---------------------------------------------------------------------------
# Trail path collector
# ---------------------------------------------------------------------------


def _collect_trail_paths(repo_root: str) -> List[str]:
    """Collect review-trail record paths via a direct filesystem scan of
    state/review-trail/ and archive/review-trail/ (sorted, *.json).

    Mirrors the bash "$LIST_RECORDS" call at review-coverage-gate.sh,
    whose accelerated primary path (``bash list-review-trail-records.sh``)
    is retired as of the 2026-07-21 de-bash cutover (C10) — see git log.
    That path had no live caller in this repo and always degraded to this
    exact filesystem-scan fallback in practice. Reimplementing it natively
    would mean threading an explicit *repo_root* through
    ``coordinator_core.ops.list_review_trail_records.list_paths`` (which
    self-resolves state root from ``os.getcwd()``, incompatible with this
    module's AC-5 "no os.getcwd() fallback in multiplex/daemon-mode" rule)
    for a code path nothing exercises; the dead bash-spawn site is deleted
    rather than ported.

    Review: code-reviewer — the retired ``list_records_script`` parameter
    (accept-and-silently-``del`` compatibility shim) is dropped rather than
    kept, per grep confirming zero non-None callers in the tree.
    """
    root = Path(repo_root)
    paths: List[str] = []
    for trail_dir in [
        root / "state" / "review-trail",
        root / "archive" / "review-trail",
    ]:
        if trail_dir.is_dir():
            for p in sorted(trail_dir.rglob("*.json")):
                paths.append(str(p))
    return paths


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass
class CoverageResult:
    """Result of the coverage-gate computation. Originally assembled by
    run_coverage_gate, removed by K-001 (2026-08-16, see
    state/kill-ledger.md). No current constructor in this tree.

    verdict:       'COVERED' | 'WARN' | 'INDETERMINATE' (C10: the pre-C10
                   binary 'UNCOVERED' token no longer exists — see the
                   ratio/threshold assembly below). 'COVERED' when
                   uncovered_shas is empty OR coverage_ratio is at/above the
                   resolved ratio threshold (its resolver was removed with
                   K-001, 2026-08-16 — this class has no constructor in the
                   tree and the described assembly no longer runs); 'WARN'
                   below threshold, carrying a remediation OFFER (dispatch
                   coordinator:review-code over uncovered_shas) rather than a
                   block.
    verdict_line:  Frozen CLI contract line (AC11), extended by C10:
                   'range=<r> chain_commits=N covered=M uncovered=K
                   coverage_ratio=R.RR VERDICT=...' — the ratio is rendered
                   inline so a stdout-only caller sees the number, not just
                   the verdict token.
    chain_commits: Total commits in the chain (0 on INDETERMINATE).
    covered:       Commits with at least one covering trail record.
    uncovered:     Commits with no covering trail record.
    exit_code:     0 (COVERED/WARN — C10: WARN never halts, see
                   coverage_ratio's docstring), 2 (INDETERMINATE), 1 (error).
    notes:         Diagnostic messages (reasons for INDETERMINATE, uncovered SHAs).
    uncovered_shas: List of uncovered commit SHAs the VERDICT keys on — i.e. the
                   CODE partition only (see bookkeeping_shas below). This is what
                   an operator is actually being asked to review.
    bookkeeping_shas: List of uncovered commit SHAs classified as ceremony
                   bookkeeping — every touched path under a ledger prefix
                   (state/, archive/, tasks/, cross-repo/) AND introducing no
                   file under state/handoffs/, so a handoff-AUTHORING commit is
                   never in here while a shipped_in stamp or pickup claim on the
                   same file is. Reported for visibility, never dropped, but does
                   NOT gate the verdict. See _classify_bookkeeping_shas. Empty by
                   default so
                   existing callers constructing CoverageResult positionally/by
                   keyword without this field keep working unchanged.
    planning_shas: List of uncovered commit SHAs classified PLANNING (see
                   _PLANNING_ARTIFACT_PATH_PREFIXES / _classify_bookkeeping_shas).
                   Deliberately a SUBSET of uncovered_shas, not disjoint from it
                   the way bookkeeping_shas is — AC9: PLANNING is not exempt from
                   review, only downgraded from a code review to a plan review,
                   so it stays counted toward the VERDICT and is never subtracted
                   here. This field exists so a consumer (e.g. C10's code-partition
                   ratio) can tell "owes a plan review" apart from "owes a code
                   review" without re-running the classifier: the CODE-only
                   uncovered count is len(uncovered_shas) - len(planning_shas), and
                   the code-partition denominator is
                   chain_commits - len(bookkeeping_shas) - len(planning_shas). Empty
                   by default, same additive-field property as bookkeeping_shas.
    dag_ordered_ancestry: DAG-mode only (empty in flat mode) — the baton
                   ancestry chain _derive_dag_chain_set walked, verbatim from
                   _DagChainResult.ordered_ancestry. Empty by default, same
                   additive-field property as bookkeeping_shas above.
    coverage_ratio: The code-partition coverage ratio (C10) — covered_code /
                   (covered_code + uncovered_code), where the code partition
                   excludes both bookkeeping_shas and planning_shas from
                   chain_commits (see planning_shas' docstring for the exact
                   arithmetic). 1.0 when the code partition is empty (no code
                   commits to cover — vacuously satisfied, mirrors the
                   pre-C10 empty-chain-set short-circuit). This is the number
                   a consumer reads instead of re-deriving it from
                   uncovered_shas/bookkeeping_shas/planning_shas by hand.
                   Also rendered in verdict_line so a CLI-only caller (never
                   touching the structured fields) still sees the number, not
                   just the verdict token. 0.0 by default (INDETERMINATE /
                   error paths never touch it), same additive-field property
                   as bookkeeping_shas above.
    dag_node_attribution: DAG-mode only (empty dict in flat mode) — {node_path:
                   _DagNodeAttribution} verbatim from
                   _DagChainResult.node_attribution, i.e. which commits belong
                   to which ancestry node. This is the datum an UNCOVERED
                   render needs to tag each uncovered commit with its
                   originating baton and print the ancestry chain without
                   recomputing segment membership (the "state exactly once"
                   property _derive_dag_chain_set's docstring requires). The
                   original renderer, run_coverage_gate's DAG-mode UNCOVERED
                   notes assembly, was removed by K-001 (2026-08-16, see
                   state/kill-ledger.md). Empty by default, same additive-field
                   property as bookkeeping_shas above.
    unattributable_shas: DAG-mode only (empty in flat mode), verbatim from
                   _DagChainResult.unattributable_shas (C2) -- commits seen
                   in a coverable node's Session-Id segment but excluded
                   from leg (b) because the seeding add-commit was judged a
                   bulk sweep. NEVER a member of uncovered_shas/chain_commits
                   /the verdict -- this is "in range, unattributable to THIS
                   chain", distinct from "this chain's inheritance", reported
                   for visibility so it is not mistaken for silently-dropped
                   work. Empty by default, same additive-field property as
                   bookkeeping_shas above.
    Negative-spec (hard-won):
      - UNCOVERED means "no covering trail record was found" -- it is AGNOSTIC to
        why. It does NOT distinguish "genuinely unreviewed" from "the caller never
        supplied review metadata this pass" (the latter makes ``review_trail.write``
        skip cleanly with ``no-review-metadata``, never writing a trail record for
        this gate to find). Check the sibling ``review_trail.write`` result before
        reading UNCOVERED as "review did not happen." See
        ``coordinator_core/ops/coverage_gate.py``'s module docstring for the full
        note and the originating 2026-07-22 incident.
    """

    verdict: str
    verdict_line: str
    chain_commits: int = 0
    covered: int = 0
    uncovered: int = 0
    exit_code: int = 0
    notes: List[str] = field(default_factory=list)
    uncovered_shas: List[str] = field(default_factory=list)
    bookkeeping_shas: List[str] = field(default_factory=list)
    planning_shas: List[str] = field(default_factory=list)
    coverage_ratio: float = 0.0
    dag_ordered_ancestry: List[str] = field(default_factory=list)
    dag_node_attribution: Dict[str, "_DagNodeAttribution"] = field(default_factory=dict)
    unattributable_shas: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DAG-mode UNCOVERED render — baton-ancestry inheritance disclosure
# ---------------------------------------------------------------------------

#: Labels assigned to ancestry nodes in chronological (oldest-first) order for
#: the UNCOVERED render below. Falls back to a numeric "N<i>" token past
#: len(_ANCESTRY_LABELS) — a real baton chain has never been observed anywhere
#: near 26 nodes deep, so this is a graceful-degradation floor, not a limit
#: this code expects to hit.
_ANCESTRY_LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _ancestry_label(index: int) -> str:
    """Return the display label for ancestry position `index` (0-based)."""
    if index < len(_ANCESTRY_LABELS):
        return _ANCESTRY_LABELS[index]
    return f"N{index}"


def _render_dag_ancestry_notes(
    ordered_ancestry: List[str],
    node_attribution: Dict[str, _DagNodeAttribution],
    closing_abs: str,
    uncovered_shas: List[str],
) -> List[str]:
    """Render the DAG-mode UNCOVERED baton-ancestry disclosure (AC3).

    Purpose (the incident this closes): a chain-terminal EM read a bare
    ``VERDICT=UNCOVERED`` plus a flat ``uncovered: <sha>`` list and concluded
    the uncovered commits were another session's problem — they were in fact
    the closing EM's own inherited baton ancestry, and the correct call was to
    add the missing review-trail coverage, not disclaim it. The gate was
    correct; its output gave no way to tell WHOSE inheritance the uncovered
    commits were without reading this module's source. This render is the
    fix: it makes the ancestry chain and per-commit baton origin explicit,
    every time, on UNCOVERED in DAG mode.

    Reads `node_attribution` (computed once, by _derive_dag_chain_set's Step
    3) rather than recomputing segment membership — see that dataclass's own
    "state exactly once" property and coverage.py's "Shape to avoid" note.

    Only nodes present in `node_attribution` are rendered (i.e. members of the
    fixpoint's closing_set — the coverable ancestry) — a blocked ancestor
    never reaches Step 3, contributes no commits to the chain, and has
    nothing to disclose here.

    A commit attributed to more than one node's segment (a possible but
    unobserved edge case — segments are not asserted disjoint) is tagged with
    the FIRST node in chronological order that claims it, and only there —
    each uncovered commit gets exactly one baton tag, never a double count.

    Deliberately does NOT print a commit subject line: this module does not
    fetch commit subjects anywhere (verified against the module's own git
    call sites before writing this render — see the dispatch brief's
    "verify resident data" instruction) and adding a `git log` spawn per
    render would be a regression under this engine's spawn-per-call budget.
    Each uncovered commit is tagged by its short SHA and originating baton
    label only.

    AC8: also appends the § "Honest accounting — the disclosed limit"
    disclosure, distilled to a line or two per the PM's ruling that it must
    not be buried. This is the single assembly point for that disclosure —
    every DAG-mode UNCOVERED consumer (the three entry points named in AC8)
    inherits it by reading `CoverageResult.notes` / the JSON-RPC `notes`
    field / `state/coverage/gate-result.json`'s `notes` key, all of which are
    populated from this function's return, so no per-veneer duplication is
    needed. Only fires here, i.e. only on DAG-mode UNCOVERED — never on
    COVERED (a limit disclosed on every run trains people to skip it).
    """
    lines: List[str] = []

    # Chronological order: ordered_ancestry is walk_forward's own
    # first-encounter order (closing handoff first, roots last — "terminal to
    # roots"); reversed here to present oldest-to-newest, matching how a
    # baton chain is actually read (predecessor before successor).
    chronological = [n for n in reversed(ordered_ancestry) if n in node_attribution]

    lines.append(
        f"These {len(uncovered_shas)} commit(s) are YOUR CHAIN'S inheritance — "
        "every ancestry node below fed a commit into this closing session's "
        "review obligation; none of them are another session's to disclaim."
    )
    lines.append("Baton ancestry (oldest to newest):")
    for i, node in enumerate(chronological):
        label = _ancestry_label(i)
        attrib = node_attribution[node]
        name = Path(node).stem
        deliv = attrib.deliverable_id or "no deliverable_id"
        marker = "  <- closing (you)" if node == closing_abs else ""
        if attrib.attribution_disagrees:
            marker += f"  <- picked up (authored by {attrib.authored_session_id})"
        lines.append(f"  [{label}] {name}    {deliv}{marker}")

    lines.append("uncovered, by originating baton:")
    claimed: Set[str] = set()
    for i, node in enumerate(chronological):
        label = _ancestry_label(i)
        attrib = node_attribution[node]
        for sha in sorted(uncovered_shas):
            if sha in attrib.shas and sha not in claimed:
                claimed.add(sha)
                lines.append(f"  [{label}]  {sha[:9]}")

    unclaimed = [sha for sha in uncovered_shas if sha not in claimed]
    if unclaimed:
        # Fail-visible, not fail-silent: every uncovered_shas member should be
        # a subset of the union of all node segments (that union is exactly
        # how chain_set was built) — a non-empty unclaimed set here means
        # something upstream is inconsistent, and hiding that inconsistency
        # would defeat the whole point of this render.
        lines.append(
            "  [?]  "
            f"{len(unclaimed)} uncovered commit(s) with no resolved originating "
            f"baton: {', '.join(sha[:9] for sha in unclaimed)}"
        )

    # AC8 / § "Honest accounting — the disclosed limit": this attribution is
    # trailer-derived (Deliverable-Id: ~5.5%, Session-Id: ~88.9% of historical
    # commits; older/hook-less commits carry neither) and over-includes rather
    # than under-includes (staging a chain's plan file acquires its
    # Deliverable-Id). Crediting is range-based, not per-SHA: a minted waiver
    # plus a close record covering the range credits every waived commit in
    # that range whether or not it was individually opened.
    lines.append(
        "NOTE: this attribution is trailer-derived and incomplete (a plan-file "
        "edit can acquire a chain's Deliverable-Id), and crediting is "
        "range-based, not per-SHA — see docs/wiki/coverage-gate-perf.md and "
        "docs/decisions/DR-245-gate-minted-chain-ancestry-waivers-supersede-in.md."
    )

    return lines
