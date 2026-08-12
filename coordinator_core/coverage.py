"""
coverage.py

Port of: review-coverage-core.sh (example-doctrine-repo c187f5b9, 2026-07-21) — SAFE_RANGE validator,
JSON/JSONL trail parser, verdict filter (pending excluded; ok/warn/blocked/
waived/absent included), per-record git rev-list union → reviewed_set, batched
git calls.
Port of: review-coverage-gate.sh (example-doctrine-repo d9ac8232, 2026-07-19) — arg-parse,
DAG-vs-flat mode, fixpoint, segment attribution -M100%, asymmetric scope
filter, INDETERMINATE-fails-verdict, verdict-line emit.

Asymmetric scope filter:
    chain_set is OPTIONALLY path-scoped via scope_paths.
    reviewed_set is NEVER path-scoped — all sessions' trail records credit toward
    coverage regardless of which paths those records were scoped to.

Inverted-intuition warning for _derive_dag_chain_set's fixpoint: EXCLUDING a
node from closing_set is the FAIL-OPEN direction, not the conservative one. A
blocker there is a deferral, not a rejection — the excluded ancestor's commits
simply never enter chain_commits, so they are never counted, never flagged
uncovered, and never waived. Deferral in a coverage gate is subtraction from
the denominator: "blocked" reads as "stricter" in almost every other gate
shape, but here a wider blocker set shrinks closing_set, which shrinks
chain_commits, which tilts the verdict toward COVERED. Any future change that
gates membership in closing_set must be checked against this direction before
it is assumed to be the safe one.

Three fidelity guards that a naive port silently drops — all three implemented here:
    (1) Session-Id UUID-shape validation BEFORE git grep interpolation.
        A Session-Id containing regex metacharacters (e.g. '.*') would over-match
        commits belonging to other sessions → false COVERED verdict. Guard: validate
        UUID shape; malformed → INDETERMINATE (never FALSE COVERED). See _UUID_RE
        and _derive_dag_chain_set segment-attribution section.
    (2) exit-0-with-empty-stdout → INDETERMINATE.
        A has-live-children check that signals "blocked" but cannot enumerate blockers
        is a contract violation — treat as INDETERMINATE rather than vacuously coverable
        (would set all_stale=1 vacuously → false COVERED). In the Python port: any
        exception from coordinator_core.archival.reverse_membership (including the C4-stub
        NotImplementedError or a runtime error) triggers INDETERMINATE.
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
    - The archival.reverse_membership body is filled by C4; NotImplementedError at call-time
      is expected in Wave 2 before C4 lands. Import resolves cleanly (C0 seam design).

Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § C3
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from coordinator_core.dag import CONTINUATION_EDGE_KINDS as _DAG_CONTINUATION_EDGE_KINDS
from coordinator_core.dag import walk_forward
from coordinator_core import session_attribution
from coordinator_core import chain_ancestry_waivers as _chain_ancestry_waivers

# Canonical frontmatter-key resolution. `coordinator_core.frontmatter.primitives`
# is the single home for the `^key:` pattern — see the negative-specs on
# _parse_handoff_consumed_by / _parse_handoff_deliverable_id below for why a
# local `\s*`-padded fork is a break-class defect, not a style preference.
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.claim_state import resolve_claim_state

# Import archival seam (C0 interface pin; C4 fills the body).
# At import time this resolves cleanly — NotImplementedError surfaces only at call-time
# when _derive_dag_chain_set runs the fixpoint. That is the documented contract.
from coordinator_core.archival import reverse_membership

from coordinator_core.liveness import resolve_live_session_ids
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
#: (covered_code + uncovered_code), see run_coverage_gate's verdict assembly)
#: below which the gate WARNs instead of reporting COVERED. A 100%-or-block
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


def _resolve_coverage_ratio_threshold() -> float:
    """Read COORDINATOR_COVERAGE_RATIO_THRESHOLD if set and valid, else
    DEFAULT_COVERAGE_RATIO_THRESHOLD. Isolated so callers needing the
    resolved threshold value (e.g. a notes render) don't re-parse env
    themselves."""
    raw = os.environ.get("COORDINATOR_COVERAGE_RATIO_THRESHOLD", "")
    if not raw:
        return DEFAULT_COVERAGE_RATIO_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_COVERAGE_RATIO_THRESHOLD
    if not (0.0 <= value <= 1.0):
        return DEFAULT_COVERAGE_RATIO_THRESHOLD
    return value


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
    on-disk records use literal "HEAD" specifically; see example-doctrine-repo
    state/review-trail/*.json), and rejecting them would risk excluding
    legitimately-scoped historical records this fix has no evidence exist.
    """
    sep = "..." if "..." in sha_range else (".." if ".." in sha_range else None)
    if sep is None:
        return bool(_STORED_HEAD_ENDPOINT_RE.match(sha_range))
    left, right = sha_range.split(sep, 1)
    return bool(_STORED_HEAD_ENDPOINT_RE.match(left) or _STORED_HEAD_ENDPOINT_RE.match(right))


#: UUID shape pattern for Session-Id validation (fidelity guard 1).
#: ^[0-9a-fA-F][0-9a-fA-F-]+[0-9a-fA-F]$
_UUID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]+[0-9a-fA-F]$")

#: Deliverable-Id shape pattern (fidelity guard 1, deliverable-attribution
#: variant). Mirrors _UUID_RE's purpose: a malformed deliverable_id containing
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

#: Leg (b) (the deliverable-attribution legacy-history fallback in
#: `_derive_dag_chain_set` Step 3) fans out from a node's add-commit
#: Session-Id to EVERY commit that session ever made on HEAD carrying no
#: (walked) Deliverable-Id. That is correct when the add-commit is really
#: this node's own author committing it in the ordinary way, but wrong when
#: the add-commit is a BULK SWEEP — some other session's routine
#: multi-hundred-file safety/auto commit that happened to include this one
#: handoff among many unrelated files. A bulk sweep is not evidence that the
#: sweeping session authored (or should review-inherit) this node's work; it
#: is evidence the sweep merely passed over the file.
#:
#: File-touch-count on the add-commit is the chosen structural signal, not
#: the commit subject: an ordinary handoff add-commit in this corpus touches
#: a small number of files (the handoff itself, occasionally an index or a
#: sibling artifact); the observed sweep commits touch 56-290. Subject-string
#: matching ("safety-commit", "auto-commit:") is not used deliberately — it
#: is corpus-specific prose that drifts, whereas file count is intrinsic git
#: structure available on every commit regardless of author or convention.
#: `deliverable_id`-on-add-commit was also considered and rejected: legacy
#: (pre-trailer) add-commits legitimately lack a Deliverable-Id trailer too,
#: so requiring one would falsely suppress leg (b) for exactly the
#: genuinely-authored history it exists to keep attributing.
#:
#: False-negative risk: a legitimately-authored add-commit that happens to
#: bundle more than this many files (e.g. a large batched multi-artifact
#: close) has its leg (b) segment suppressed too, under-crediting that
#: chain. This is the accepted direction — § "fail toward current
#: behaviour, not away from it" only binds when suppressing does not corrupt
#: leg (a) or turn the node INDETERMINATE; under-crediting a legitimately
#: bulky close is a narrower, correctable-on-review miss, not a fabricated
#: over-attribution. Leg (a) (Deliverable-Id-stamped commits) is completely
#: unaffected by this threshold in either direction.
_BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD = 20


def _add_commit_touched_file_count(add_sha: str, repo_root: str) -> Optional[int]:
    """Return the number of files touched by commit `add_sha`, or None if the
    count cannot be determined (git failure / unparsable output) — the caller
    treats None the same as "below threshold" (fail toward current
    behaviour: an unresolvable count must never itself suppress leg (b)).

    Uses `git show --stat --format=` and counts stat lines carrying the
    `" | "` file/changes separator, which the trailing "N files changed, ..."
    summary line never contains — avoids an off-by-one from that line.
    """
    rc, out, _ = _run(
        ["git", "show", "--stat", "--format=", add_sha],
        cwd=repo_root,
    )
    if rc != 0:
        return None
    return sum(1 for line in out.splitlines() if " | " in line)


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

# Max concurrent `git rev-list` spawns in build_reviewed_set Phase 2. The distinct
# ranges are independent read-only shell-outs; on Windows each git spawn costs ~90ms
# (CREATE_NO_WINDOW), so resolving N records serially is N×90ms. Bounded fan-out keeps
# wall-clock ~= one spawn without launching an unbounded swarm of git.exe at once.
_REVLIST_MAX_WORKERS = 16


def _run(
    cmd: List[str], cwd: Optional[str] = None, input_text: Optional[str] = None
) -> Tuple[int, str, str]:
    """Run cmd; return (returncode, stdout.strip(), stderr). Never raises.

    `input_text`, when given, is fed to the child's stdin (used by callers
    like `git diff-tree --stdin`); otherwise stdin is DEVNULL as before — nt:
    inherited invalid stdin + CREATE_NO_WINDOW hangs _execute_child.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
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


def _chain_ancestry_waived_shas(cwd: str, reading_chain_id: Optional[str]) -> FrozenSet[str]:
    """Read-side lookup of gate-minted chain-ancestry waivers (C1,
    docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md),
    SCOPED to exactly the chain identified by ``reading_chain_id`` — this is
    the exact-chain-identity-match form of the two read-side scope options
    AC3 offers (the weaker alternative — honouring a waiver for any record
    whose session is merely an ancestry NODE of the minting chain — would
    require re-deriving or re-consulting the minting chain's whole walked
    ``closing_set`` at read time; this function has no such derivation in
    hand and does not attempt to reconstruct one). Exact match needs only
    the reading record's own chain identity, already available at every
    call site below.

    ``reading_chain_id`` is the READING trail record's own ``session_id``.
    For a record whose ``scope`` is in ``_FOREIGN_STRIPPED_SCOPES``
    (session, chain, workstream-close-auto — see that constant), the
    session that WROTE the record is, for a ``scope="chain"`` record, the
    same session that ran ``/workstream-complete`` and is therefore the
    same ``closing_session_id`` the gate passed to ``_derive_dag_chain_set``
    when it HALTed and minted the waiver (see
    ``coordinator_core.ops.coverage_gate``, C2). A waiver minted for chain
    A's ``closing_session_id`` therefore matches a record ONLY when that
    record's own ``session_id`` is chain A's — a record belonging to a
    DIFFERENT chain's close (a different ``session_id``) sees an EMPTY
    result here, never chain A's waivers. This is what keeps this source
    narrower than DR-243's presence-only ``pm-vouches`` (AC3's named
    scope-mismatch test: a waiver minted for chain A must not relax the
    strip for a record belonging to chain B).

    ``reading_chain_id=None`` (a record with no ``session_id`` at all)
    resolves no chain — returns the empty set; there is no chain identity
    to match against, so no gate-minted waiver can apply to it.

    Delegates the on-disk shape entirely to ``chain_ancestry_waivers.py``
    (this module's shared home for the new artifact, per the plan's
    Anti-scope allowance for one dirname/path-builder module) — this
    function's own job is only the read-side POLICY (exact chain-identity
    match), not the storage shape.
    """
    if not reading_chain_id:
        return frozenset()
    return _chain_ancestry_waivers.chain_ancestry_waived_shas(cwd, reading_chain_id)


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
    discriminator.md § C1): a SECOND source was introduced — a per-SHA,
    per-chain waiver minted by the coverage GATE at HALT
    (`coordinator_core.ops.coverage_gate`, C2), stored under
    `state/review-trail/chain-ancestry-waivers/<chain_id>/`. Unlike the
    PM-vouch set (honoured for ANY record regardless of which session
    wrote it, presence-only), the gate-minted set is honoured ONLY when
    `own_session_id` (this record's own chain identity) matches the
    `chain_id` that minted the waiver — see `_chain_ancestry_waived_shas`'s
    docstring for why exact match, not ancestry-node membership, was
    chosen, and for the AC3 scope-mismatch test this narrowing exists to
    satisfy (a waiver minted for chain A must not relax the strip for a
    record belonging to chain B).

    REMOVED (docs/plans/2026-08-08-vouch-free-review-coverage-gates.md §
    C2/C3): the PM-vouch waiver source (`_pm_vouched_waiver_shas`,
    `state/review-trail/pm-vouches/`) has been deleted outright — a
    foreign-attributed commit is now waived ONLY via the gate-minted
    chain-ancestry source above. No waiver can be minted from this read
    side or from any caller of this function — it is a fact this function
    only OBSERVES.
    """
    try:
        foreign = session_attribution.trailer_foreign_shas(
            sha_range, own_session_id, cwd, cache, run=_run,
        )
    except session_attribution.GitLogFailed as exc:
        raise _ForeignSessionLookupError(str(exc)) from exc
    if not foreign:
        return foreign
    waived = _chain_ancestry_waived_shas(cwd, own_session_id)
    if not waived:
        return foreign
    return foreign - waived


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
# bookkeeping partition (never silently dropping it) — see run_coverage_gate's
# split-site comment for how the two partitions are threaded into the result.
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
#: `shipped_in`-stamp, pickup-assemble-claim, and `state/handoff-tracker.md`
#: commits back to CODE, which is the permanent false-UNCOVERED tail
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
    uncovered-sha sets within one run_coverage_gate invocation reuse work.
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


# ---------------------------------------------------------------------------
# Single-graph-walk helpers (durable perf lever — replaces the per-range git
# rev-list fan-out with ONE `git rev-list --parents` graph build + in-memory
# per-range set math). See docs and the spike verdict record
# state/handoffs/2026-07-15_082943_spike-result-coverage-gate-graph-walk.md.
# ---------------------------------------------------------------------------

#: Splits a rev endpoint token ("sha", "sha^", "sha^2", "sha~3", "sha^~2") into
#: its base SHA and the trailing ^/~ op-chain, for in-memory ancestry resolution.
_ENDPOINT_SUFFIX = re.compile(r"^([0-9A-Za-z_/.]+?)((?:[~^][0-9]*)*)$")

#: Sentinel: git cannot resolve the ref at all (a genuinely bad ref). Only ever
#: returned under on_record_error='fail', where it must raise.
_ENDPOINT_UNRESOLVED = object()

#: Sentinel: the endpoint resolves to an ancestor of the graph_range window base
#: (older than the chain base). Its reach_chain is provably ∅ — precondition:
#: the resolved commit must be an ancestor-of-base (checked via
#: _classify_out_of_window), not merely absent from parent_map. See
#: MAJOR-1/MAJOR-2 fix (state/review-trail/findings/
#: 2026-07-15-the Staff Engineer-coverage-graph-walk-correctness.md) — conflating "absent
#: from parent_map" with "ancestor of base" was the false-COVERED bug.
_ENDPOINT_OUT_OF_WINDOW = object()

#: Sentinel: the endpoint is a valid, git-resolvable ref that is NEITHER
#: in-window NOR an ancestor of the graph_range base (e.g. a side-branch tip
#: descended from an in-window commit but never merged in). The in-window
#: parent_map is insufficient to compute its reach — the caller must fall back
#: to a per-range `git rev-list` spawn for that range alone (Review: the Staff Engineer —
#: MAJOR-2/MINOR-3 fix, false-COVERED/false-UNCOVERED on a correctness gate).
_ENDPOINT_FOREIGN = object()


def _build_parent_map(cwd: str, graph_range: str) -> Optional[Dict[str, List[str]]]:
    """Build a child→parents map from ONE `git rev-list --parents <graph_range>`.

    Built WITH merges (no --no-merges) so merge commits are present as intermediate
    graph nodes and BFS can follow all parents. Returns None on git failure, so the
    caller degrades gracefully to the per-range fan-out.
    """
    rc, out, _ = _run(["git", "rev-list", "--parents", graph_range], cwd=cwd)
    if rc != 0:
        return None
    parent_map: Dict[str, List[str]] = {}
    for line in out.splitlines():
        parts = line.split()
        if parts:
            parent_map[parts[0]] = parts[1:]
    return parent_map


#: A pure hex token (full or abbreviated SHA). Non-hex tokens (HEAD, branch names,
#: origin/main, tags) are symbolic refs that need git to resolve.
_HEX_TOKEN = re.compile(r"^[0-9a-fA-F]{4,40}$")

#: A FULLY-resolved objectname (never an abbreviated input token). Distinct from
#: _HEX_TOKEN above (deliberately permissive, {4,40}, for abbreviated *input*
#: tokens) — this one gates what counts as a *resolved* SHA coming back from
#: `git cat-file --batch-check`, so it is intentionally narrower.
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")

#: Object types `--batch-check=%(objectname) %(objecttype)` can report for a
#: SUCCESSFUL resolution. Used as a positive allowlist rather than enumerating
#: failure markers ("missing", "ambiguous", ...) by name, so any marker git
#: adds in the future (this module has empirically observed both) falls to
#: None uniformly instead of needing to be named here.
_GIT_OBJECT_TYPES = frozenset({"commit", "tree", "blob", "tag"})


def _range_base(range_str: str) -> str:
    """The left (base) token of a `L..R` or `L...R` range string, unresolved."""
    sep = "..." if "..." in range_str else ".."
    return range_str.split(sep, 1)[0]


class _OutOfWindowCache:
    """Per-call cache for out-of-window endpoint resolution (durable perf lever —
    see the coordinator dispatch that added this class: distinct trail records
    frequently cite the SAME base token or the same out-of-window SHA, and the
    original implementation re-spawned git for each occurrence).

    Two independent caches, both scoped to one build_reviewed_set() call:

      • `resolved` — memoizes _resolve_base's return value BY BASE TOKEN (hex or
        symbolic alike). _resolve_base is a pure function of (token, parent_map,
        on_record_error, graph_base), all constant within one call, so caching by
        token alone is sound and collapses repeated identical base tokens
        (common — many records share a base SHA) to one resolution total.
      • `ancestor_of_base` — lazily built ONCE via a SINGLE `git rev-list
        <graph_base>` (the full ancestor closure of the window base), replacing
        the old one-`git merge-base --is-ancestor`-spawn-per-endpoint classify
        step with in-memory set membership. Built lazily: a corpus with zero
        out-of-window endpoints never triggers this spawn. On git failure,
        `ancestor_of_base()` returns None and the caller degrades to the
        original per-SHA `merge-base --is-ancestor` probe (still correct, just
        not the fast path).

    A third cache, `hex_existence`, holds the result of a BATCHED existence
    probe (see `preload_hex_tokens` / `_batch_check_hex_tokens`) — a real
    corpus commonly cites many DISTINCT out-of-window hex tokens (not shared
    base SHAs, where `resolved` alone already collapses the spawn to one), so
    `_reviewed_via_graph_walk` pre-resolves ALL of them in one
    `git cat-file --batch-check` spawn before the per-range loop runs, rather
    than one `git rev-parse` spawn per distinct token.
    """

    def __init__(self) -> None:
        self.resolved: Dict[str, Any] = {}
        self._ancestor_of_base: Optional[FrozenSet[str]] = None
        self._ancestor_built = False
        self.hex_existence: Dict[str, Optional[str]] = {}

    def ancestor_of_base(self, cwd: str, graph_base: str) -> Optional[FrozenSet[str]]:
        if not self._ancestor_built:
            rc, out, _ = _run(["git", "rev-list", graph_base], cwd=cwd)
            self._ancestor_of_base = (
                frozenset(s.strip() for s in out.splitlines() if s.strip())
                if rc == 0 else None
            )
            self._ancestor_built = True
        return self._ancestor_of_base

    def preload_hex_tokens(self, tokens: "Set[str]", cwd: str) -> None:
        """Batch-resolve every not-yet-cached token in `tokens` (hex base tokens
        known, by prefix-scan against parent_map, to need an out-of-window
        existence probe) via ONE `git cat-file --batch-check` spawn. See
        `_batch_check_hex_tokens` for the mechanics; a no-op if every token is
        already cached (e.g. re-entrant calls across ranges that share a token)."""
        uncached = sorted(t for t in tokens if t not in self.hex_existence)
        if not uncached:
            return
        self.hex_existence.update(_batch_check_hex_tokens(uncached, cwd))


def _out_of_window_hex_tokens(
    sha_ranges: "Set[str]", parent_map: Dict[str, List[str]]
) -> Set[str]:
    """Every distinct hex BASE token cited by `sha_ranges` (across both endpoints
    of each range) that a zero-spawn prefix-scan against `parent_map` already
    shows is NOT in-window — i.e. exactly the tokens `_probe_out_of_window`
    would otherwise resolve with one `git rev-parse` spawn EACH.

    Pure in-memory; the caller feeds the result to
    `_OutOfWindowCache.preload_hex_tokens` for a single batched resolution.

    Shared by BOTH graph-walk consumers — `_reviewed_via_graph_walk` (the
    crediting path) and `_make_chain_range_resolver` (the open-review-loop
    diagnosis path). It is deliberately ONE helper rather than an inline scan
    per consumer: the second consumer originally had no pre-scan at all, and
    that omission alone cost 1402 `git rev-parse` spawns / ~30s on a
    1700-record review-trail corpus, which pushed `coverage.gate` past the
    engine's 30s dispatch timeout so it returned no verdict at all. A
    corpus-scaled per-token spawn in either consumer is the defect shape; a
    new consumer must call this, not re-derive it.

    Negative-spec: does NOT decide whether a token is genuinely out-of-window —
    only that the in-window graph cannot answer, so a probe is owed. The
    ancestor-of-base vs foreign classification stays entirely in
    `_classify_out_of_window`.
    """
    needs_probe: Set[str] = set()
    for sha_range in sha_ranges:
        sep = "..." if "..." in sha_range else (".." if ".." in sha_range else None)
        if sep is None:
            continue
        left, right = sha_range.split(sep, 1)
        for token in (left, right):
            m = _ENDPOINT_SUFFIX.match(token)
            base = m.group(1) if m else token
            if not _HEX_TOKEN.match(base):
                continue
            b = base.lower()
            if not any(k.startswith(b) for k in parent_map):
                needs_probe.add(base)
    return needs_probe


def _batch_check_hex_tokens(tokens: List[str], cwd: str) -> Dict[str, Optional[str]]:
    """Resolve existence + canonical full SHA for MANY hex tokens (full or
    abbreviated) in ONE `git cat-file --batch-check` spawn, feeding all tokens
    via stdin. Returns {token: full_sha_or_None} — None means git could not
    resolve the token (missing object / bad ref).

    Deliberately NOT built on `_run`: `--batch-check` is the one git subcommand
    in this module that legitimately needs to WRITE to stdin (one token per
    line), which `_run`'s own `stdin=subprocess.DEVNULL` (the pinned Windows
    hang fix — left untouched by design) cannot support. This wrapper mirrors
    `_run`'s portability flag (`_NO_CONSOLE`) without inheriting its stdin
    behavior, so it stays equally safe on Windows without touching `_run`
    itself.

    Empirically verified (Review: code-reviewer item 3 + EM follow-up,
    2026-07-28) `--batch-check` output shape, since the zip pairing below
    depends on it: exactly ONE stdout line per stdin token, in input order —
    ambiguity diagnostics and hints go to stderr and never contaminate stdout.
    A genuinely ambiguous short prefix (e.g. `332c`) still comes back as
    "<token> ambiguous" on stdout, which is why resolution is gated on
    `_GIT_OBJECT_TYPES` + `_FULL_SHA` below rather than treating any non-empty
    `parts[0]` as resolved — an abbreviated/ambiguous token must never be
    handed back to the caller as if it were a resolved SHA.
    """
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input="\n".join(tokens) + "\n",
            cwd=cwd,
            capture_output=True,
            text=True,
            **_NO_CONSOLE,
        )
        lines = result.stdout.splitlines() if result.returncode == 0 else []
    except Exception:
        lines = []
    out: Dict[str, Optional[str]] = {}
    for token, line in zip(tokens, lines):
        parts = line.split()
        resolved = (
            parts[0]
            if len(parts) == 2 and parts[1] in _GIT_OBJECT_TYPES and _FULL_SHA.match(parts[0])
            else None
        )
        out[token] = resolved
    for token in tokens[len(lines):]:
        out[token] = None
    return out


#: A range whose left endpoint is the right endpoint's sole immediate parent
#: expression (`A^..A` or `A~1..A`, hex SHA only — the shape trail records
#: actually carry, per fleet doctrine of one per-commit record per session on
#: a shared branch). Group 1 is the shared hex token.
_SINGLE_COMMIT_RANGE_RE = re.compile(r"^([0-9a-fA-F]{4,40})(?:\^|~1)\.\.\1$")


def _batch_parent_counts(
    tokens: List[str], cwd: str
) -> Dict[str, Optional[Tuple[str, int]]]:
    """Resolve, for MANY hex commit tokens in TWO batched spawns, each one's FULL
    (canonical, 40-char) SHA and how many parents it has. Returns
    {token: (full_sha, parent_count) or None}, where None means git could not
    resolve the token at all (bad ref / missing object) — the caller must treat
    that as "cannot short-circuit", never as "zero parents" (a root commit is a
    REAL parent_count of 0, distinct from unresolvable).

    The full SHA is returned alongside the count, not just the count, because a
    caller's `token` may itself be an ABBREVIATED sha_range endpoint — crediting
    the short input token as though it were the resolved commit would silently
    fail every downstream set-membership check against full 40-char SHAs (chain_set,
    intersect_shas), which is the exact defect an earlier draft of this function
    shipped with (see below).

    Two spawns, not one, and NEITHER trusts output-vs-input positional order:

      1. `_batch_check_hex_tokens` (`git cat-file --batch-check`, already proven
         order-preserving — see that function's own empirically-verified
         docstring) resolves token -> full_sha.
      2. `git rev-list --no-walk --parents --stdin`, fed the resolved full SHAs,
         for parent counts. Its output is parsed into a dict KEYED BY THE FULL
         SHA EACH LINE REPORTS (`parts[0]`), never by input position — an earlier
         draft of this function assumed `--stdin` preserves input order the way
         `--batch-check` does; empirically it does NOT (reorders, apparently by
         commit topology/date), and a positional `zip(tokens, lines)` silently
         paired each token with the WRONG commit's parent count — a false
         `parent_count == 1` credited a completely unrelated SHA as this range's
         sole member (verified live on this repo's own corpus: token `0d4afc29c`
         paired against a different commit's output line). Keying by the SHA the
         line itself names is immune to whatever order git chooses to emit in.

    Fed via stdin (`--stdin`), not argv, mirroring `_dag_frontier_ancestry`'s own
    stdin-feed rationale — a corpus with thousands of distinct single-commit
    ranges must not risk Windows' 32K command-line ceiling.

    A failure in EITHER spawn (rc != 0) degrades every token in this batch to
    None (cannot short-circuit — never silently assign a wrong parent count).

    Purely a resolution + parent-count PROBE — never a reachability query, and
    never combines these commits into one positive/negative rev-list the way
    `build_reviewed_set`'s own negative-spec forbids (see that function's
    docstring: batching several `A^..A` ranges into one combined rev-list call
    computes reachable(positives) \\ reachable(negatives), which is a DIFFERENT
    and wrong operation). `--no-walk --parents` never subtracts one commit's
    reachability from another's — each output line describes only its own
    commit's own immediate parents, so this stays outside that negative-spec.
    """
    if not tokens:
        return {}
    resolved = _batch_check_hex_tokens(tokens, cwd)
    full_shas = sorted({sha for sha in resolved.values() if sha is not None})
    if not full_shas:
        return {token: None for token in tokens}
    # Deliberately NOT built on `_run` — same rationale as `_batch_check_hex_tokens`
    # above: this spawn legitimately needs to WRITE to stdin (one token per line),
    # which `_run`'s pinned `stdin=subprocess.DEVNULL` (the Windows hang fix) cannot
    # support. Mirrors `_run`'s portability flag (`_NO_CONSOLE`) without inheriting
    # its stdin behavior.
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--no-walk", "--parents", "--stdin"],
            input="\n".join(full_shas) + "\n",
            cwd=cwd,
            capture_output=True,
            text=True,
            **_NO_CONSOLE,
        )
        rc, out = proc.returncode, proc.stdout
    except Exception:
        rc, out = 1, ""
    if rc != 0:
        return {token: None for token in tokens}
    parent_counts_by_sha: Dict[str, int] = {}
    for line in out.strip().splitlines():
        parts = line.split()
        if parts:
            parent_counts_by_sha[parts[0]] = len(parts) - 1
    result: Dict[str, Optional[Tuple[str, int]]] = {}
    for token in tokens:
        full_sha = resolved.get(token)
        if full_sha is not None and full_sha in parent_counts_by_sha:
            result[token] = (full_sha, parent_counts_by_sha[full_sha])
        else:
            result[token] = None
    return result


def _classify_out_of_window(
    resolved_sha: str, graph_base: str, cwd: str, oow_cache: "_OutOfWindowCache"
) -> Any:
    """A concrete SHA that git resolved but is absent from parent_map. Distinguish
    "ancestor of the graph_range window base" (reach_chain is provably ∅ —
    _ENDPOINT_OUT_OF_WINDOW) from "foreign" (a valid ref off the window's lineage,
    e.g. an abandoned side-branch tip — the in-window graph cannot compute its
    reach, so the caller must fall back to a per-range git rev-list spawn —
    _ENDPOINT_FOREIGN). Classified via `oow_cache`'s lazily-built ancestor-of-base
    set (one `git rev-list <graph_base>` total, ANY number of candidates tested by
    in-memory membership — Review: the Staff Engineer, MAJOR-2/MINOR-3 fix's correctness
    preserved, just no longer one `merge-base --is-ancestor` spawn per candidate).
    Falls back to the original per-SHA merge-base probe only if the ancestor-set
    build itself failed (graceful degradation, not a correctness change)."""
    ancestor_set = oow_cache.ancestor_of_base(cwd, graph_base)
    if ancestor_set is not None:
        return _ENDPOINT_OUT_OF_WINDOW if resolved_sha in ancestor_set else _ENDPOINT_FOREIGN
    rc, _, _ = _run(["git", "merge-base", "--is-ancestor", resolved_sha, graph_base], cwd=cwd)
    return _ENDPOINT_OUT_OF_WINDOW if rc == 0 else _ENDPOINT_FOREIGN


def _resolve_base(
    base: str,
    parent_map: Dict[str, List[str]],
    cwd: str,
    on_record_error: str,
    symref_cache: Dict[str, Any],
    graph_base: str,
    oow_cache: "_OutOfWindowCache",
) -> Any:
    """Resolve an endpoint's BASE token (before any ^/~ ops) to a concrete in-window SHA.

    Returns the full in-window SHA (a parent_map key), _ENDPOINT_OUT_OF_WINDOW (the ref
    is an ancestor of graph_base → reach_chain ∅), _ENDPOINT_FOREIGN (a valid ref off
    the window's lineage — needs a per-range fan-out fallback), or _ENDPOINT_UNRESOLVED
    (bad ref, fail-mode only).

    Memoized whole-function BY TOKEN in `oow_cache.resolved` (see that class's
    docstring) — the three-shape dispatch below only actually runs on a cache miss.

    Three token shapes, in cost order:
      • hex (full or abbreviated SHA) → prefix-match against the in-window graph keys,
        zero spawns. A unique match is the in-window SHA; zero matches means the SHA is
        out-of-window/foreign/bad (see _probe_out_of_window); >1 (vanishingly rare
        within a single window) falls back to a probe.
      • symbolic ref (HEAD, branch, origin/main, tag) → resolve via `git rev-parse`,
        CACHED per token, so the ubiquitous `<sha>..HEAD` records cost ONE spawn total,
        not one per record.
    """
    if base in oow_cache.resolved:
        return oow_cache.resolved[base]
    result = _resolve_base_uncached(
        base, parent_map, cwd, on_record_error, symref_cache, graph_base, oow_cache
    )
    oow_cache.resolved[base] = result
    return result


def _resolve_base_uncached(
    base: str,
    parent_map: Dict[str, List[str]],
    cwd: str,
    on_record_error: str,
    symref_cache: Dict[str, Any],
    graph_base: str,
    oow_cache: "_OutOfWindowCache",
) -> Any:
    """The actual resolution logic behind _resolve_base's per-token cache — see that
    function's docstring for the token-shape dispatch and return-value contract."""
    if _HEX_TOKEN.match(base):
        b = base.lower()
        matches = [k for k in parent_map if k.startswith(b)]  # git keys are lowercase hex
        if len(matches) == 1:
            # Known divergence (Review: the Staff Engineer — MINOR-4, documented rather than
            # fixed): this confirms the prefix is unique WITHIN the in-window graph
            # only. A prefix unique in-window but ambiguous repo-wide (matching some
            # out-of-window commit too) would make git's own `git rev-list
            # <abbrev>..R` fail with "fatal: ambiguous argument" (fan-out → skip),
            # while the graph-walk resolves the single in-window match and proceeds.
            # This is a rare, narrow divergence (requires a genuine short-prefix
            # collision across in-window and out-of-window commits) and — unlike
            # MAJOR-1/2 — is NOT a false-COVERED path: the in-window match is a real
            # commit the graph-walk correctly reasons about; the fan-out's failure
            # mode here is overly conservative (skip), not unsafe. Fixing it would
            # cost a `git rev-parse` probe on every abbreviated-hex endpoint (the
            # common case this function is optimized to keep spawn-free), which is
            # disproportionate to a divergence that never leaks unreviewed code
            # through the gate. Left undone by design; revisit only if trail records
            # are observed to actually collide (see MINOR-4 in
            # state/review-trail/findings/2026-07-15-the Staff Engineer-coverage-graph-walk-correctness.md).
            return matches[0]
        if not matches:
            return _probe_out_of_window(base, cwd, on_record_error, graph_base, oow_cache)
        return _probe_in_window(base, parent_map, cwd, on_record_error, graph_base, oow_cache)  # ambiguous prefix
    # symbolic ref — resolve once, cache the full SHA (or None for a bad ref)
    if base not in symref_cache:
        rc, out, _ = _run(["git", "rev-parse", "--verify", "--quiet", base], cwd=cwd)
        symref_cache[base] = out if (rc == 0 and out) else None
    full = symref_cache[base]
    if full is None:
        return _ENDPOINT_UNRESOLVED if on_record_error == "fail" else _ENDPOINT_OUT_OF_WINDOW
    if full in parent_map:
        return full
    return _classify_out_of_window(full, graph_base, cwd, oow_cache)


def _probe_out_of_window(
    token: str, cwd: str, on_record_error: str, graph_base: str, oow_cache: "_OutOfWindowCache"
) -> Any:
    """A token that matched no in-window SHA. Resolved via `oow_cache.hex_existence`
    if `_reviewed_via_graph_walk`'s upfront batch pre-scan already resolved it
    (the common case — one `git cat-file --batch-check` spawn for every distinct
    out-of-window token in the whole call); falls back to an individual
    `git rev-parse` spawn only when called outside that pre-scan (e.g. direct
    unit-test call, or a token the pre-scan didn't anticipate). Either way,
    distinguishes: a bad ref → _ENDPOINT_UNRESOLVED (fail-mode) /
    _ENDPOINT_OUT_OF_WINDOW (skip-mode, matching the fan-out's whole-range skip);
    a resolvable ref → classified via _classify_out_of_window as ancestor-of-base
    (∅) or foreign (fallback fan-out)."""
    if token in oow_cache.hex_existence:
        out = oow_cache.hex_existence[token]
    else:
        rc, rp_out, _ = _run(["git", "rev-parse", "--verify", "--quiet", token], cwd=cwd)
        out = rp_out if (rc == 0 and rp_out) else None
    if not out:
        return _ENDPOINT_UNRESOLVED if on_record_error == "fail" else _ENDPOINT_OUT_OF_WINDOW
    return _classify_out_of_window(out, graph_base, cwd, oow_cache)


def _probe_in_window(
    token: str,
    parent_map: Dict[str, List[str]],
    cwd: str,
    on_record_error: str,
    graph_base: str,
    oow_cache: "_OutOfWindowCache",
) -> Any:
    """Disambiguate a prefix that matched >1 in-window SHA via `git rev-parse` (rare)."""
    rc, out, _ = _run(["git", "rev-parse", "--verify", "--quiet", token], cwd=cwd)
    if rc == 0 and out:
        return out if out in parent_map else _classify_out_of_window(out, graph_base, cwd, oow_cache)
    return _ENDPOINT_UNRESOLVED if on_record_error == "fail" else _ENDPOINT_OUT_OF_WINDOW


def _resolve_endpoint(
    token: str,
    parent_map: Dict[str, List[str]],
    cwd: str,
    on_record_error: str,
    symref_cache: Dict[str, Any],
    graph_base: str,
    oow_cache: "_OutOfWindowCache",
) -> Any:
    """Resolve a rev token (sha, HEAD, branch, sha^, sha^N, sha~N, or a chain) to a
    concrete in-window SHA for the graph walk, or an OUT_OF_WINDOW / FOREIGN /
    UNRESOLVED sentinel.

    Resolves the base token (symbolic ref or abbreviated/full SHA — see _resolve_base),
    then walks the ^/~ ops through the in-memory parent map (zero spawns). An op that
    steps onto a SHA absent from the map has walked out of the window along a REAL
    parent-map edge (ops only go to older commits, never re-enter) — that is always an
    ancestor of the window base by construction of parent_map, so OUT_OF_WINDOW (∅) is
    provably correct here (no foreign case possible mid-walk — Review: the Staff Engineer,
    MAJOR-1/2 fix confirmed this branch is sound; only the initial base-token
    resolution needs the ancestor-of-base-vs-foreign distinction, in _resolve_base).
    """
    m = _ENDPOINT_SUFFIX.match(token)
    if not m:
        # No hex-suffix shape — treat the whole token as a symbolic base (no ops).
        return _resolve_base(token, parent_map, cwd, on_record_error, symref_cache, graph_base, oow_cache)
    base, suffix = m.group(1), m.group(2)
    cur = _resolve_base(base, parent_map, cwd, on_record_error, symref_cache, graph_base, oow_cache)
    if cur is _ENDPOINT_OUT_OF_WINDOW or cur is _ENDPOINT_UNRESOLVED or cur is _ENDPOINT_FOREIGN:
        return cur  # ops on a non-in-window base only go older → sentinel is final
    for op in re.findall(r"[~^][0-9]*", suffix):
        if cur not in parent_map:
            return _ENDPOINT_OUT_OF_WINDOW  # walked out of window (older) → ∅
        parents = parent_map[cur]
        kind, num = op[0], op[1:]
        n = int(num) if num else 1
        if kind == "^":
            if n == 0:
                continue  # X^0 == X
            if n > len(parents):
                # ^N beyond this commit's parent count is ALWAYS an invalid git ref
                # (git itself fails to resolve it — never a legitimate ancestor).
                # MAJOR-1 fix: skip-mode must treat this as UNRESOLVED (whole-range
                # skip, matching git's rc!=0), never OUT_OF_WINDOW (which would
                # leave the range's other endpoint's entire reach uncontested —
                # false COVERED). No probe needed: the ref is unconditionally bad.
                return _ENDPOINT_UNRESOLVED
            cur = parents[n - 1]
        else:  # "~N" — follow the first parent N times
            for _ in range(n):
                if cur not in parent_map or not parent_map[cur]:
                    return _ENDPOINT_OUT_OF_WINDOW  # walked below the window → ∅
                cur = parent_map[cur][0]
    return cur if cur in parent_map else _ENDPOINT_OUT_OF_WINDOW


def _reach_chain(seed: Any, parent_map: Dict[str, List[str]], chain_set: Set[str]) -> Set[str]:
    """chain_set members that are ancestors-or-self of `seed`, walked over parent_map.

    A seed absent from parent_map yields ∅ — the load-bearing collapse that makes the
    in-window graph sufficient. PRECONDITION: the seed must be an ancestor of the
    graph_range window base (never a "foreign" ref off the window's lineage) — the
    _ENDPOINT_OUT_OF_WINDOW sentinel is the only caller-supplied "absent from
    parent_map" value this function ever receives; _ENDPOINT_FOREIGN seeds are routed
    around this function entirely (per-range fan-out fallback in
    _reviewed_via_graph_walk). Callers that violate the precondition would reintroduce
    the false-COVERED bug fixed 2026-07-15 (Review: the Staff Engineer — MAJOR-1/MAJOR-2).
    """
    if seed not in parent_map:
        return set()
    seen: Set[str] = set()
    stack: List[str] = [seed]
    out: Set[str] = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node in chain_set:
            out.add(node)
        for parent in parent_map.get(node, ()):
            if parent not in seen:
                stack.append(parent)
    return out


def _reviewed_via_graph_walk(
    distinct_ranges: List[Tuple[str, str, Optional[str], Optional[str], str]],
    chain_set: Set[str],
    parent_map: Dict[str, List[str]],
    cwd: str,
    on_record_error: str,
    graph_base: str,
    graph_range: Optional[str] = None,
) -> Dict[str, Set[str]]:
    """Resolve every distinct range against the in-memory graph and union the result,
    PARTITIONED BY `kind` (C5, docs/plans/2026-08-05-coverage-gate-planning-
    artifact-class.md § C5): returns `{kind: reviewed_shas}` from ONE graph walk
    (one parent-map build, one shared BFS memo, one pre-scan) rather than one
    walk per kind — kind-partitioning is pure in-memory set bookkeeping layered
    on top of the existing single-graph-walk machinery below, so the durable
    perf lever (Phase 2's docstring) is unaffected. The caller
    (build_reviewed_set) is responsible for what each kind's bucket is allowed
    to credit — this function only resolves ranges and buckets them by their
    record's own kind.

    Per range `L..R`: reach_chain(R) − reach_chain(L). Per range `L...R` (symmetric):
    reach_chain(L) △ reach_chain(R). chain-restriction distributes over ∪/∩/\\, so this
    equals `(git rev-list <range>) ∩ chain_set` for each range independently — the
    per-range-independent semantics the negative-stripping invariant demands, computed
    without the per-range fan-out, PROVIDED both endpoints are either in-window or an
    ancestor of graph_base (_ENDPOINT_OUT_OF_WINDOW). A "foreign" endpoint
    (_ENDPOINT_FOREIGN — valid ref, neither in-window nor an ancestor of graph_base)
    breaks that precondition; such a range is routed to a per-range git rev-list
    fallback below (still one independent spawn per range — the negative-stripping
    invariant is untouched, see build_reviewed_set's module-level negative-spec).

    Each distinct_ranges entry carries (sha_range, artifact, scope, session_id).
    Records whose scope is in `_FOREIGN_STRIPPED_SCOPES` (session, chain,
    workstream-close-auto — see C7, docs/plans/2026-07-27-review-trail-scope-
    guard.md) additionally have any commit PROVABLY attributed (via its own
    Session-Id trailer) to a DIFFERENT session stripped out before being
    unioned in — see _narrow_foreign_session_scope's docstring for why raw
    range-reachability over-credits on a concurrent (non-sequential) shared
    branch, and for the OLD-vs-NEW semantics this widening replaces. Any other
    scope value keeps the full per-range result (the architectural
    union-coverage rule). That narrowing is primed in bulk up front from ONE
    `git log` walk over `graph_range` rather than one per distinct sha_range —
    see the upfront batch pre-scan below for the equivalence argument.

    `graph_range` is optional (defaults to None) so a direct test-only call
    that supplies no window still works — that pre-scan is simply skipped and
    `_narrow_foreign_session_scope` falls back to resolving each
    (sha_range, session_id) pair on its own, exactly as before this scan
    existed.
    """
    memo: Dict[Any, Set[str]] = {}
    symref_cache: Dict[str, Any] = {}  # symbolic ref (HEAD, branch) → full SHA, resolved once
    session_cache: Dict[Tuple[str, Optional[str]], FrozenSet[str]] = {}
    oow_cache = _OutOfWindowCache()  # per-token resolution + lazy ancestor-of-base set

    # Upfront batch pre-scan: collect every distinct hex BASE token (across both
    # endpoints of every distinct range) that a zero-spawn prefix-scan against
    # parent_map already shows is NOT in-window, and resolve all of them in ONE
    # `git cat-file --batch-check` spawn (see _OutOfWindowCache.preload_hex_tokens).
    # A real corpus commonly cites many DISTINCT out-of-window SHAs (not shared
    # base tokens, where the per-token `resolved` cache alone already collapses
    # repeats) — without this pre-scan, each would cost its own `git rev-parse`
    # spawn in _probe_out_of_window. Pure in-memory work; zero spawns if every
    # endpoint turns out to be in-window.
    _needs_probe = _out_of_window_hex_tokens(
        {_entry[0] for _entry in distinct_ranges}, parent_map
    )
    if _needs_probe:
        oow_cache.preload_hex_tokens(_needs_probe, cwd)

    # Upfront batch pre-scan (mirrors the out-of-window pre-scan above): prime
    # `session_cache` for every (sha_range, session_id) pair this call's records
    # will ask `_narrow_foreign_session_scope` about, via ONE bulk
    # `git log --no-merges` walk over the WHOLE graph_range window instead of
    # one `git log` per distinct sha_range (a real corpus has 60+ records with
    # mostly-distinct ranges, so trailer_foreign_shas's own per-range memo
    # rarely hits).
    #
    # Equivalence: `_narrow`'s only use of the cached value is `shas - foreign`,
    # where `shas` is this range's reach_chain result, ALREADY intersected with
    # chain_set (see `reach`/`_reach_chain` — a node is only added to `out` when
    # `node in chain_set`), or — on the FOREIGN-endpoint fallback path a few
    # lines below — explicitly `& chain_set` before reaching `_narrow`. So
    # `shas` is always a subset of chain_set, and chain_set is always a subset
    # of the commits `git rev-list <graph_range>` would walk (flat-mode
    # chain_set is built from exactly that command; DAG mode never reaches this
    # function — graph_range is None there, see build_reviewed_set). A single
    # `git log --no-merges <graph_range>` walk therefore observes every commit
    # `shas` could ever contain, with the SAME `--no-merges` semantics
    # `trailer_foreign_shas` itself uses (a merge commit is never a foreign
    # candidate either way).
    #
    # A second equivalence collapses this from per-(sha_range, session_id) to
    # per-session_id: `trailer_foreign_shas(sha_range, own, ...)` filters its
    # own `git log <sha_range>` output to commits within sha_range — but since
    # `shas` is already a subset of sha_range's own reachable set (the
    # docstring above: raw == (git rev-list sha_range) ∩ chain_set), any commit
    # that is BOTH in `shas` AND foreign-to-`own` is automatically within
    # sha_range too. So `shas ∩ foreign(sha_range, own)` == `shas ∩
    # foreign(graph_range, own)` — the per-range restriction is redundant once
    # intersected with `shas`. This lets one foreign-set-per-own-session-id
    # (not per-range) serve every distinct_ranges entry that shares a
    # session_id, regardless of how many distinct sha_ranges it spans.
    #
    # Zero extra spawns when there is nothing to narrow (no _FOREIGN_STRIPPED_
    # SCOPES record in this call, or graph_range wasn't supplied — e.g. a
    # direct test-only call to this function). On a bulk `git log` failure,
    # this pre-scan is simply skipped (not populated) — _narrow's existing
    # per-range call to `_narrow_foreign_session_scope` (and its own
    # fail-closed `_ForeignSessionLookupError` handling) runs unchanged for
    # every pair that didn't get a cache hit, so a bulk-walk failure degrades
    # to the pre-existing per-range behavior rather than silently crediting
    # anything.
    _narrowed_pairs = {
        (_sha_range, _session_id)
        for _sha_range, _artifact, _scope, _session_id, _kind in distinct_ranges
        if _scope in _FOREIGN_STRIPPED_SCOPES
    }
    if _narrowed_pairs and graph_range is not None:
        try:
            _bulk_map = session_attribution.bulk_trailer_session_map(graph_range, cwd, _run)
        except session_attribution.GitLogFailed:
            _bulk_map = None
        if _bulk_map is not None:
            _foreign_by_session: Dict[Optional[str], FrozenSet[str]] = {}
            for _sha_range, _session_id in _narrowed_pairs:
                if _session_id not in _foreign_by_session:
                    _foreign_by_session[_session_id] = frozenset(
                        _sha for _sha, _sess in _bulk_map.items() if _sess != _session_id
                    )
                session_cache[(_sha_range, _session_id)] = _foreign_by_session[_session_id]

    def reach(seed: Any) -> Set[str]:
        cached = memo.get(seed)
        if cached is None:
            cached = _reach_chain(seed, parent_map, chain_set)
            memo[seed] = cached
        return cached

    def _narrow(
        shas: Set[str], sha_range: str, scope: Optional[str], session_id: Optional[str]
    ) -> Set[str]:
        if scope not in _FOREIGN_STRIPPED_SCOPES:
            return shas
        try:
            foreign = _narrow_foreign_session_scope(sha_range, session_id, cwd, session_cache)
        except _ForeignSessionLookupError:
            # Fail closed: the git subprocess backing the foreign-session
            # exclusion errored, so we cannot safely narrow this record.
            # Route through the same on_record_error skip/raise semantics
            # every other git failure in this function already uses — "skip"
            # excludes this record's ENTIRE contribution (never falls back
            # to crediting it uncontrolled).
            if on_record_error == "skip":
                return set()
            raise
        return shas - foreign

    reviewed_by_kind: Dict[str, Set[str]] = {}
    for sha_range, artifact, scope, session_id, kind in distinct_ranges:
        symmetric = "..." in sha_range
        sep = "..." if symmetric else ".."
        left, right = sha_range.split(sep, 1)
        r_sha = _resolve_endpoint(right, parent_map, cwd, on_record_error, symref_cache, graph_base, oow_cache)
        l_sha = _resolve_endpoint(left, parent_map, cwd, on_record_error, symref_cache, graph_base, oow_cache)
        if r_sha is _ENDPOINT_UNRESOLVED or l_sha is _ENDPOINT_UNRESOLVED:
            if on_record_error == "skip":
                continue
            raise RuntimeError(f"git rev-list {sha_range!r} failed for {artifact!r}")
        bucket = reviewed_by_kind.setdefault(kind, set())
        if r_sha is _ENDPOINT_FOREIGN or l_sha is _ENDPOINT_FOREIGN:
            # MAJOR-2/MINOR-3 fix: the in-window graph cannot compute reach for a
            # foreign endpoint — fall back to a real git rev-list for THIS range
            # alone (still resolved independently; never batched with other ranges).
            rc, shas_out, _ = _run(["git", "rev-list", sha_range], cwd=cwd)
            if rc != 0:
                if on_record_error == "skip":
                    continue
                raise RuntimeError(f"git rev-list {sha_range!r} failed for {artifact!r}")
            bucket |= _narrow(
                {s.strip() for s in shas_out.splitlines() if s.strip()} & chain_set,
                sha_range, scope, session_id,
            )
            continue
        raw = (reach(l_sha) ^ reach(r_sha)) if symmetric else (reach(r_sha) - reach(l_sha))
        bucket |= _narrow(raw, sha_range, scope, session_id)
    return {kind: (shas & chain_set) for kind, shas in reviewed_by_kind.items()}


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


def build_reviewed_set(
    trail_paths: List[str],
    on_record_error: str = "skip",
    intersect_shas: Optional[Set[str]] = None,
    repo_root: Optional[str] = None,
    graph_range: Optional[str] = None,
) -> Set[str]:
    """Build the reviewed_set from a list of trail record file paths.

    Asymmetric scope filter: the reviewed_set is NEVER path-scoped — all sessions'
    trail records count toward coverage regardless of which file paths those records
    were originally scoped to.

    Two-phase approach:
      Phase 1: classify all valid records (scope-kind filter + SAFE_RANGE + verdict
               filter) — no git calls.
      Phase 2: resolve each record's sha_range and union into reviewed_set. Two
               strategies, byte-identical in result (cross-checked by a differential
               harness — see test_build_reviewed_set_graphwalk_matches_fanout_differential
               — and pinned against an independent git rev-list oracle by
               test_build_reviewed_set_graphwalk_merge_reachability; ground truth is
               the oracle, not mere new==old agreement, per Finding 5 of
               state/review-trail/findings/2026-07-15-codereview-slicecoverage-reviewed-set-test-audit-coordinator-core-tests-test-coverage-rev.md):
                 • single-graph-walk (when `graph_range` and `intersect_shas` are both
                   given) — ONE `git rev-list --parents` graph build + in-memory
                   per-range set math. The durable perf lever: ~1–2 spawns instead of
                   one-per-distinct-range.
                 • per-range fan-out (fallback / when graph_range is absent) — one
                   `git rev-list <range>` per distinct range, resolved concurrently.

    Negative-spec — no batched git rev-list: batching multiple single-commit ranges
    (A^..A B^..B ...) into one git rev-list call produces reachable(positives) \
    reachable(negatives), which excludes every older commit (each parent is an
    ancestor of a newer tip) — only the newest tip survives. Both strategies above
    resolve each range INDEPENDENTLY (the fan-out via a separate process, the
    graph-walk via a separate reach_chain(R) − reach_chain(L) set difference) and
    union — never a single combined positive/negative rev-list. The in-memory set
    algebra is the exact analog of the per-range union; getting the set difference
    wrong reintroduces the same false-COVERED bug the batched form has.

    Args:
        trail_paths:     List of .json trail file paths.
        on_record_error: 'skip' (warn-and-continue on unresolvable ref) or 'fail'.
        intersect_shas:  If provided, return only reviewed SHAs present in this set
                         (verdict-preserving optimisation — gate tests chain-set
                         membership only, so extra SHAs never affect the verdict).
                         REQUIRED to enable the single-graph-walk path: the
                         out-of-window collapse (a BFS seeded outside the graph == ∅)
                         is only provably correct when results are intersected with
                         chain_set, AND the seed is an ancestor of the graph_range
                         window base (see _ENDPOINT_OUT_OF_WINDOW vs _ENDPOINT_FOREIGN
                         — a seed off the window's lineage but not an ancestor of the
                         base is routed to a per-range fallback, never collapsed).
        repo_root:       Repository root for git calls. Required — os.getcwd() fallback
                         removed (AC-5: daemon cwd is not a valid repo root).
        graph_range:     The chain-range the chain_set was derived from (flat mode
                         passes range_arg). When given alongside intersect_shas, the
                         single-graph-walk path is taken; otherwise the per-range
                         fan-out is used. A parent-map build failure silently degrades
                         to the fan-out.

    Returns:
        Set of reviewed commit SHAs.
    """
    if not repo_root:
        raise ValueError(
            "coverage.build_reviewed_set: repo_root is required in multiplex daemon mode; "
            "os.getcwd() fallback removed (AC-5). Pass explicit repo_root."
        )
    cwd = repo_root

    # Phase 1: classification pass — no git calls.
    # (sha_range, artifact, scope, session_id, kind). scope/session_id are carried
    # through so a record whose scope is in _FOREIGN_STRIPPED_SCOPES (session, chain,
    # workstream-close-auto) can be narrowed to exclude provably-foreign commits
    # in Phase 2 (see _narrow_foreign_session_scope) — any other scope (legacy /
    # absent) keeps the full per-range result per the union-coverage rule.
    #
    # `kind` is the record's scope_kind ("diff" for legacy/absent, "plan" for a
    # plan review). It is threaded through so Phase 2 can credit a "plan" record
    # ONLY against planning-artifact commits (see the kind-aware union below) —
    # never against code, which is what a bare `del` of the old
    # `scope_kind in ("plan", "integration"): continue` skip would do. AC6.
    # "integration" remains skipped here entirely — it is NOT reopened by this
    # chunk (see the plan's Anti-scope: only "plan" becomes creditable).
    valid_ranges: List[Tuple[str, str, Optional[str], Optional[str], str]] = []

    for path in trail_paths:
        try:
            records = _parse_trail_file(path)
        except _TrailParseError:
            if on_record_error == "skip":
                continue
            raise

        for rec in records:
            sha_range = rec.get("sha_range", "")
            scope_kind = rec.get("scope_kind")
            artifact = rec.get("artifact", "<unknown>")
            scope = rec.get("scope")
            session_id = rec.get("session_id")

            if scope_kind is not None:
                # Explicit scope_kind: integration → skip (not reopened by this
                # chunk); plan → creditable, but ONLY against planning-artifact
                # commits (enforced in Phase 2, never here — Phase 1 has no git
                # calls and cannot yet know which commits those are).
                if scope_kind == "integration":
                    continue
                if not sha_range:
                    continue  # diff/plan record with empty range — skip silently
                if scope_kind not in _RECOGNIZED_SCOPE_KINDS:
                    # Per-record degrade, not global fatal (2026-08-10 coverage-
                    # gate wedge: a single unrecognized scope_kind anywhere in
                    # the trail corpus used to AssertionError the whole gate
                    # before it ever reached a VERDICT — see
                    # cross-repo/inbox/2026-08-10-example-retrieval-repo-ue-addon-em-
                    # coverage-gate-crashes-on-chunk-and-inline-dispatch-
                    # kinds.md). The record still flows through so its shape is
                    # resolved like any other, but `_credit_from_kind_partition`
                    # never reads an unrecognized kind's bucket, so it earns
                    # zero credit — fail-closed, unchanged safety direction.
                    print(
                        f"WARN: unrecognized scope_kind {scope_kind!r} — record "
                        f"credits nothing: {artifact} ({path})",
                        file=sys.stderr,
                    )
                kind = scope_kind
            else:
                # Legacy record — no scope_kind; use ".." inference. Always "diff".
                if not sha_range or ".." not in sha_range:
                    continue  # non-diff shape — skip
                kind = "diff"

            # SAFE_RANGE check — argument-injection defence
            if not SAFE_RANGE.match(sha_range):
                continue

            # Stored literal-HEAD defence (read-side half of the sha_range
            # false-COVERED defect) — a record whose range re-resolves against
            # whatever HEAD is at gate-run time is excluded entirely, never
            # credited. See _record_range_has_stored_head's docstring.
            if _record_range_has_stored_head(sha_range):
                continue

            # Verdict filter (pending → EXCLUDED; everything else → INCLUDED)
            if not _verdict_counts(rec):
                continue

            valid_ranges.append((sha_range, artifact, scope, session_id, kind))

    if not valid_ranges:
        return set()

    # Phase 2: resolve each distinct range and union into reviewed_set.
    # Each record's sha_range is resolved INDEPENDENTLY so the union of all records'
    # reachable commits accumulates correctly. A single batched call
    # (git rev-list A^..A B^..B ...) is WRONG: it computes
    # reachable(positives) \ reachable(negatives), which strips every commit whose
    # parent appears as a negative (i.e. is an ancestor of another tip) — only the
    # newest tip survives. That invariant forbids COMBINING ranges into one call — it
    # does NOT dictate HOW each range is resolved. Two equivalent strategies below:
    #   (A) single-graph-walk — ONE `git rev-list --parents` builds the ancestor graph,
    #       then each range is resolved by pure-Python set math (reach_chain(R) −
    #       reach_chain(L), intersected with chain_set). Each range is still an
    #       independent set difference, so the negative-stripping invariant is untouched;
    #       the win is ~1–2 spawns instead of one-per-distinct-range.
    #   (B) per-range fan-out — one `git rev-list <range>` per distinct range, resolved
    #       concurrently (each in its own process). The fallback path.
    # We dedup identical ranges first (many trail records cite the same sha_range).
    # Dedup preserves one artifact label per distinct range for the error message; which
    # duplicate wins is immaterial — the resolution failure is a property of the range,
    # not the record. Union into `reviewed` is order-independent, so completion order is
    # irrelevant in either strategy.
    # Dedup key includes (scope, session_id): a record whose scope is in
    # _FOREIGN_STRIPPED_SCOPES must be resolved (and narrowed) independently of
    # a record with a different session_id that happens to cite the identical
    # sha_range — collapsing them by sha_range alone would let one session's
    # narrowed resolution silently stand in for another's. session_id is
    # included in the key ONLY for _FOREIGN_STRIPPED_SCOPES records — those are
    # the sole scopes whose resolution actually depends on session_id (via
    # _narrow_foreign_session_scope's per-session narrowing). C7
    # (docs/plans/2026-07-27-review-trail-scope-guard.md) widened this set from
    # `{"session"}` alone to `{"session", "chain", "workstream-close-auto"}` —
    # unchanged for any scope outside that set, where session_id is still
    # normalized to None before keying, so two records citing the identical
    # sha_range under a scope _narrow_foreign_session_scope is never called
    # for still collapse to one resolution — a bare sha_range distinguishes
    # nothing there, and keying on session_id anyway would spawn a redundant
    # git resolution per distinct session_id (Review: code-reviewer — Finding
    # 3, WARN 958054a5).
    # `kind` joins the dedup key (C5, docs/plans/2026-08-05-coverage-gate-
    # planning-artifact-class.md § C5) — without it, a "plan" record and a
    # "diff" record citing the identical (sha_range, scope, session_id) would
    # silently collapse to whichever parses first via `setdefault`, dropping
    # the other kind's credit entirely (13 on-disk scope_kind:"plan" records
    # carry diff-shaped ranges and scope:"chain", which is exactly this
    # collision case).
    distinct: Dict[Tuple[str, Optional[str], Optional[str], str], str] = {}
    for sha_range, artifact, scope, session_id, kind in valid_ranges:
        dedup_session_id = session_id if scope in _FOREIGN_STRIPPED_SCOPES else None
        distinct.setdefault((sha_range, scope, dedup_session_id, kind), artifact)
    distinct_ranges = [
        (sha_range, artifact, scope, session_id, kind)
        for (sha_range, scope, session_id, kind), artifact in distinct.items()
    ]

    # --- Strategy A: single-graph-walk (durable perf lever) -----------------
    # Requires BOTH a graph_range AND intersect_shas: the out-of-window collapse
    # (reach_chain of a seed absent from the in-window graph == ∅) is only provably
    # correct when the result is intersected with chain_set. A parent-map build
    # failure (rc != 0) silently degrades to the fan-out — the gate must never break
    # on a graph-build hiccup.
    if (
        graph_range is not None
        and intersect_shas is not None
        and SAFE_RANGE.match(graph_range)
    ):
        parent_map = _build_parent_map(cwd, graph_range)
        if parent_map is not None:
            reviewed_by_kind = _reviewed_via_graph_walk(
                distinct_ranges, intersect_shas, parent_map, cwd, on_record_error,
                _range_base(graph_range), graph_range,
            )
            return _credit_from_kind_partition(reviewed_by_kind, cwd)
        # else: fall through to the per-range fan-out (graceful degradation)

    # --- Strategy B: per-range git rev-list fan-out (fallback) --------------
    #
    # Single-commit short-circuit (the dominant spawn hazard measured on this
    # corpus — fleet doctrine has sessions write one per-commit trail record
    # each on a shared branch, so the overwhelming majority of distinct ranges
    # here are `A^..A` / `A~1..A`). `A^..A` is provably `{A}` ONLY when A is
    # NOT a merge commit: a bare `^`/`~1` suffix names A's FIRST parent only,
    # so for a merge, `A^..A` also carries every commit reachable from A's
    # OTHER parents but not the first — {A} would silently under-credit those.
    # Establishing non-merge-ness is therefore batched — ONE
    # `git rev-list --no-walk --parents --stdin` spawn resolves + counts
    # parents for every candidate token at once (`_batch_parent_counts`) —
    # rather than trusting the shape alone, which is what the caveat in this
    # function's own dispatch brief warns against skipping. A candidate whose
    # token doesn't resolve, or resolves with parent_count != 1 (root commit,
    # 0 parents; merge commit, 2+), falls through to the ordinary per-range
    # spawn below UNCHANGED — this short-circuit can only ever REMOVE spawns
    # for provably-safe single-parent commits, never change which range is
    # asked to resolve which way.
    _single_commit_candidates: List[Tuple[str, str, Optional[str], Optional[str], str, str]] = []
    _fanout_items: List[Tuple[str, str, Optional[str], Optional[str], str]] = []
    for item in distinct_ranges:
        sha_range = item[0]
        m = _SINGLE_COMMIT_RANGE_RE.match(sha_range)
        if m:
            _single_commit_candidates.append(item + (m.group(1),))
        else:
            _fanout_items.append(item)

    _short_circuit_results: List[Tuple[str, str, Optional[str], Optional[str], str, int, str]] = []
    if _single_commit_candidates:
        _tokens = sorted({c[5] for c in _single_commit_candidates})
        _parent_info = _batch_parent_counts(_tokens, cwd)
        for sha_range, artifact, scope, session_id, kind, token in _single_commit_candidates:
            _info = _parent_info.get(token)
            if _info is not None and _info[1] == 1:
                _full_sha = _info[0]
                _short_circuit_results.append(
                    (sha_range, artifact, scope, session_id, kind, 0, _full_sha)
                )
            else:
                _fanout_items.append((sha_range, artifact, scope, session_id, kind))

    distinct_ranges = _fanout_items

    def _resolve(
        item: Tuple[str, str, Optional[str], Optional[str], str]
    ) -> Tuple[str, str, Optional[str], Optional[str], str, int, str]:
        sha_range, artifact, scope, session_id, kind = item
        rc, shas_out, _ = _run(["git", "rev-list", sha_range], cwd=cwd)
        return sha_range, artifact, scope, session_id, kind, rc, shas_out

    max_workers = min(len(distinct_ranges), _REVLIST_MAX_WORKERS)
    if max_workers <= 1:
        results = [_resolve(item) for item in distinct_ranges]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(_resolve, distinct_ranges))
    results = results + _short_circuit_results

    # Records whose scope is in _FOREIGN_STRIPPED_SCOPES (session, chain,
    # workstream-close-auto — widened by C7, docs/plans/2026-07-27-review-
    # trail-scope-guard.md) have any commit PROVABLY attributed (via its own
    # Session-Id trailer) to a DIFFERENT session stripped out — see
    # _narrow_foreign_session_scope's docstring for why raw sha_range
    # reachability over-credits on a shared branch with concurrent (not
    # sequential) sessions, and for the OLD-vs-NEW semantics this widening
    # replaces. Cache is local to this call; many records commonly share a
    # range/session_id.
    session_cache: Dict[Tuple[str, Optional[str]], FrozenSet[str]] = {}

    # Bulk pre-scan (Strategy B's counterpart to _reviewed_via_graph_walk's
    # own upfront batch pre-scan — see that function's equivalence argument
    # for the general shape). Strategy B has no single graph_range window to
    # walk trailers over in one shot, but the per-range `git rev-list` spawn
    # just above has ALREADY resolved the exact SHA membership of every
    # range (`shas_out`) — trailer lookup can therefore be batched over that
    # fixed, already-known SHA set via bare-SHA `git log` positional args
    # (never ranges, so there is no cross-range exclusion ambiguity: a bare
    # SHA is always shown, unlike combining several `A..B` ranges in one
    # invocation, where exclusions apply globally and could silently drop a
    # commit that belongs to a DIFFERENT range) — see `_bulk_trailer_lookup`.
    # This replaces what would otherwise be one `trailer_foreign_shas` ->
    # `git log <sha_range>` spawn per distinct (sha_range, session_id) pair
    # with ONE (chunked) bulk spawn for the whole call.
    _narrowed_items: List[Tuple[str, Optional[str], List[str]]] = []
    _narrowed_shas: Set[str] = set()
    for sha_range, artifact, scope, session_id, kind, rc, shas_out in results:
        if rc == 0 and scope in _FOREIGN_STRIPPED_SCOPES:
            _shas = [s.strip() for s in shas_out.splitlines() if s.strip()]
            _narrowed_shas.update(_shas)
            _narrowed_items.append((sha_range, session_id, _shas))
    if _narrowed_items:
        _bulk_trailers = _bulk_trailer_lookup(_narrowed_shas, cwd)
        if _bulk_trailers is not None:
            for _sha_range, _session_id, _shas in _narrowed_items:
                session_cache[(_sha_range, _session_id)] = frozenset(
                    sha for sha in _shas
                    if _bulk_trailers.get(sha) not in (None, _session_id)
                )
        # else: bulk lookup failed — session_cache stays unprimed for these
        # pairs, and _narrow_foreign_session_scope's per-pair call below
        # (via trailer_foreign_shas' own cache-miss path) resolves each one
        # exactly as it did before this pre-scan existed — same fail-closed
        # degrade-to-per-range posture the graph-walk pre-scan already uses.

    # Partitioned by kind (C5) — see _credit_from_kind_partition's docstring
    # for why "plan" is filtered against the planning classifier before
    # joining the final credited set, and "diff" is not.
    reviewed_by_kind: Dict[str, Set[str]] = {}
    for sha_range, artifact, scope, session_id, kind, rc, shas_out in results:
        if rc != 0:
            if on_record_error == "skip":
                continue
            raise RuntimeError(f"git rev-list {sha_range!r} failed for {artifact!r}")
        if scope in _FOREIGN_STRIPPED_SCOPES:
            try:
                foreign = _narrow_foreign_session_scope(sha_range, session_id, cwd, session_cache)
            except _ForeignSessionLookupError:
                # Fail closed (see _ForeignSessionLookupError): route through
                # the same skip/raise semantics as the git rev-list failure
                # just above — "skip" excludes this record's entire
                # contribution rather than crediting it uncontrolled.
                if on_record_error == "skip":
                    continue
                raise
        else:
            foreign = frozenset()
        bucket = reviewed_by_kind.setdefault(kind, set())
        for sha in shas_out.splitlines():
            sha = sha.strip()
            if not sha:
                continue
            if sha in foreign:
                continue
            if sha and (intersect_shas is None or sha in intersect_shas):
                bucket.add(sha)

    return _credit_from_kind_partition(reviewed_by_kind, cwd)


# ---------------------------------------------------------------------------
# Handoff-level utilities for the DAG-mode fixpoint
# ---------------------------------------------------------------------------


def _build_dag_index(repo_root: str) -> List[str]:
    """Build the in-memory handoff path list for the archival seam.

    Collects all handoff paths (live + archived). This list is passed as dag_index
    to coordinator_core.archival.reverse_membership; C4's implementation will use it
    as the in-memory frontmatter index (replacing the query-records.js double-spawn).

    Paths scanned:
        <repo_root>/state/handoffs/*.md        — live handoffs
        <repo_root>/archive/handoffs/**/*.md   — archived handoffs
    """
    paths: List[str] = []
    root = Path(repo_root)

    live_dir = root / "state" / "handoffs"
    if live_dir.is_dir():
        for p in sorted(live_dir.glob("*.md")):
            paths.append(str(p))

    archive_dir = root / "archive" / "handoffs"
    if archive_dir.is_dir():
        for p in sorted(archive_dir.rglob("*.md")):
            paths.append(str(p))

    return paths


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

    Used by Step 3's leg (b) (legacy-history fallback): a commit already
    matched by Session-Id is kept in a deliverable_id node's segment if it
    carries NO Deliverable-Id trailer at all, OR one that no WALKED node
    holds (see AC0 / the pre-pass `walked_deliverable_ids` set) — this is
    what makes that leg attribute untrailered history exactly as before
    while still excluding a commit stamped with a deliverable a *different
    walked* node holds, without dropping it entirely just because SOME
    (possibly unwalked) node happens to hold a Deliverable-Id.

    On git failure (rc != 0), every sha maps to "" (absent) — fail-closed
    for the leg-(b) inclusion test (an unreadable trailer is treated as "no
    trailer", i.e. still eligible for legacy inclusion via Session-Id, not
    silently dropped).
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
# matcher (parity with flat mode's `git rev-list -- scope_paths`; see
# run_coverage_gate's DAG branch, which applies this after _derive_dag_chain_set
# resolves chain_set and only when scope_paths is non-empty).
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

    Exists so a renderer (run_coverage_gate's DAG-mode UNCOVERED notes) can
    label an uncovered commit with its originating baton and print the
    ancestry chain without recomputing which commits belong to which node —
    see the module's "Shape to avoid" guidance on _derive_dag_chain_set: the
    per-node segment resolution happens ONCE, here, never copy-pasted into a
    caller.

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
    """Internal result of _derive_dag_chain_set.

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
    #: have seeded leg (b) was judged a bulk sweep (see
    #: _BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD). Never a member of `shas` —
    #: these are "in range, unattributable to this chain", not "this
    #: chain's inheritance". Reported so a closer sees they exist and are
    #: someone else's, rather than the report silently shrinking. Additive
    #: field — existing callers reading only `shas`/`node_attribution` are
    #: unaffected.
    unattributable_shas: List[str] = field(default_factory=list)


@dataclass
class _DagChainSetContext:
    """Optional cross-call cache for _derive_dag_chain_set (C6b), amortising
    the per-call dag_index build (_build_dag_index) and the batched HEAD
    Session-Id/Deliverable-Id git-log walk across multiple invocations that
    share the same repo_root within one process run — e.g. a future caller
    computing one oracle per baton, where every baton's fixpoint walks the
    same repo history from scratch today.

    Lifetime: construct ONE instance per repo_root per run (e.g. once at the
    top of a multi-baton driver loop), pass it to every _derive_dag_chain_set
    call for that repo_root, and discard it at the end of the run. It is NOT
    safe to persist across processes or across a long-lived daemon's tick
    boundary — git history and archived handoffs can change between runs,
    and a stale dag_index or stale sid_to_shas/did_to_shas/sha_to_did map
    would silently under- or over-count ancestors. Passing the same instance
    across two DIFFERENT repo_root values is a caller bug this function does
    not detect or guard against.

    Fields are populated lazily: the first _derive_dag_chain_set call that
    receives an instance with `dag_index is None` (unfilled) performs the
    normal per-call build and stores the result back onto the instance;
    every subsequent call sharing that same instance skips both the
    dag_index build (_build_dag_index) and the batched `git log HEAD
    --no-merges` walk entirely, reading the already-populated dicts instead.

    MUST NOT hold a `dag.GitHistoryCache` (dag.py:924's `set` subclass
    carrying `.complete`, read via `getattr(..., False)` in
    `_memoized_ever_tracked`) or anything shaped to satisfy that contract —
    this context is unrelated to it. In particular this class does NOT
    adopt `dag.as_history_membership_set`'s "strip to a bare set before
    reuse" requirement, because none of these fields are ever passed to a
    `GitHistoryCache`-consuming seam; do not widen that association by
    adding a cache-shaped field here without re-reading both contracts.
    """

    dag_index: Optional[List[str]] = None
    sid_to_shas: Optional[Dict[str, List[str]]] = None
    did_to_shas: Optional[Dict[str, List[str]]] = None
    sha_to_did: Optional[Dict[str, str]] = None


def _resolve_closing_handoff_disk_path(from_handoff_abs: str, repo_root: str) -> str:
    """Return the actual on-disk path for the closing handoff, following it
    into ``archive/handoffs/`` (flat or month-nested) when the caller-supplied
    path has since been swept out of ``state/handoffs/`` by the fleet's
    routine post-close archival — e.g. a diagnostic re-run of this gate
    against a handoff path recorded before that handoff's own archival commit
    landed.

    Latent-bug fix (this dispatch, C2): ``walk_forward``'s edge-target
    resolution (``dag.resolve_target``) already walks this same
    state→archive→month-nested search for every ANCESTOR edge it follows, but
    the START node it is given (``closing_abs`` in ``_derive_dag_chain_set``)
    was passed through verbatim with no equivalent fallback. When the closing
    handoff itself had already been archived, ``walk_forward`` could not read
    its frontmatter at all — the DFS found zero edges, ``ordered_ancestry``
    collapsed to the single closing node, and every downstream per-node read
    in this module (``_parse_handoff_deliverable_id``,
    ``_resolve_add_commit_session_id`` et al) silently degraded to its
    conservative-None/live-default fallback. The net effect: the entire
    weak-authorship/leg-(b) split this module exists to perform (AC1-AC4)
    never engaged, and C2's unattributable-in-range reporting had nothing to
    report — not because there was nothing foreign, but because the one node
    that would have surfaced it (the archived ancestor baton) was never
    walked. Returns the original ``from_handoff_abs`` unchanged whenever it
    still exists on disk (the overwhelmingly common case) or no archived
    counterpart can be found — never raises, never widens resolution beyond
    an exact basename match under ``archive/handoffs/``.
    """
    if os.path.exists(from_handoff_abs):
        return from_handoff_abs
    basename = os.path.basename(from_handoff_abs)
    archive_dir = os.path.join(repo_root, "archive", "handoffs")
    flat_candidate = os.path.join(archive_dir, basename)
    if os.path.exists(flat_candidate):
        return flat_candidate
    if os.path.isdir(archive_dir):
        try:
            for entry in sorted(os.listdir(archive_dir), reverse=True):
                if re.match(r"^\d{4}-\d{2}$", entry):
                    candidate = os.path.join(archive_dir, entry, basename)
                    if os.path.exists(candidate):
                        return candidate
        except OSError:
            pass
    return from_handoff_abs


def _derive_dag_chain_set(
    from_handoff: str,
    repo_root: str,
    closing_session_id: str = "",
    shared_context: Optional["_DagChainSetContext"] = None,
) -> _DagChainResult:
    """Derive the DAG-mode chain_set via ancestor walk + coverable-set fixpoint.

    Step 1 — Walk: dag.walk_forward from the closing handoff, following
        predecessor + additional_predecessors edges only (forked_from EXCLUDED —
        review obligation follows continuation edges, not fork ancestry; plan B.0).
        Mirrors: node "$WALK_DAG" --start "$closing_abs"
                   --edge-kinds "predecessor,additional_predecessors" --format paths

    Step 2 — Fixpoint: iteratively add ancestors to closing_set when they have no
        live blockers outside closing_set. Converges when a full sweep adds nothing
        new. Uses archival.reverse_membership as the has-live-children seam per
        ancestor.

    Step 3 — Segment attribution: for each coverable node, find its add-commit
        Session-Id trailer (--follow -M100% exact-rename), then use git log --grep
        to enumerate all commits in that session. When the node's handoff carries
        a `deliverable_id` frontmatter field (the durable fleet artifact-spine
        join key — see handoff.schema.json), attribution instead becomes the
        UNION of (a) commits whose `Deliverable-Id` trailer equals that id, and
        (b) commits matching the node's Session-Id that carry NO `Deliverable-Id`
        trailer at all, or one that no WALKED node (this closing_set) holds
        (leg (b) is the legacy-history fallback: it is what keeps every
        existing untrailered commit attributed exactly as before, while a
        commit stamped with a deliverable a *different walked* node holds is
        now correctly excluded — an UNWALKED node's deliverable stamp does
        NOT exclude, or the commit becomes a member of no segment at all, see
        AC0 — the fix for Session-Id over-attribution, see improvement-queue
        2026-07-01/2026-07-03 chain-end-coverage-gate entries and the 2026-07-27
        example-retrieval-repo / example-cockpit-repo reports of the same defect). Nodes without
        a `deliverable_id` keep the plain Session-Id segment, byte-identical to
        pre-existing behaviour.

    Fidelity guards implemented (all three from the C3 spec):
        Guard 1 (UUID-shape): validate Session-Id before git grep interpolation.
            A malformed SID with regex metacharacters would over-match → false COVERED.
            Malformed SID → INDETERMINATE (never FALSE COVERED).
        Guard 2 (exit-0-empty): any exception from reverse_membership
            (including the C4-stub NotImplementedError) → INDETERMINATE.
            This guards "exit-0-with-empty-stdout → INDETERMINATE": if we cannot safely
            enumerate blockers, we fail closed rather than vacuously coverable.
        Guard 3 (all-stale-blockers): if every blocker's consuming session is
            non-live, that ancestor is coverable (not actively blocked).

    Args:
        from_handoff:       Absolute path to the closing handoff.
        repo_root:          Repository root for git calls.
        closing_session_id: Active session ID of the closing handoff (D3 case 3 —
                            equates to $CLAUDE_CODE_SESSION_ID in the bash gate).
        shared_context:     Optional (C6b). When absent (default), this call
                            builds and discards its own dag_index and
                            sid_to_shas/did_to_shas/sha_to_did exactly as
                            before — existing callers are unaffected. When a
                            caller threads a shared `_DagChainSetContext`
                            across multiple calls for the same repo_root,
                            this call reuses the context's already-populated
                            fields (skipping _build_dag_index and the batched
                            git log HEAD walk) and, on first use with an
                            unfilled context, populates it for later callers.
                            See `_DagChainSetContext` for its lifetime rules.
    """
    closing_abs = _resolve_closing_handoff_disk_path(
        os.path.abspath(from_handoff), repo_root
    )
    result = _DagChainResult()

    # Step 1: walk ancestors from closing handoff.
    walk_result = walk_forward(
        closing_abs,
        edge_kinds=set(_CONTINUATION_EDGE_KINDS),
        # Pass the true repo_root explicitly — do NOT let walk_forward infer it two-dirs-up
        # from the closing handoff's own dir, which is WRONG when that handoff was swept to a
        # month-nested archive/handoffs/YYYY-MM/ path (collapses the DAG → INDETERMINATE).
        repo_root=repo_root,
    )

    # A lineage-cycle is always INDETERMINATE — cannot safely determine coverage.
    if walk_result.get("terminatedEarly") == "lineage-cycle":
        result.indeterminate = True
        result.notes.append(
            f"walkForward detected lineage-cycle from {closing_abs}"
        )
        return result

    ancestors: List[str] = walk_result.get("orderedPaths", [])
    # Threaded through verbatim (AC1) — see _DagChainResult.ordered_ancestry.
    result.ordered_ancestry = list(ancestors)

    # Build dag_index once for all reverse_membership calls in the fixpoint.
    # C4's reverse_membership will use this list as the in-memory frontmatter index.
    # C6b: reuse a caller-threaded shared_context's dag_index when present and
    # already populated, instead of rebuilding it every call.
    if shared_context is not None and shared_context.dag_index is not None:
        dag_index = shared_context.dag_index
    else:
        dag_index = _build_dag_index(repo_root)
        if shared_context is not None:
            shared_context.dag_index = dag_index

    # Step 2: fixpoint.
    # closing_set: ancestors determined to be coverable. The closing handoff itself is
    # always coverable (it is the current workstream's chain end).
    closing_set: Set[str] = {closing_abs}

    changed = True
    while changed:
        changed = False

        # Re-resolve live_sids once per SWEEP (not once globally before the loop,
        # and not once per ancestor). A single global snapshot goes stale if a
        # blocker's owning session dies partway through the fixpoint's own
        # execution (crash, or a concurrent session legitimately closing that
        # handoff mid-walk) — later sweeps would keep checking liveness against
        # a dead session's still-live snapshot, so `all_stale` (guard 3, below)
        # never flips and the ancestor is wrongly kept not-coverable
        # (false-UNCOVERED). Per-sweep re-resolution bounds the shell-out cost to
        # O(sweeps) rather than O(ancestors) — the cost the original hoist-once
        # comment was avoiding — while still observing a mid-walk session death
        # on the very next sweep instead of never.
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        # Previously: except Exception: live_sids = frozenset() — continuing the
        # fixpoint on an empty live-session set is NOT fail-closed: a blocker whose
        # consumed_by session IS actually live has `sid in live_sids` false against
        # the empty set, so _handoff_session_live reports it non-live, which can
        # flip all_stale to True and mark an ancestor coverable that a correct
        # lookup would have kept blocked — a fail-OPEN effect hiding behind a
        # conservative-looking default. Matches Guard-2 (:856/:864): capture the
        # exception text and fail the whole fixpoint closed to INDETERMINATE
        # instead of silently computing on a false-empty live-session set.
        try:
            live_sids: FrozenSet[str] = resolve_live_session_ids()
        except Exception as exc:
            result.indeterminate = True
            result.notes.append(
                f"resolve_live_session_ids raised {type(exc).__name__}: {exc} — INDETERMINATE"
            )
            return result
        # --- end Tier 2 ---

        for ancestor in ancestors:
            if ancestor == closing_abs:
                continue  # closing handoff always in closing_set; skip
            if ancestor in closing_set:
                continue  # already determined coverable

            # has-live-children check via archival seam (fidelity guard 2).
            # Any exception → INDETERMINATE (fail closed). This mirrors the bash
            # "exit-0-with-empty-stdout → INDETERMINATE" guard (:292-298): if we
            # cannot enumerate blockers safely, do NOT vacuously treat as coverable.
            # Edge-symmetric with Step 1's walk BY CONSTRUCTION, not by
            # coincidence. `reverse_membership` defaults to the ARCHIVAL edge
            # set — see `dag.ARCHIVAL_EDGE_KINDS` / `dag.CONTINUATION_
            # EDGE_KINDS` for why that default is wrong for the CONCLUSION-
            # shaped question asked here. Taking that default lets a live
            # spinoff veto an ancestor whose commits Step 1 already decided
            # the spinoff does not inherit, dropping them out of
            # chain_commits with no note -> false COVERED. Origin:
            # cross-repo/inbox/2026-08-05-example-cockpit-repo-em-wsc-leg-b-counts-
            # spinoffs-as-live-children.md.
            try:
                children: FrozenSet[str] = reverse_membership(
                    ancestor, dag_index, edge_kinds=set(_CONTINUATION_EDGE_KINDS)
                )
            except NotImplementedError:
                # C4 body not yet filled; reverse_membership is a stub (C0 seam).
                # INDETERMINATE — cannot determine coverage (guard 2).
                result.indeterminate = True
                result.notes.append(
                    f"{ancestor}: reverse_membership not yet implemented (C4 pending) — INDETERMINATE"
                )
                return result
            except Exception as exc:
                # Runtime error or contract violation — guard 2.
                result.indeterminate = True
                result.notes.append(
                    f"{ancestor}: reverse_membership raised {type(exc).__name__}: {exc} — INDETERMINATE"
                )
                return result

            # blockers = live children outside closing_set (already-coverable nodes
            # cannot block an ancestor from being coverable).
            blockers = [c for c in children if c not in closing_set]

            if not blockers:
                # No live blockers outside closing_set → ancestor is coverable.
                closing_set.add(ancestor)
                changed = True
                continue

            # Has live blockers → check if ALL their sessions are stale (guard 3).
            all_stale = True
            for blocker in blockers:
                is_live, live_note = _handoff_session_live(blocker, live_sids)
                # --- Tier 2 (behaviour change -- PM sign-off required) ---
                # Previously: a read/parse exception inside _get_handoff_consumed_by
                # was swallowed there and surfaced only as the ordinary
                # conservative-live return, indistinguishable from a legitimately-
                # unconsumed handoff. Matches Guard-2 (:856/:864): capture the
                # exception text and fail the whole fixpoint closed to
                # INDETERMINATE rather than let one unreadable handoff silently
                # steer an ancestor's coverability.
                if live_note is not None:
                    result.indeterminate = True
                    result.notes.append(live_note)
                    return result
                # --- end Tier 2 ---
                if is_live:
                    all_stale = False
                    break

            if all_stale:
                # Guard 3: all-stale-blockers → coverable.
                closing_set.add(ancestor)
                changed = True
            # else: ancestor has at least one live blocker → not yet coverable

    # Step 3: segment attribution.
    # For each coverable ancestor, find its Session-Id trailer from the add-commit,
    # then enumerate all commits in that session.
    # -M100% exact-rename: archival is a 100%-identical git mv; fuzzy rename detection
    # (default 50%) falsely chains content-similar but distinct handoffs, misattributing
    # the segment to an unrelated older handoff. Exact-rename kills the false match
    # while preserving archival tracking. (AC16)
    # Pre-pass (AC0, 2026-07-31): collect every WALKED node's deliverable_id
    # before the per-node loop below. This closes a fail-open in leg (b):
    # `trailers.get(sha)` used to be a truthiness test on ANY Deliverable-Id,
    # not on a *different* one, so a commit carrying node A's Session-Id and
    # some OTHER (possibly unwalked) node's Deliverable-Id was dropped from
    # A's segment outright. It was recovered only if that other node was
    # itself walked and reached leg (a)'s grep -- an unwalked node's grep
    # never runs, so the commit became a member of no node's segment at all
    # and silently vanished from chain_set with no note, no miscount, never a
    # candidate. Scoping the exclusion to WALKED deliverable_ids only (this
    # set) means an unwalked node's deliverable stamp no longer excludes
    # anything -- only a deliverable another walked node actually holds does.
    # `_parse_handoff_deliverable_id` reads handoff frontmatter from disk, not
    # git -- no new subprocess spawns added by this pre-pass.
    # Review: code-reviewer — plain accumulator loop, matching this file's
    # existing for/if idiom (no walrus operator appears elsewhere here),
    # rather than the singleton-tuple trick that only existed to avoid a
    # second _parse_handoff_deliverable_id call.
    walked_deliverable_ids: Set[str] = set()
    for node_path in closing_set:
        did = _parse_handoff_deliverable_id(node_path)
        if did is not None:
            walked_deliverable_ids.add(did)

    # C6a batching (this chunk): the per-node segment-attribution loop below
    # used to spawn THREE separate git processes per coverable node —
    # (a) `git log HEAD --no-merges --grep=^Session-Id: <sid>$` (segment
    #     enumeration), (b) `git log HEAD --no-merges --grep=^Deliverable-Id:
    #     <did>$` (leg-a deliverable match), and (c)
    #     `_commit_deliverable_id_trailers(seg, repo_root)` (leg-b legacy
    #     fallback, itself already one batched call, but one PER node).
    # All three read the SAME ref (HEAD) with no negative endpoint — a single
    # positive walk, not a multi-range subtraction, so this is outside the
    # set-algebra trap the module docstring warns about (reachable(positives)
    # \ reachable(negatives) collapsing a linear chain to its tip): there is
    # exactly one positive endpoint (HEAD) and zero negatives here. One
    # `git log HEAD --no-merges` walk, reading both trailers per commit in
    # the same `--format=`, replaces all three per-node call sites with a
    # single spawn shared by the whole loop — segment/deliverable membership
    # then becomes an in-memory dict lookup. This does NOT touch the
    # `--follow -M100%` add-commit resolution above (still per-node,
    # unavoidable — see module Out-of-scope) nor its own single-commit
    # Session-Id trailer read (still per add_sha — batching that needs the
    # add_sha values known ahead of the per-node `--follow` walk, which would
    # mean restructuring THAT loop's early-exit ordering; out of scope here).
    # `%(trailers:key=...,valueonly)` always emits its OWN trailing newline
    # as part of the placeholder's own expansion (empirically verified on
    # this repo: `A[%(trailers:key=Session-Id,valueonly)]B` renders as
    # `A[<value>\n]B`, not `A[<value>]B`) — a per-record joined single line
    # therefore does NOT stay one line; splitting the whole batched output on
    # newlines (as `_commit_touched_paths` does for `--name-only`, which has
    # no such embedded-newline placeholder) would silently shear each record
    # into two ragged lines. Per-record framing instead uses
    # `_COMMIT_HEADER_SENTINEL` and splits the WHOLE batched stdout on that
    # sentinel, never on "\n".
    #
    # Field separator is `\x01` (SOH), deliberately NOT `\x1f` (Unit
    # Separator, the choice this module's other batched-trailer helper
    # `_commit_deliverable_id_trailers` uses): `_run`'s blanket
    # `result.stdout.strip()` operates over Python's Unicode whitespace
    # definition, which — surprisingly — classifies U+001C-U+001F
    # (File/Group/Record/Unit Separator) as White_Space=Yes. When the LAST
    # record in a batch has an empty trailing field (e.g. no Deliverable-Id
    # trailer), that record's tail is `...<sid>\n\x1f\n` — an unbroken run of
    # Unicode-whitespace at the very end of the whole string — and `_run`'s
    # `.strip()` eats straight through the `\x1f` separator along with the
    # surrounding newlines, silently truncating the batch's last record
    # (empirically verified: reproduces on this repo's own two-commit
    # fixture, confirmed via `'\x1f'.isspace() == True`). `\x01` is not
    # White_Space, so it survives `.strip()` at any position, including the
    # very end of the batched output.
    # C6b: reuse a caller-threaded shared_context's batched-walk dicts when
    # present and already populated (all three, since they're filled
    # together from one spawn), instead of re-spawning `git log HEAD` here.
    if (
        shared_context is not None
        and shared_context.sid_to_shas is not None
        and shared_context.did_to_shas is not None
        and shared_context.sha_to_did is not None
    ):
        sid_to_shas = shared_context.sid_to_shas
        did_to_shas = shared_context.did_to_shas
        sha_to_did = shared_context.sha_to_did
    else:
        rc_head, head_out, _ = _run(
            [
                "git", "log", "HEAD", "--no-merges",
                f"--format={_COMMIT_HEADER_SENTINEL}%H\x01"
                "%(trailers:key=Session-Id,valueonly)"
                "\x01%(trailers:key=Deliverable-Id,valueonly)",
            ],
            cwd=repo_root,
        )
        sid_to_shas = {}
        did_to_shas = {}
        sha_to_did = {}
        if rc_head == 0:
            for record in head_out.split(_COMMIT_HEADER_SENTINEL):
                if not record.strip():
                    continue
                fields = record.split("\x01", 2)
                if len(fields) != 3:
                    continue
                h, sid_v, did_v = fields[0].strip(), fields[1].strip(), fields[2].strip()
                if not h:
                    continue
                if sid_v:
                    sid_to_shas.setdefault(sid_v, []).append(h)
                sha_to_did[h] = did_v
                if did_v:
                    did_to_shas.setdefault(did_v, []).append(h)
        # On rc_head != 0, all three dicts stay empty — every per-node lookup
        # below then sees zero matches, exactly the same degraded shape the
        # old per-node `rc3/rc4 != 0 -> []` branches produced, so the existing
        # vacuous-match guard (INDETERMINATE, never silently COVERED) still
        # fires downstream unchanged.
        if shared_context is not None:
            shared_context.sid_to_shas = sid_to_shas
            shared_context.did_to_shas = did_to_shas
            shared_context.sha_to_did = sha_to_did

    def _resolve_add_commit_session_id(node_path: str) -> Tuple[str, str]:
        """Resolve (add_sha, Session-Id) for the commit that originally added
        `node_path` — the exact two-`git log`-call derivation the `else`
        branch below has always performed. Factored out so the `closing_abs`
        branch reuses it verbatim instead of a second, divergent copy.
        Returns ("", "") when either call fails or the add-commit/trailer
        cannot be resolved.

        Used for TWO purposes on the `closing_abs` path, not one: it detects
        the pickup (author != runner, recorded on
        `_DagNodeAttribution.attribution_disagrees`), AND on the claim-gated
        branch its resolved Session-Id becomes the attributed `sid` (DR-286).
        An earlier revision of this docstring said "observation path", which
        was true of C1 alone and understated the helper's role after C2.

        COST, stated because the obvious summary understates it: the
        `closing_abs` branch calls this UNCONDITIONALLY whenever
        `closing_session_id` is truthy — not only on a detected pickup — so
        every ordinary DAG-mode gate run now pays these two `git log` spawns,
        where it previously took a zero-spawn shortcut. The cost is O(1) per
        gate run, NOT O(nodes): "no new PER-NODE spawn" is true but is a
        weaker claim than "no new spawn", and the difference is this. Accepted
        deliberately — detecting a pickup requires resolving the author, and
        there is no cheaper signal that is also trustworthy (frontmatter
        `authoring_session` is self-declared; the add-commit trailer is
        git-truth). Revisit only if gate-run frequency makes a fixed
        two-spawn tax material on this machine's load norm.
        """
        rc, add_sha_out, _ = _run(
            [
                "git", "log", "--follow", "-M100%",
                "--diff-filter=A", "--format=%H", "--", node_path,
            ],
            cwd=repo_root,
        )
        add_sha_lines = (
            [s.strip() for s in add_sha_out.splitlines() if s.strip()]
            if rc == 0
            else []
        )
        # Take the OLDEST add-commit (last line — git log is newest-first)
        add_sha = add_sha_lines[-1] if add_sha_lines else ""
        if rc != 0 or not add_sha:
            return "", ""
        rc2, sid_raw, _ = _run(
            ["git", "log", "-1", "--format=%(trailers:key=Session-Id,valueonly)", add_sha],
            cwd=repo_root,
        )
        resolved_sid = sid_raw.strip().strip("\r\n") if rc2 == 0 else ""
        return add_sha, resolved_sid

    seen_sha: Set[str] = set()
    for node in closing_set:
        sid = ""
        authored_sid: Optional[str] = None
        attribution_disagrees = False
        # Captured in both branches below so the leg (b) bulk-sweep check
        # (deliverable_id union, further down) has this node's add-commit
        # regardless of which branch resolved `sid` — see
        # _add_commit_touched_file_count / _BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD.
        add_sha = ""

        if node == closing_abs and closing_session_id:
            # D3 case 3: the closing handoff's session is the currently-active session.
            # The add-commit trailer trail might not yet exist (unpublished handoff).
            sid = closing_session_id
            # Resolve the node's own add-commit Session-Id (the same
            # derivation the `else` branch performs) and record whether it
            # disagrees with the closing_session_id shortcut above — a
            # disagreement IS a detected pickup. See
            # _DagNodeAttribution.attribution_disagrees.
            #
            # `sid` is reassigned below ONLY on the claim-gated pickup path
            # (DR-286). When author == runner — the ordinary close, and the
            # overwhelming majority — this block records nothing and `sid`
            # keeps `closing_session_id`, so attribution, `result.shas`, and
            # the verdict are byte-identical to pre-C1 behaviour. That
            # byte-identity is asserted by
            # test_same_session_author_runner_control_MUST_NOT_CHANGE; if it
            # ever fails, this code is wrong, not the test.
            add_sha, authored_sid_raw = _resolve_add_commit_session_id(node)
            if authored_sid_raw:
                authored_sid = authored_sid_raw
                if authored_sid_raw != closing_session_id:
                    attribution_disagrees = True
                    # DR-286: a detected pickup (disagreement above) attributes
                    # the handoff's authoring session — gated on the pickup
                    # claim record. Fail closed on any non-positive resolution
                    # (unreadable/absent/ambiguous/third-party claim): `sid`
                    # stays `closing_session_id`, today's behaviour. Only a
                    # claim positively held by `closing_session_id` flips it.
                    # `_get_handoff_consumed_by` already degrades read/parse
                    # failures to `None` (conservative), which is the correct
                    # fail-closed direction for this gate too — a failure must
                    # never widen attribution.
                    claim_holder = _get_handoff_consumed_by(
                        node, repo_root=repo_root
                    )
                    if claim_holder == closing_session_id:
                        sid = authored_sid_raw
                        result.notes.append(
                            f"{node}: pickup detected — claim on this handoff "
                            f"held by running session {closing_session_id!r}; "
                            f"attributing authoring session {authored_sid_raw!r} "
                            "(DR-286)"
                        )
                    else:
                        result.notes.append(
                            f"{node}: OBSERVATION — add-commit Session-Id "
                            f"{authored_sid_raw!r} disagrees with closing_session_id "
                            f"{closing_session_id!r} (attribution unchanged this run)"
                        )
        else:
            # Find the commit that originally added this handoff file.
            # tail -1 equivalent: git log outputs newest→oldest; last line = oldest = add-commit.
            add_sha, sid = _resolve_add_commit_session_id(node)
            authored_sid = sid or None
            if not add_sha:
                if node == closing_abs:
                    # Preserved safety guard: the CLOSING/current session's own
                    # node must still hard-fail INDETERMINATE when its own
                    # add-commit is unresolvable — this is the node THIS run
                    # is responsible for reviewing, not an inherited
                    # ancestor's segment.
                    result.indeterminate = True
                    result.notes.append(f"{node}: no add-commit found (attribution gap)")
                    return result
                # Inherited ANCESTOR node whose add-commit cannot be resolved
                # (e.g. `--follow` broken by a provenance-losing move/rename,
                # or history predating this clone) does NOT poison the whole
                # chain verdict — mirrors the untrailered-add-commit carve-out
                # below. Skip this node's segment attribution (it contributes
                # nothing to `covered`/`shas` — never a falsely-COVERED
                # verdict) and continue the walk; the gap is recorded in
                # `notes`, never silently absorbed into "walked and clean".
                result.notes.append(
                    f"{node}: no add-commit found (attribution gap) — "
                    "ancestor segment skipped (attributed to its own closing session)"
                )
                result.node_attribution[node] = _DagNodeAttribution(
                    path=node,
                    session_id="",
                    deliverable_id=_parse_handoff_deliverable_id(node),
                    shas=frozenset(),
                )
                continue

            # Session-Id trailer already resolved by
            # _resolve_add_commit_session_id above (`sid`) — no second
            # extraction call here (that would double the per-node spawn
            # count the batched-git-surface tests police).
            if not sid:
                if node == closing_abs:
                    # Preserved safety guard: the CLOSING/current session's own
                    # node (the --from-handoff node itself) must still hard-fail
                    # INDETERMINATE if its own add-commit is untrailered — this
                    # is the node THIS run is responsible for reviewing, not an
                    # inherited ancestor's segment.
                    result.indeterminate = True
                    result.notes.append(
                        f"{node}: add-commit {add_sha} has no Session-Id trailer"
                    )
                    return result
                # Inherited ANCESTOR node with an untrailered add-commit (e.g. a
                # spinoff-add commit authored outside the Session-Id-stamping
                # path) does NOT poison the whole chain verdict. Skip this
                # node's segment attribution (it contributes nothing to
                # `covered` — never a falsely-COVERED verdict) and continue the
                # walk; the ancestor's own commits are presumed reviewed by
                # whatever session actually closed that ancestor's handoff.
                result.notes.append(
                    f"{node}: ancestor add-commit {add_sha} untrailered — "
                    "segment skipped (attributed to its own closing session)"
                )
                # Attribution entry with no segment (AC1/AC2) — the node still
                # belongs in the rendered ancestry chain; it simply contributes
                # no commits (never falsely credited via a fabricated segment).
                result.node_attribution[node] = _DagNodeAttribution(
                    path=node,
                    session_id="",
                    deliverable_id=_parse_handoff_deliverable_id(node),
                    shas=frozenset(),
                )
                continue

        # Fidelity guard 1: UUID-shape validation BEFORE git grep interpolation.
        # A Session-Id containing regex metacharacters (e.g. '.*') interpolated
        # raw into git log --grep would over-match commits not in this session →
        # false COVERED verdict. Validate first; malformed → INDETERMINATE.
        if not _UUID_RE.match(sid):
            result.indeterminate = True
            result.notes.append(
                f"{node}: Session-Id {sid!r} failed UUID validation (fidelity guard 1)"
            )
            return result

        # Enumerate all commits in this session via Session-Id trailer grep.
        # --no-merges mirrors flat-range rationale: a merge commit carrying this
        # session's trailer must not enter the segment
        # and cause false UNCOVERED for chain commits that would otherwise be covered.
        #
        # Scoped to HEAD, not --all (2026-07-26 fix — see improvement-queue
        # 2026-07-01-chain-end-coverage-gate-dag-mode-over-at.yaml and
        # 2026-07-03-coverage-gate-dag-mode-misfires-on-prede.yaml). --all walks
        # EVERY ref in the repository -- every other local/remote branch, every
        # worktree's branch, every tag -- not just the history this DAG-mode
        # evaluation is actually reasoning about. A session-id trailer match on a
        # commit that lives on a wholly unrelated branch (a different feature
        # branch, an abandoned spike, another concurrent EM's worktree branch)
        # has no business entering THIS handoff's chain_set: it is not an
        # ancestor of the commit being closed out, so build_reviewed_set's
        # git-ancestry-based reviewed-set can never cover it, guaranteeing a
        # false UNCOVERED. HEAD is the correct reachability bound: DAG mode always
        # runs against the checkout that is mid-close, and every commit legitimately
        # attributable to this workstream must be an ancestor of HEAD by construction.
        # Does not by itself resolve same-branch concurrent-session contamination
        # (a sibling EM's commits interleaved on a shared work/* branch remain
        # reachable from HEAD too) -- that class is bounded by correct
        # Session-Id attribution instead, see closing_session_id threading below.
        seg = sid_to_shas.get(sid, [])

        if not seg:
            # Vacuous-match guard (mirrors bash AC14 logic): valid SID, zero commits
            # → INDETERMINATE (never silently COVERED on an empty match).
            result.indeterminate = True
            result.notes.append(
                f"{node}: Session-Id {sid!r} matched 0 commits (vacuous-match guard)"
            )
            return result

        # Deliverable-Id attribution (2026-07-27 fix): if this node's handoff
        # names a deliverable_id, prefer it over the plain Session-Id segment.
        # A Session-Id records who typed `git commit`, not what work the
        # commit was — a session that spins off a baton and also ships
        # unrelated work donates all of it to the spinoff's review obligation
        # under the old rule. See improvement-queue
        # 2026-07-01-chain-end-coverage-gate-dag-mode-over-at.yaml,
        # 2026-07-03-coverage-gate-dag-mode-misfires-on-prede.yaml, and the
        # 2026-07-27 example-retrieval-repo / example-cockpit-repo reports of the same shape.
        deliverable_id = _parse_handoff_deliverable_id(node)

        if deliverable_id is None:
            # No deliverable_id on this node — unchanged legacy behaviour.
            for sha in seg:
                seen_sha.add(sha)
            # AC1/AC2: record this node's own segment alongside the flat
            # union above — computed once, here, never recomputed by a caller.
            result.node_attribution[node] = _DagNodeAttribution(
                path=node,
                session_id=sid,
                deliverable_id=None,
                shas=frozenset(seg),
                authored_session_id=authored_sid,
                attribution_disagrees=attribution_disagrees,
            )
            continue

        # Fidelity guard 1 (deliverable-attribution variant): shape-validate
        # BEFORE interpolating into `git log --grep`, exactly as for
        # Session-Id above — an unvalidated id is the same injection hazard.
        # Malformed → INDETERMINATE, never fail-open to COVERED.
        if not _DELIVERABLE_ID_RE.match(deliverable_id):
            result.indeterminate = True
            result.notes.append(
                f"{node}: deliverable_id {deliverable_id!r} failed shape "
                "validation (fidelity guard 1, deliverable variant)"
            )
            return result

        # Leg (a): commits directly stamped with THIS deliverable_id. Scoped
        # to HEAD + --no-merges for the same reachability/merge-commit
        # reasons as the Session-Id segment query above.
        deliv_seg = did_to_shas.get(deliverable_id, [])

        # Leg (b): legacy-history fallback — commits already matched by
        # Session-Id that carry no Deliverable-Id trailer AT ALL, or that
        # carry one no WALKED node holds. This is the leg that makes the
        # change no-regression: every commit this gate has always attributed
        # via Session-Id keeps being attributed that way UNLESS it is now
        # stamped with a deliverable a *different walked* node holds (that
        # exclusion is the entire fix -- see the pre-pass comment above for
        # why "walked" is the correct scope, not "any"). A deliverable
        # belonging to an UNWALKED handoff does not exclude: this node's own
        # deliverable_id, if any, is also in `walked_deliverable_ids`, but a
        # commit stamped with it is already carried via `deliv_seg` above, so
        # excluding it here too is redundant, not incorrect -- the union
        # below is unaffected either way. One batched trailer-lookup call for
        # all of `seg`, not one subprocess per commit.
        # sha_to_did already covers every commit in `seg` (seg is itself
        # sourced from the same `git log HEAD --no-merges` walk that built
        # sha_to_did) — no separate `_commit_deliverable_id_trailers` spawn
        # needed per node any more (see the batched pre-pass above).
        #
        # Weak-authorship guard (AC1/AC2): an add-commit that touched more
        # than _BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD files is a bulk sweep,
        # not this node's authorship record — its Session-Id must not seed
        # leg (b). Only computed here (lazily, per node with a
        # deliverable_id) since it is only leg (b) that this guards; leg (a)
        # is untouched either way. `add_sha` is always populated by this
        # point for a node that reached here (both branches above resolve
        # it before `sid` can be truthy).
        is_bulk_sweep_add = False
        if add_sha:
            touched = _add_commit_touched_file_count(add_sha, repo_root)
            if touched is not None and touched > _BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD:
                is_bulk_sweep_add = True

        candidate_legacy_leg = [
            sha
            for sha in seg
            if not sha_to_did.get(sha) or sha_to_did[sha] not in walked_deliverable_ids
        ]

        if is_bulk_sweep_add:
            # AC4: fail toward current behaviour — the node itself does NOT
            # become INDETERMINATE, and leg (a) is untouched. Leg (b) simply
            # contributes nothing from this add-commit's session.
            legacy_leg: List[str] = []
            unattributable = candidate_legacy_leg
            if unattributable:
                # AC3/C2: report rather than drop — a distinct note category
                # ("unattributable-in-range") so a closer can tell "this
                # chain's inheritance" apart from "in range, foreign".
                result.notes.append(
                    f"{node}: add-commit {add_sha} touched more than "
                    f"{_BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD} files (bulk sweep) — "
                    f"leg (b) suppressed for session {sid!r}; "
                    f"{len(unattributable)} commit(s) unattributable-in-range "
                    f"(not this chain's, not dropped): {sorted(unattributable)}"
                )
                result.unattributable_shas.extend(
                    sha for sha in unattributable if sha not in result.unattributable_shas
                )
        else:
            legacy_leg = candidate_legacy_leg

        node_shas = set(deliv_seg) | set(legacy_leg)
        for sha in node_shas:
            seen_sha.add(sha)
        # AC1/AC2: record this node's own (deliverable-attributed) segment
        # alongside the flat union above — computed once, here.
        result.node_attribution[node] = _DagNodeAttribution(
            path=node,
            session_id=sid,
            deliverable_id=deliverable_id,
            shas=frozenset(node_shas),
            authored_session_id=authored_sid,
            attribution_disagrees=attribution_disagrees,
        )

    result.shas = list(seen_sha)
    return result


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
    module's AC-5 "no os.getcwd() fallback in multiplex/daemon-mode" — see
    ``run_coverage_gate`` docstring) for a code path nothing exercises; the
    dead bash-spawn site is deleted rather than ported.

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
    """Result of run_coverage_gate.

    verdict:       'COVERED' | 'WARN' | 'INDETERMINATE' (C10: the pre-C10
                   binary 'UNCOVERED' token no longer exists — see the
                   ratio/threshold assembly below). 'COVERED' when
                   uncovered_shas is empty OR coverage_ratio is at/above
                   _resolve_coverage_ratio_threshold(); 'WARN' below
                   threshold, carrying a remediation OFFER (dispatch
                   coordinator:review-code over uncovered_shas) rather than a
                   block — see run_coverage_gate's own module-level decision
                   note on what, if anything, still hard-blocks.
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
                   originating baton and print the ancestry chain — see
                   run_coverage_gate's DAG-mode UNCOVERED notes assembly, which
                   reads this field rather than recomputing segment membership
                   (the "state exactly once" property _derive_dag_chain_set's
                   docstring requires). Empty by default, same additive-field
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


# ---------------------------------------------------------------------------
# Open-review-loop diagnosis — four named end-states behind one UNCOVERED
#
# A long-run review loop (mise-en-place step 5: integrate -> re-freeze ->
# re-dispatch -> exit on zero findings) used to leave this gate with exactly
# one word for four materially different situations: a range nobody ever
# reviewed, a round somebody opened and abandoned mid-flight, a round that
# CONVERGED and was then followed by integration commits nobody re-froze
# (cockpit's `88d1cadd` shape — two batons both read "no gap here" for a commit
# no review ever covered), and a round frozen from the wrong base that left a
# gap BEHIND the review frontier. This section names which one it is, in
# CoverageResult.notes only.
#
# ADDITIVE-ONLY, and that is a hard invariant (AC4, plan
# docs/plans/2026-08-03-open-review-loops-are-a-named-gap.md § C3): nothing
# here touches `reviewed_set`, `uncovered_shas`, `covered`, the verdict, or the
# frozen AC11 verdict line. A pending record still credits ZERO coverage
# whether its round is closed or not, and all four states resolve
# VERDICT=UNCOVERED — the distinction is diagnostic, never a new way to pass.
# ---------------------------------------------------------------------------

#: The four end-state tokens, emitted VERBATIM in the diagnosis note. Consumed
#: by coordinator/tests/test_review_coverage_gate.py (plan § C4) and by the
#: outbound cross-repo memo to example-doctrine-repo-em that this vocabulary was promised
#: to (plan § C5) — renaming one is a cross-repo contract break, not a local
#: rewording.
_OPEN_LOOP_NEVER_REVIEWED = "never-reviewed"
_OPEN_LOOP_MID_ROUND = "loop-open-mid-round"
_OPEN_LOOP_AFTER_INTEGRATION = "loop-open-after-integration"
_OPEN_LOOP_GAP_BEHIND_FRONTIER = "gap-behind-frontier"

#: Note prefix carrying the state token, e.g.
#: "coverage: open-review-loop=loop-open-after-integration — ...". Keyed on by
#: token, not by prefix, so the trailing prose stays free to improve.
_OPEN_LOOP_NOTE_PREFIX = "coverage: open-review-loop="


def _make_chain_range_resolver(
    cwd: str,
    chain_set: Set[str],
    graph_range: Optional[str],
    prescan_ranges: Optional["Set[str]"] = None,
) -> Optional[Any]:
    """Build a `Callable[[str], Optional[Set[str]]]` resolving a trail record's
    sha_range to its CHAIN-RESTRICTED SHA set, for injection into
    `review_coverage_core.classify_pending_records` as its `resolve_range`.

    At most ONE `git rev-list --parents <graph_range>` spawn, built LAZILY on
    the first range actually resolved — a corpus with no pending record at all
    (every corpus predating § C1's freeze-time emission) short-circuits inside
    `classify_pending_records` before asking for a single range, and must not
    pay for a graph it never reads. After the build, resolution is the same
    machinery `_reviewed_via_graph_walk` credits with (`_resolve_endpoint` +
    `_reach_chain`, per-range set math, zero further spawns).

    Injecting this is the point: `classify_pending_records`' DEFAULT resolver
    shells out one `git rev-list` per DISTINCT range across the whole trail
    corpus (60+ records on a real corpus), which this gate's spawn-per-call
    budget cannot absorb.

    Returns None only when there is no window to walk at all — no `graph_range`
    (DAG mode derives chain_set from a segment fixpoint, so there is no single
    contiguous range). The caller then passes `resolve_range=None` and accepts
    the default per-range resolver, matching what the crediting path itself
    already does in that mode (`build_reviewed_set` Strategy B, the per-range
    fan-out). A graph BUILD failure degrades the same way, per-range and
    in-place, rather than reporting every record unresolvable.

    Negative-spec:
        - The returned sets are intersected with `chain_set`, exactly as the
          crediting path's are. Containment is therefore tested WITHIN the
          chain: a closer covering a pending round's in-chain commits closes it
          even if the round also froze commits outside this gate's range. That
          is the conservative direction for this diagnosis — it can only move a
          verdict-irrelevant note from `loop-open-mid-round` to
          `loop-open-after-integration`, never change `uncovered_shas`.
        - An unresolvable endpoint returns None ("unresolvable"), never a
          partial set — `classify_pending_records` reports such a record
          `resolved: False` / `closed: False` with a null closer, and a null
          set never intersects the uncovered commits. A FOREIGN endpoint (a
          valid ref off the window's lineage) is not unresolvable: it takes the
          same one-spawn-for-this-range-alone fallback the crediting path takes,
          because collapsing it to None would silently under-report open rounds
          on a side-branch freeze.
        - Does NOT reuse the parent map `build_reviewed_set` builds internally
          for the same window: that map is not returned, and threading it out
          would mean changing a function that feeds the verdict. One extra
          read-only spawn, only on the UNCOVERED path, is the deliberate price
          of keeping this chunk notes-only (AC4).

    Spec backlink: docs/plans/2026-08-03-open-review-loops-are-a-named-gap.md § C3
    """
    if not graph_range or not SAFE_RANGE.match(graph_range):
        return None

    graph_base = _range_base(graph_range)
    oow_cache = _OutOfWindowCache()
    symref_cache: Dict[str, Any] = {}
    memo: Dict[str, Optional[Set[str]]] = {}
    graph: Dict[str, Optional[Dict[str, List[str]]]] = {}

    def parent_map() -> Optional[Dict[str, List[str]]]:
        if "map" not in graph:
            built = _build_parent_map(cwd, graph_range)
            graph["map"] = built
            # Batched out-of-window pre-scan, the exact counterpart of
            # `_reviewed_via_graph_walk`'s (see `_out_of_window_hex_tokens`):
            # ONE `git cat-file --batch-check` resolves every token this
            # resolver will be asked about but cannot answer from the in-window
            # graph, instead of `_probe_out_of_window` paying a `git rev-parse`
            # spawn PER token. Deferred to here rather than run at construction
            # time so the "corpus with no pending record spawns nothing at all"
            # laziness this function's docstring promises survives — the map and
            # the pre-scan are built together, on the first range actually asked
            # for, or never.
            if built is not None and prescan_ranges:
                needs_probe = _out_of_window_hex_tokens(prescan_ranges, built)
                if needs_probe:
                    oow_cache.preload_hex_tokens(needs_probe, cwd)
        return graph["map"]

    def resolve_via_fanout(sha_range: str) -> Optional[Set[str]]:
        rc, shas_out, _ = _run(["git", "rev-list", sha_range], cwd=cwd)
        if rc != 0:
            return None
        return {s.strip() for s in shas_out.splitlines() if s.strip()} & chain_set

    def resolve(sha_range: str) -> Optional[Set[str]]:
        if sha_range in memo:
            return memo[sha_range]
        pmap = parent_map()
        result: Optional[Set[str]] = None
        symmetric = "..." in sha_range
        sep = "..." if symmetric else ".."
        if pmap is None:
            result = resolve_via_fanout(sha_range)
        elif sep in sha_range:
            left, right = sha_range.split(sep, 1)
            l_sha = _resolve_endpoint(
                left, pmap, cwd, "skip", symref_cache, graph_base, oow_cache
            )
            r_sha = _resolve_endpoint(
                right, pmap, cwd, "skip", symref_cache, graph_base, oow_cache
            )
            unusable = (_ENDPOINT_UNRESOLVED, _ENDPOINT_FOREIGN)
            if l_sha is _ENDPOINT_FOREIGN or r_sha is _ENDPOINT_FOREIGN:
                # The in-window graph cannot compute reach for a foreign
                # endpoint — same per-range fallback the crediting path takes
                # (_reviewed_via_graph_walk's MAJOR-2/MINOR-3 branch).
                result = resolve_via_fanout(sha_range)
            elif l_sha not in unusable and r_sha not in unusable:
                reach_l = _reach_chain(l_sha, pmap, chain_set)
                reach_r = _reach_chain(r_sha, pmap, chain_set)
                result = (reach_l ^ reach_r) if symmetric else (reach_r - reach_l)
        memo[sha_range] = result
        return result

    return resolve


def _reach_ancestors(
    seeds: Set[str], parent_map: Dict[str, List[str]], collect: Set[str]
) -> Set[str]:
    """`collect` members that are ancestors-or-self of ANY seed, in ONE
    multi-seed walk over `parent_map`.

    The multi-seed generalisation of `_reach_chain`: same traversal, same
    discipline of walking UNRESTRICTED (through commits absent from `collect`)
    while collecting only `collect` members, so ancestry survives the gaps in a
    non-contiguous set. DAG mode asks "is this commit an ancestor of ANY commit
    in a set", which `_reach_chain` can only answer one seed at a time — N full
    walks where one suffices.

    Unlike `_reach_chain`, seeds absent from `parent_map` are NOT collapsed to ∅:
    that collapse guards a caller-supplied foreign endpoint, and every seed here
    is read out of the map's own parent lists.

    Spec backlink: docs/plans/2026-08-03-open-review-loops-are-a-named-gap.md § C3c
    """
    seen: Set[str] = set()
    stack: List[str] = list(seeds)
    out: Set[str] = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node in collect:
            out.add(node)
        for parent in parent_map.get(node, ()):
            if parent not in seen:
                stack.append(parent)
    return out


def _dag_frontier_ancestry(
    cwd: str,
    reviewed_in_chain: Set[str],
    uncovered_set: Set[str],
) -> Optional[Tuple[Set[str], List[str], List[str]]]:
    """DAG mode's ANCESTRY answer to the question flat mode answers with
    `chain_ordered`: which uncovered commits sit BEHIND the reviewed frontier,
    and which sit off/ahead of it.

    The discrimination never needed a total commit order — it needs ancestry
    between the uncovered commits and the reviewed frontier, which is one
    `git rev-list --parents` seeded on the reviewed commits (the same primitive
    `_build_parent_map` uses, and the same one-spawn ceiling flat mode already
    pays through `_make_chain_range_resolver`). DAG mode has no contiguous
    `graph_range` to bound the walk, so the seeds are the reviewed commits
    themselves and the walk runs to the roots; fed over STDIN (`--stdin`), not
    argv, so a chain with thousands of reviewed commits cannot hit Windows'
    32K command-line ceiling.

    Returns None — caller keeps the coarse "reviewed history exists and these
    commits are not in it" fallback — when there is nothing to discriminate
    (no frontier, no uncovered commits) or git/the map is unusable. Otherwise
    returns:

      behind:       uncovered commits that are STRICT ancestors of some reviewed
                    commit, i.e. the gap-behind-frontier population. Its
                    complement within the uncovered commits is "not behind the
                    frontier" (newer than it, or on a concurrent line that never
                    reaches it) — flat mode's leg-2 population.
      frontier:     the MAXIMAL reviewed commits (each an ancestor of no other
                    reviewed commit), sorted. Exactly one element is the honest
                    DAG-mode reading of "the newest reviewed commit"; several
                    means concurrent reviewed heads and the note says so rather
                    than crowning one.
      oldest_behind: the MINIMAL elements of `behind` (each a descendant of no
                    other behind-commit), sorted — the re-freeze bases. Several
                    means the gap has concurrent tails and a single "at or
                    before X" would be a false instruction.

    Negative-spec:
        - Notes-only, like everything else in this section (AC4): nothing here
          is read by `reviewed_set`, `uncovered_shas`, the verdict or the frozen
          verdict line, and a total failure degrades the NOTE, never the gate.
        - Ancestry is not the same claim as chronology. A commit that is neither
          ancestor nor descendant of the frontier (a concurrent segment — DAG
          mode's chain_set is a union of session segments, so this is a real
          shape) is reported as NOT behind the frontier, because "re-freeze from
          further back" would not reach it. Commit dates are deliberately not
          consulted: they are not ancestry and survive neither rebase nor a
          skewed clock.
        - The walk sees only what the local object store holds: in a SHALLOW
          clone, ancestry below the graft boundary is unknown, and an uncovered
          commit there reads as not-behind-the-frontier (leg 2). Notes-only
          again — the wrong remedy sentence, never a wrong verdict.
        - Does NOT reuse `_derive_dag_chain_set`'s `ordered_ancestry` /
          `node_attribution` for this, though both are already in hand: those
          order handoff NODES and attribute commit SEGMENTS to them, which can
          only place a commit's whole segment relative to another segment. The
          shape this diagnosis exists to name (`88d1cadd`: an integration commit
          landing after a review inside ONE session's segment) is intra-node and
          invisible at that granularity.

    Spec backlink: docs/plans/2026-08-03-open-review-loops-are-a-named-gap.md § C3c
    """
    if not reviewed_in_chain or not uncovered_set:
        return None
    tips = sorted(sha for sha in reviewed_in_chain if _FULL_SHA.match(sha))
    if not tips:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-list", "--parents", "--stdin"],
            input="\n".join(tips) + "\n",
            cwd=cwd,
            capture_output=True,
            text=True,
            **_NO_CONSOLE,
        )
        lines = result.stdout.splitlines() if result.returncode == 0 else []
    except Exception:
        lines = []
    parent_map: Dict[str, List[str]] = {}
    for line in lines:
        parts = line.split()
        if parts:
            parent_map[parts[0]] = parts[1:]
    if not parent_map:
        return None

    reviewed_parents: Set[str] = set()
    for sha in reviewed_in_chain:
        reviewed_parents.update(parent_map.get(sha, ()))
    strict_ancestors_of_reviewed = _reach_ancestors(
        reviewed_parents, parent_map, uncovered_set | set(reviewed_in_chain)
    )

    behind = set(uncovered_set) & strict_ancestors_of_reviewed
    frontier = sorted(set(reviewed_in_chain) - strict_ancestors_of_reviewed)

    behind_parents: Set[str] = set()
    for sha in behind:
        behind_parents.update(parent_map.get(sha, ()))
    oldest_behind = sorted(behind - _reach_ancestors(behind_parents, parent_map, behind))

    if not frontier:
        return None
    return behind, frontier, oldest_behind


def _diagnose_open_review_loop(
    trail_paths: List[str],
    chain_set: Set[str],
    reviewed_set: Set[str],
    uncovered_shas: List[str],
    chain_ordered: List[str],
    cwd: str,
    graph_range: Optional[str],
) -> List[str]:
    """Classify an UNCOVERED result into exactly ONE of the four end-states
    above and return the note(s) naming it (AC3). Never returns an empty list
    on an UNCOVERED result, so "silence" stops being one of the gate's answers.

    Discrimination, in priority order — the first match wins, so exactly one
    token is ever emitted:

      1. `loop-open-mid-round` — an UNCLOSED pending record's resolved SHAs
         intersect the uncovered commits. A review was frozen and dispatched and
         never verdicted; the open round is the answer, and it outranks the
         others because an open round explains the gap.
      2. `loop-open-after-integration` — every pending round is closed (which
         includes the vacuous case of a corpus with no pending record at all —
         every record predating the freeze-time emission in § C1) AND at least
         one uncovered commit is NOT behind the review frontier in this chain
         (flat mode: newer than the newest reviewed commit; DAG mode: an
         ancestor of no reviewed commit). The `88d1cadd` shape: findings
         integrated, session ended before re-freezing.
      3. `gap-behind-frontier` — a review frontier EXISTS in this chain (at
         least one reviewed commit), no pending round is open over the uncovered
         commits, and EVERY uncovered commit is behind that frontier (flat mode:
         older than the newest reviewed commit; DAG mode: a strict ancestor of
         some reviewed commit). A past round froze from the wrong base and
         skipped over these:
         the remedy is re-freezing from FURTHER BACK, not freezing forward,
         which is why it cannot share `never-reviewed`'s token (plan § C3b).
      4. `never-reviewed` — the residual, meaning strictly what its name says:
         no reviewed commit in this chain at all, so these commits have no
         review round, open or closed. The prose distinguishes the sub-case
         where an unclosed pending record resolves into the chain without
         touching the uncovered commits; the remedy is the same either way.

    "After the newest reviewed commit" is decided against `chain_ordered` in
    flat mode — its own `git rev-list --no-merges` output, newest-first, already
    in hand at zero cost — and against ANCESTRY in DAG mode, where there is no
    single ordered chain (chain_set is a fixpoint over session segments) and
    `chain_ordered` is `[]`. All four legs are reachable in both modes; the two
    modes agree by construction on a linear history, where "not an ancestor of
    any reviewed commit" and "newer than the newest reviewed commit" are the
    same set.

    DAG mode's ancestry costs at most ONE extra `git rev-list --parents`
    (`_dag_frontier_ancestry`), spent only on an UNCOVERED DAG-mode run that
    got past leg 1 AND has a reviewed commit in the chain — a corpus with no
    frontier, or one whose gap is explained by an open round, pays nothing. If
    that spawn or its graph is unusable the pre-existing coarse fallback stands
    ("reviewed history exists and these commits are not in it" → leg 2), so a
    diagnostic failure still names one of the four tokens.

    What DAG mode still cannot say: nothing about which of the four states
    applies, but two prose refinements stay honest instead of guessing. With
    concurrent reviewed heads (several maximal reviewed commits) the note names
    the frontier as a set rather than crowning one commit "newest", and an
    uncovered commit on a line concurrent with the frontier — neither ancestor
    nor descendant — is reported as not behind it, since re-freezing further
    back would not reach it.

    Records carrying a literal-HEAD sha_range are dropped before classification,
    matching `build_reviewed_set`'s read-side exclusion
    (`_record_range_has_stored_head`): a record that cannot credit coverage must
    not be able to close a round either.

    Fail-visible, never fatal: any exception from the classification pass
    degrades to a single note naming the failure. A diagnostic must not be able
    to break the gate it annotates.

    Spec backlink: docs/plans/2026-08-03-open-review-loops-are-a-named-gap.md § C3
    """
    # Lazy import: coordinator_core.ops.review_coverage_core imports this
    # module's SAFE_RANGE/_parse_trail_file/_verdict_counts at module scope, so
    # a top-level import here would be circular. This is the only direction the
    # cycle can be broken from — that module is the one with the C2 contract.
    from coordinator_core.ops.review_coverage_core import classify_pending_records

    uncovered_set = set(uncovered_shas)
    try:
        all_records: List[Tuple[str, dict]] = []
        for path in trail_paths:
            try:
                records = _parse_trail_file(path)
            except _TrailParseError:
                continue
            for rec in records:
                if _record_range_has_stored_head(rec.get("sha_range", "")):
                    continue
                all_records.append((path, rec))

        # Hand the resolver the full set of ranges `classify_pending_records`
        # can ask it about (it resolves BOTH the pending records and every
        # candidate closer), so its out-of-window pre-scan is one batched
        # spawn rather than one `git rev-parse` per distinct token. A range
        # this set misses still resolves correctly — `_probe_out_of_window`
        # falls back to its per-token probe — so this is a spawn-count
        # optimisation with no bearing on any resolved value.
        resolver = _make_chain_range_resolver(
            cwd,
            chain_set,
            graph_range,
            prescan_ranges={
                str(rec.get("sha_range") or "")
                for _path, rec in all_records
                if rec.get("sha_range")
            },
        )
        pending_entries = classify_pending_records(
            all_records, resolve_range=resolver, cwd=cwd
        )
    except Exception as exc:  # diagnostic must never break the gate
        return [
            f"{_OPEN_LOOP_NOTE_PREFIX}unknown — open-review-loop diagnosis failed "
            f"({type(exc).__name__}: {exc}); the verdict above is unaffected."
        ]

    notes: List[str] = []
    total = len(uncovered_shas)

    open_rounds = [
        entry
        for entry in pending_entries
        if not entry["closed"] and (set(entry["shas"]) & uncovered_set)
    ]
    if open_rounds:
        covered_by_open = set()
        for entry in open_rounds:
            covered_by_open |= set(entry["shas"]) & uncovered_set
        artifacts = ", ".join(sorted(str(e["artifact"]) for e in open_rounds))
        notes.append(
            f"{_OPEN_LOOP_NOTE_PREFIX}{_OPEN_LOOP_MID_ROUND} — "
            f"{len(open_rounds)} pending review-trail record(s) cover "
            f"{len(covered_by_open)} of the {total} uncovered commit(s) and were "
            f"never verdicted: a review round was opened and left mid-flight. "
            f"Verdict the open round(s), do not re-freeze: {artifacts}"
        )
        return notes + _unresolved_pending_note(pending_entries)

    reviewed_in_chain = reviewed_set & chain_set
    newest_reviewed: Optional[str] = None
    after_integration: List[str] = []
    order: Dict[str, int] = {}
    #: Prose naming of the review frontier — flat mode's newest reviewed commit,
    #: or DAG mode's maximal reviewed commit(s). Empty string means no frontier
    #: could be PLACED (no reviewed commit in chain, or DAG ancestry unavailable),
    #: which is exactly what makes leg 2 fall back to its coarse anchor and leg 3
    #: step aside.
    frontier_anchor = ""
    #: The re-freeze base(s) for leg 3: the OLDEST uncovered commit(s) behind the
    #: frontier. Exactly one in flat mode (a total order has one oldest); DAG mode
    #: yields several when the gap has concurrent tails.
    behind_bases: List[str] = []
    #: How leg 2 relates its commits to the frontier. Flat mode's total order
    #: licenses "land after"; DAG-mode ancestry only licenses "is not behind",
    #: since a commit on a line concurrent with the frontier is neither.
    placement = "land after"
    if reviewed_in_chain:
        if chain_ordered:
            order = {sha: i for i, sha in enumerate(chain_ordered)}  # 0 == newest
            newest_reviewed = min(
                reviewed_in_chain,
                key=lambda sha: order.get(sha, len(chain_ordered)),
            )
            newest_idx = order.get(newest_reviewed, len(chain_ordered))
            after_integration = sorted(
                sha
                for sha in uncovered_shas
                if order.get(sha, len(chain_ordered)) < newest_idx
            )
            frontier_anchor = f"the newest reviewed commit {newest_reviewed[:9]}"
            behind_bases = sorted(
                uncovered_shas, key=lambda sha: order.get(sha, len(chain_ordered))
            )[-1:]
        else:
            # No chain ordering available (DAG mode) — ancestry against the
            # reviewed frontier answers the same question a total order would,
            # for one `git rev-list --parents`. See _dag_frontier_ancestry.
            ancestry = _dag_frontier_ancestry(cwd, reviewed_in_chain, uncovered_set)
            if ancestry is None:
                # No frontier placeable / git or graph unusable — the coarse
                # "reviewed history exists and these commits are not in it"
                # stand-in, so a failed refinement still names a token.
                after_integration = sorted(uncovered_shas)
            else:
                behind, frontier, behind_bases = ancestry
                after_integration = sorted(
                    sha for sha in uncovered_shas if sha not in behind
                )
                frontier_anchor = (
                    f"the newest reviewed commit {frontier[0][:9]}"
                    if len(frontier) == 1
                    else (
                        f"the review frontier ({len(frontier)} concurrent reviewed "
                        f"heads, incl. {frontier[0][:9]})"
                    )
                )
                placement = "are not behind"

    if after_integration:
        closed_rounds = [entry for entry in pending_entries if entry["closed"]]
        round_clause = (
            f"{len(closed_rounds)} pending round(s) closed by a later verdict record"
            if closed_rounds
            else "no pending round is open"
        )
        anchor = frontier_anchor or "the newest closed round's range"
        notes.append(
            f"{_OPEN_LOOP_NOTE_PREFIX}{_OPEN_LOOP_AFTER_INTEGRATION} — "
            f"{round_clause}, but {len(after_integration)} of {total} uncovered "
            f"commit(s) {placement} {anchor}: the "
            f"review loop was left open after an integration commit (nothing "
            f"re-froze the range). Re-freeze and review from "
            f"{after_integration[0][:9]} forward."
        )
        return notes + _unresolved_pending_note(pending_entries)

    if reviewed_in_chain and frontier_anchor:
        if len(behind_bases) == 1:
            remedy = (
                f"Re-freeze from further back, at or before "
                f"{behind_bases[0][:9]}, and review forward."
            )
        elif behind_bases:
            shown = ", ".join(sha[:9] for sha in behind_bases[:3])
            more = "" if len(behind_bases) <= 3 else ", ..."
            remedy = (
                f"Re-freeze from further back, at or before each of the "
                f"{len(behind_bases)} oldest uncovered commit(s) "
                f"({shown}{more}), and review forward."
            )
        else:
            remedy = "Re-freeze from further back and review forward."
        notes.append(
            f"{_OPEN_LOOP_NOTE_PREFIX}{_OPEN_LOOP_GAP_BEHIND_FRONTIER} — "
            f"review-trail records resolve into this range and no pending round "
            f"is open over these commits, but all {total} uncovered commit(s) "
            f"predate {frontier_anchor}: a past "
            f"review round froze from the wrong base and left a gap behind the "
            f"review frontier. {remedy}"
        )
        return notes + _unresolved_pending_note(pending_entries)

    touched_by_pending = any(set(entry["shas"]) & chain_set for entry in pending_entries)
    if touched_by_pending:
        detail = (
            "pending review-trail records resolve into this range but none covers "
            "these commits, and no round is open over them"
        )
    else:
        detail = "no review-trail record resolves into this range at all"
    notes.append(
        f"{_OPEN_LOOP_NOTE_PREFIX}{_OPEN_LOOP_NEVER_REVIEWED} — {detail}: all "
        f"{total} uncovered commit(s) have no review round, open or closed. "
        f"Freeze and review them."
    )
    return notes + _unresolved_pending_note(pending_entries)


def _unresolved_pending_note(pending_entries: List[Dict[str, object]]) -> List[str]:
    """Disclose pending records whose sha_range would not resolve locally (a
    cross-machine SHA, a rewritten branch) — they are structurally invisible to
    the three-way discrimination above, since a null SHA set intersects nothing.

    Emitted as a SEPARATE note rather than folded into the state note: the state
    note carries the token C4/the outbound memo key on, and must stay one line
    about one fact. Absent (empty list) in the overwhelmingly common case where
    every pending record resolved.
    """
    unresolved = [e for e in pending_entries if not e["resolved"]]
    if not unresolved:
        return []
    ranges = ", ".join(sorted(str(e["sha_range"]) for e in unresolved))
    return [
        f"coverage: {len(unresolved)} pending review-trail record(s) could not be "
        f"resolved locally and are excluded from the open-review-loop "
        f"classification above: {ranges}"
    ]


# ---------------------------------------------------------------------------
# Main gate function
#
# C10 hard-block decision (docs/plans/2026-08-05-coverage-gate-planning-
# artifact-class.md, AC14): for ORDINARY code-partition coverage, nothing
# hard-blocks, ever. Below-threshold is WARN — an offer (dispatch
# coordinator:review-code), never a refusal — and this module's own verdict
# never resolves to a halting token. The 100%-or-permanent-red-light posture
# this replaces was the PM's own words "the worst of both worlds": it trained
# reflex reach for COORDINATOR_OVERRIDE_COVERAGE_GATE=1 and protected nothing.
#
# This is a DELIBERATE, NAMED exception, not "nothing ever blocks anywhere":
# it does NOT extend to the PARTITION-MANDATORY chain-verdict case (a
# workstream closing with reviewers_required>=2 and zero reviews run — see
# workstream_complete/directives_review.py's chain_partition_verdict handling
# and _decide_review_scale). That is a distinct verdict over a distinct
# question (was the REQUIRED review scale discharged at all, not what
# fraction of commits carry a trail record) with its own live incident
# (example-doctrine-repo, 2026-08-05: a workstream reached a clean terminal stamp having
# run zero reviews). Fixing that gap is tracked separately at
# state/sizings/2026-08-06-partition-mandatory-must-refuse-the-chai.yaml and
# is explicitly NOT folded into this ratio/warn change.
# ---------------------------------------------------------------------------


def run_coverage_gate(
    range_arg: str = "",
    scope_paths: Optional[List[str]] = None,
    from_handoff: str = "",
    repo_root: Optional[str] = None,
    closing_session_id: str = "",
    verbose: bool = False,
) -> CoverageResult:
    """Run the coverage gate; return a CoverageResult.

    Flat mode (from_handoff is empty):
        range_arg → validated by SAFE_RANGE → git rev-list --no-merges [-- scope_paths]
        → chain_set. Reviewed_set built from trail records (never path-scoped).

    DAG mode (from_handoff provided):
        chain_set derived via _derive_dag_chain_set fixpoint (dag.walk_forward +
        archival.reverse_membership + segment attribution). range label = 'dag:<basename>'.
        When scope_paths is non-empty, chain_set is then narrowed via
        _filter_shas_by_scope_paths (git's own pathspec matcher over the
        already-derived SHAs) — the same "touches >=1 scoped path" semantics
        as flat mode's `git rev-list -- scope_paths`, applied as a post-filter
        rather than at derivation time. A git failure during this filter
        returns INDETERMINATE (never an unfiltered or empty fallback).

    Asymmetric scope filter: scope_paths is applied to chain_set in BOTH modes;
    reviewed_set is NEVER path-scoped, in either mode. Scoping narrows what
    gets checked for coverage — a stale or incomplete scope_paths pathspec
    means commits outside it are excluded from chain_set and never reviewed
    by this gate. The documented backstop is `/merging-to-main`, which always
    runs this gate UNSCOPED.

    Args:
        range_arg:           Git rev-range string (flat mode). Validated by SAFE_RANGE.
                             Default: resolved from git merge-base origin/main HEAD.
        scope_paths:         Path-scope chain_set (both flat and DAG mode). Never
                             applied to reviewed_set. See the scoping hazard note
                             above.
        from_handoff:        Absolute path to the closing handoff for DAG mode.
        repo_root:           Repository root for git calls. Required — os.getcwd() fallback
                             removed (AC-5: daemon cwd is not a valid repo root).
        closing_session_id:  Active session ID of the closing handoff (DAG mode D3
                             case 3; maps to $CLAUDE_CODE_SESSION_ID in bash).
        verbose:             When True, the bookkeeping-partition note includes the
                             full raw ``bookkeeping_shas`` list (previously always-on,
                             a Python-repr dump of N 40-char SHAs — see the note's
                             assembly below); default False emits a count instead.
                             Not yet reachable from the JSON-RPC wire params
                             (``coordinator_core.ops.coverage_gate`` does not forward
                             it) — a caller wanting the full list from the CLI must
                             invoke this function directly today.

    Returns:
        CoverageResult with verdict, verdict_line, exit_code, and diagnostics.
    """
    if not repo_root:
        raise ValueError(
            "coverage.run_coverage_gate: repo_root is required in multiplex daemon mode; "
            "os.getcwd() fallback removed (AC-5). Pass explicit repo_root."
        )
    cwd = repo_root
    scope_paths = scope_paths or []

    # -----------------------------------------------------------------------
    # Determine range label and chain_set
    # -----------------------------------------------------------------------
    notes: List[str] = []

    # graph_range enables the single-graph-walk in build_reviewed_set. Only flat mode
    # has a single contiguous chain-range to walk; DAG mode's chain_set comes from a
    # fixpoint over session segments (no single range), so it stays None → fan-out.
    graph_range: Optional[str] = None

    # Flat mode's own `git rev-list` output order (newest-first), kept for the
    # open-review-loop diagnosis' "lands after the newest reviewed commit" leg.
    # Zero cost — the ordering is already in hand; only `chain_set` discards it.
    # Empty in DAG mode, whose chain_set is a fixpoint over segments with no
    # single contiguous order (see _diagnose_open_review_loop).
    chain_ordered: List[str] = []

    if from_handoff:
        # --- DAG mode ---
        range_label = f"dag:{os.path.basename(from_handoff)}"
        dag_result = _derive_dag_chain_set(from_handoff, cwd, closing_session_id)
        if dag_result.indeterminate:
            notes.extend(dag_result.notes)
            vline = (
                f"range={range_label} chain_commits=0 covered=0 uncovered=0 "
                f"VERDICT=INDETERMINATE"
            )
            return CoverageResult(
                verdict="INDETERMINATE",
                verdict_line=vline,
                exit_code=2,
                notes=notes,
            )
        chain_set = set(dag_result.shas)
        # Review: code-reviewer P1 — dag_result.notes (e.g. the ancestor-skip
        # note) was previously merged into the outer `notes` list ONLY on the
        # indeterminate short-circuit above. On every other path (COVERED,
        # WARN) it was silently dropped, contradicting the "recorded in
        # notes, never silently absorbed" claim this fixpoint's docstring
        # makes. Merge unconditionally here so it survives every return.
        notes.extend(dag_result.notes)

        if scope_paths:
            filtered, filter_note = _filter_shas_by_scope_paths(
                dag_result.shas, scope_paths, cwd
            )
            if filtered is None:
                notes.append(filter_note)
                vline = (
                    f"range={range_label} chain_commits=0 covered=0 uncovered=0 "
                    f"VERDICT=INDETERMINATE"
                )
                return CoverageResult(
                    verdict="INDETERMINATE",
                    verdict_line=vline,
                    exit_code=2,
                    notes=notes,
                )
            chain_set = set(filtered)

        # AC1/AC2 threading: carried forward for the DAG-mode UNCOVERED
        # ancestry render below (never recomputed — see _DagChainResult's
        # own "state exactly once" note).
        dag_ordered_ancestry = dag_result.ordered_ancestry
        dag_node_attribution = dag_result.node_attribution
        dag_unattributable_shas = dag_result.unattributable_shas
        dag_closing_abs = os.path.abspath(from_handoff)
    else:
        # --- Flat mode ---
        dag_ordered_ancestry = []
        dag_node_attribution = {}
        dag_unattributable_shas = []
        dag_closing_abs = ""
        if not range_arg:
            # Default: git merge-base origin/main HEAD..HEAD
            rc, merge_base, _ = _run(
                ["git", "merge-base", "origin/main", "HEAD"],
                cwd=cwd,
            )
            if rc != 0 or not merge_base.strip():
                return CoverageResult(
                    verdict="INDETERMINATE",
                    verdict_line=(
                        "range=<unknown> chain_commits=0 covered=0 uncovered=0 "
                        "VERDICT=INDETERMINATE"
                    ),
                    exit_code=1,
                    notes=[
                        "cannot resolve origin/main — pass a range explicitly "
                        "(review-coverage-gate.sh)"
                    ],
                )
            range_arg = f"{merge_base.strip()}..HEAD"

        # Validate the caller-supplied range.
        if not SAFE_RANGE.match(range_arg):
            return CoverageResult(
                verdict="INDETERMINATE",
                verdict_line=(
                    f"range={range_arg} chain_commits=0 covered=0 uncovered=0 "
                    f"VERDICT=INDETERMINATE"
                ),
                exit_code=1,
                notes=[f"unsafe range argument: {range_arg!r}"],
            )

        range_label = range_arg
        # Flat mode has a single contiguous chain-range — hand it to build_reviewed_set
        # to enable the single-graph-walk. (SAFE_RANGE-validated just above.)
        graph_range = range_arg

        # Build flat-mode chain_set (asymmetric scope filter: chain IS scoped; :40-46).
        rev_list_cmd = ["git", "rev-list", "--no-merges", range_arg]
        if scope_paths:
            rev_list_cmd += ["--"] + scope_paths

        rc, chain_out, _ = _run(rev_list_cmd, cwd=cwd)
        if rc != 0:
            return CoverageResult(
                verdict="INDETERMINATE",
                verdict_line=(
                    f"range={range_label} chain_commits=0 covered=0 uncovered=0 "
                    f"VERDICT=INDETERMINATE"
                ),
                exit_code=1,
                notes=[f"git rev-list failed for range {range_arg!r}"],
            )
        chain_ordered = [sha.strip() for sha in chain_out.splitlines() if sha.strip()]
        chain_set = set(chain_ordered)

    # -----------------------------------------------------------------------
    # Short-circuit: empty chain → always COVERED
    # -----------------------------------------------------------------------
    if not chain_set:
        vline = (
            f"range={range_label} chain_commits=0 covered=0 uncovered=0 "
            f"coverage_ratio=1.00 VERDICT=COVERED"
        )
        return CoverageResult(
            verdict="COVERED",
            verdict_line=vline,
            exit_code=0,
            notes=notes,
            coverage_ratio=1.0,
        )

    # -----------------------------------------------------------------------
    # Build reviewed_set from trail records.
    # Asymmetric scope: reviewed_set is NEVER path-scoped.
    #
    # intersect_shas=chain_set is a LOAD-BEARING BOUND, not an optimisation —
    # it was described as one here until 2026-07-31, which is the description
    # that makes it look safe to drop. Crediting is range-based: a record's
    # resolved sha_range can span commits well outside the derived chain, and
    # `_narrow_foreign_session_scope`'s exclusion-based subtraction never
    # strips untrailered ones. Absent this intersection, a record admitted by
    # gate-minted chain-ancestry waivers credits its untrailered, interleaved,
    # non-chain commits for free — reproducing the 2026-07-27 defect-1
    # widening for exactly the population the waiver mechanism exists to
    # admit. This is AC11 of
    # docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md, and
    # it bounds the CREDIT rather than merely the refusal. Removing it on
    # performance grounds reopens a fail-open.
    # -----------------------------------------------------------------------
    trail_paths = _collect_trail_paths(cwd)
    reviewed_set = build_reviewed_set(
        trail_paths,
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=cwd,
        graph_range=graph_range,
    )

    # -----------------------------------------------------------------------
    # Compute verdict
    # -----------------------------------------------------------------------
    chain_list = list(chain_set)
    raw_uncovered_shas = [sha for sha in chain_list if sha not in reviewed_set]

    # -----------------------------------------------------------------------
    # Bookkeeping-vs-code partition (signal-honesty fix). A ceremony like
    # /workstream-complete necessarily authors its own bookkeeping commits
    # (completion entry, review-trail record, shipped_in stamp, boot sweep,
    # pickup-assemble claim) AFTER the trail record that would cover them —
    # those commits are structurally uncoverable by any trail record, but they
    # are also not code a reviewer could open. The VERDICT keys on the CODE
    # partition only; the bookkeeping partition is still reported (via
    # bookkeeping_shas + a note), never silently dropped. Fail-closed: a
    # mixed commit (bookkeeping path + any other path) classifies as CODE —
    # see _classify_bookkeeping_shas.
    # -----------------------------------------------------------------------
    touched_paths_cache: Dict[str, FrozenSet[str]] = {}
    bookkeeping_set, planning_set, bookkeeping_diagnostic_note = _classify_bookkeeping_shas(
        raw_uncovered_shas, cwd, touched_paths_cache
    )
    # Negative-spec — `planning_set` is deliberately NOT subtracted from
    # `uncovered_shas` here. PLANNING is not exempt from review (AC9): it owes a
    # PLAN review, not a code review, so excluding it at this seam would drop the
    # obligation rather than downgrade it — the vacuity direction 87578a319
    # exists to prevent. Carrying the third class through the result and the
    # envelope is C4's chunk; this call site only stops mis-unpacking the
    # three-way return.
    uncovered_shas = [sha for sha in raw_uncovered_shas if sha not in bookkeeping_set]
    bookkeeping_shas = [sha for sha in raw_uncovered_shas if sha in bookkeeping_set]
    # planning_shas is a SUBSET of uncovered_shas (never subtracted — see the
    # negative-spec above): every planning_set member already survived the
    # bookkeeping_set exclusion above, since exhaust_set/planning_set are
    # disjoint by construction (_classify_bookkeeping_shas' own docstring).
    planning_shas = [sha for sha in uncovered_shas if sha in planning_set]
    covered_count = len(chain_list) - len(uncovered_shas)

    # C3/AC6: classification skipped for >=1 commit (a chunked `git log`
    # call still failed even after batching under the Windows argv ceiling —
    # see _TOUCHED_PATHS_CHUNK/_commit_touched_paths) means the
    # bookkeeping/planning partition, and therefore the code-partition
    # coverage_ratio derived from it, rests on incomplete data for this run.
    # Recorded now, applied below (after the ordinary ratio/threshold
    # decision) as a hard downgrade to WARN — never a silent COVERED — so a
    # ratio computed over a corpus that failed to classify cannot read as a
    # clean finding.
    classification_skipped = bool(bookkeeping_diagnostic_note)
    if bookkeeping_diagnostic_note:
        notes.append(bookkeeping_diagnostic_note)

    if planning_shas:
        # Distinct wording from the bookkeeping/exhaust note above — PLANNING
        # is NOT excluded from the verdict (AC9): it stays inside uncovered_shas
        # and still owes a review, just a plan review rather than a code review.
        # A consumer must not be able to mistake this note for an exemption.
        if verbose:
            notes.append(
                "coverage: "
                f"{len(planning_shas)} uncovered commit(s) classified PLANNING "
                "(docs/plans/, docs/research/, docs/problems/, state/plan-sidecars/) — "
                "NOT exempt from review; each owes a plan review rather than a code "
                f"review, and remains counted in uncovered_shas/the VERDICT: {planning_shas!r}"
            )
        else:
            notes.append(
                "coverage: "
                f"{len(planning_shas)} uncovered commit(s) classified PLANNING — "
                "NOT exempt from review; each owes a plan review rather than a code "
                "review, and remains counted in uncovered_shas/the VERDICT (pass "
                "verbose=True to run_coverage_gate for the full sha list)."
            )

    if bookkeeping_shas:
        # Count by default (AC5) — the previous unconditional `{bookkeeping_shas!r}`
        # dumped N raw 40-char SHAs as one bracketed line directly adjacent to the
        # real uncovered list, the likely source of the "visually inseparable"
        # illegibility this render addresses. The fail-closed classification rule
        # itself (_classify_bookkeeping_shas) is untouched — this is render-only.
        if verbose:
            notes.append(
                "coverage: "
                f"{len(bookkeeping_shas)} uncovered commit(s) classified as ceremony "
                f"bookkeeping (ledger paths only, authoring no handoff) and excluded from the "
                f"verdict: {bookkeeping_shas!r}"
            )
        else:
            notes.append(
                "coverage: "
                f"{len(bookkeeping_shas)} uncovered commit(s) classified as ceremony "
                f"bookkeeping (ledger paths only, authoring no handoff) and excluded from the "
                f"verdict (pass verbose=True to run_coverage_gate for the full sha list)."
            )

    # AC6: an uncovered commit that touches >=1 bookkeeping path falls to CODE
    # under one of two DISTINCT rules in _classify_bookkeeping_shas, and the
    # note must name the one that actually fired:
    #   - the mixed-commit rule (>=1 bookkeeping path AND >=1 non-bookkeeping
    #     path) — the genuine "mixed" case, note wording frozen below.
    #   - the change-type leg (every touched path is bookkeeping, but the
    #     commit INTRODUCES a file under state/handoffs/ — see
    #     _handoff_authoring_shas) — `other_paths` is empty here, so the
    #     mixed-commit note's "AND non-bookkeeping path(s) ()" clause would be
    #     both false and self-contradictory; it gets its own note instead.
    # A sha reaches this loop with bk_paths non-empty and other_paths empty
    # only if it was excluded from bookkeeping_set by the change-type leg —
    # by_path already required every touched path to be bookkeeping, so its
    # absence from bookkeeping_set forces authoring membership, which in turn
    # guarantees a state/handoffs/ path among bk_paths. touched_paths_cache
    # already holds every raw_uncovered_shas' touched-path set (mutated in
    # place by _classify_bookkeeping_shas -> _commit_touched_paths above) —
    # reused here rather than re-spawning git or widening
    # _classify_bookkeeping_shas' return contract.
    for sha in sorted(uncovered_shas):
        touched = touched_paths_cache.get(sha, frozenset())
        bk_paths = sorted(p for p in touched if _is_bookkeeping_path(p))
        if not bk_paths:
            continue
        if sha in planning_set:
            # A planning commit that ALSO touches a bookkeeping path (e.g. a
            # plan-sidecar sync alongside a debt-backlog edit) falls to the
            # `other_paths`-non-empty branch below on a naive path-only
            # re-derivation, because docs/plans/ is deliberately not a
            # bookkeeping prefix — that would print the mixed-commit
            # sentence for a sha the mixed-commit rule never touched
            # (`_classify_bookkeeping_shas` already routed it to
            # `planning_set`). Check `planning_set` first and use its own
            # note, mirroring the aggregate PLANNING note's "NOT exempt"
            # framing: PLANNING owes a plan review, not a code review, and
            # stays inside uncovered_shas/the VERDICT.
            planning_paths = sorted(p for p in touched if _is_planning_artifact_path(p))
            notes.append(
                "coverage: "
                f"{sha} touches planning-artifact path(s) ({', '.join(planning_paths)}) "
                f"and bookkeeping path(s) ({', '.join(bk_paths)}) — classified PLANNING "
                "(not the fail-closed mixed-commit rule); it owes a plan review, not a "
                "code review, and remains counted in uncovered_shas/the VERDICT."
            )
            continue
        other_paths = sorted(p for p in touched if not _is_bookkeeping_path(p))
        if other_paths:
            notes.append(
                "coverage: "
                f"{sha} touches ceremony bookkeeping path(s) ({', '.join(bk_paths)}) "
                f"AND non-bookkeeping path(s) ({', '.join(other_paths)}) — classified "
                "CODE under the fail-closed mixed-commit rule (a commit must touch "
                "ONLY bookkeeping paths to be excluded from the verdict)."
            )
        else:
            handoff_paths = sorted(
                p for p in bk_paths if p.startswith(_HANDOFF_AUTHORING_PATH_PREFIX)
            )
            notes.append(
                "coverage: "
                f"{sha} touches only bookkeeping path(s) ({', '.join(bk_paths)}) but "
                f"introduces {', '.join(handoff_paths)} — classified CODE under the "
                f"handoff-authoring rule (a commit introducing a file under "
                f"{_HANDOFF_AUTHORING_PATH_PREFIX} is excluded from bookkeeping "
                "regardless of the path-prefix rule)."
            )

    # -----------------------------------------------------------------------
    # C10: coverage ratio over the CODE partition, replacing the binary
    # `verdict = "COVERED" if not uncovered_shas else "UNCOVERED"` test.
    #
    # code_partition_total excludes BOTH bookkeeping_shas (never owed a
    # review) and planning_shas (owed a PLAN review, not a code review — see
    # CoverageResult.planning_shas' docstring for why it stays counted in
    # uncovered_shas but must NOT count toward the code-ratio denominator).
    # code_uncovered is uncovered_shas minus its planning_shas subset — the
    # exact arithmetic CoverageResult.planning_shas' docstring already
    # names, reused here rather than re-derived a second way.
    #
    # Empty code partition (every uncovered commit was bookkeeping/planning,
    # or the chain had none to begin with) is vacuously 1.0 — mirrors the
    # pre-existing empty-chain-set short-circuit above, not a new "silently
    # returns COVERED for everything" hole: it only fires when there is
    # LITERALLY no code commit left to be uncovered about.
    # -----------------------------------------------------------------------
    code_partition_total = len(chain_list) - len(bookkeeping_shas) - len(planning_shas)
    code_uncovered = len(uncovered_shas) - len(planning_shas)
    if code_partition_total <= 0:
        coverage_ratio = 1.0
    else:
        coverage_ratio = (code_partition_total - code_uncovered) / code_partition_total

    threshold = _resolve_coverage_ratio_threshold()

    # C10/AC14: below threshold WARNs — carrying a remediation OFFER
    # (dispatch coordinator:review-code over uncovered_shas), never a block.
    # "Nothing hard-blocks" is this chunk's deliberate answer for ORDINARY
    # coverage (see the module-level decision note below the return); it does
    # NOT extend to the unrelated PARTITION-MANDATORY chain-verdict case
    # (workstream_complete/judgments.py's _decide_review_scale), which this
    # chunk's scope does not touch.
    if not uncovered_shas or coverage_ratio >= threshold:
        verdict = "COVERED"
    else:
        verdict = "WARN"
        notes.append(
            "coverage: "
            f"WARN — code-partition coverage ratio {coverage_ratio:.2f} is below "
            f"the {threshold:.2f} threshold. Nothing in this gate blocks on its "
            "own (see coordinator_core.coverage's module-level hard-block "
            "decision note) — but not blocking is not the same as not owed: "
            "the uncovered commits above are this chain's inheritance, and the "
            "remedy is to dispatch coordinator:review-code over them, then "
            "re-run this gate."
        )

    if classification_skipped and verdict == "COVERED":
        # AC6: never let an incomplete-classification run silently pass as
        # COVERED — the coverage_ratio above was computed over a corpus that
        # failed to classify for at least one commit, so a clean-looking
        # ratio here would misreport as a finding. Downgrade to WARN
        # (exit_code stays 0 — WARN never halts, see the module's own
        # decision note) rather than fabricate INDETERMINATE for what is a
        # partial-data condition, not a walk/derivation failure.
        verdict = "WARN"
        notes.append(
            "coverage: "
            "WARN — bookkeeping/planning classification was skipped for one "
            "or more commits (see the note above); the coverage_ratio below "
            "is computed over an INCOMPLETE partition and must not be read "
            "as a clean finding. Re-run once git log succeeds for the whole "
            "range."
        )

    vline = (
        f"range={range_label} chain_commits={len(chain_list)} "
        f"covered={covered_count} uncovered={len(uncovered_shas)} "
        f"coverage_ratio={coverage_ratio:.2f} VERDICT={verdict}"
    )

    # AC3: DAG-mode WARN gets the baton-ancestry render, in addition to
    # (never in place of — AC4) the frozen verdict line above. Renamed from
    # the pre-C10 "UNCOVERED" condition — WARN is this gate's only non-COVERED,
    # non-INDETERMINATE outcome now.
    if from_handoff and verdict == "WARN":
        notes.extend(
            _render_dag_ancestry_notes(
                dag_ordered_ancestry, dag_node_attribution, dag_closing_abs, uncovered_shas
            )
        )

    # AC3/AC4: name WHICH of the four open-review-loop end-states this run
    # hit, in notes only — appended last, after every pre-existing note. The
    # verdict and verdict_line above are already final at this point, by
    # construction: nothing below this line may read as feeding them.
    #
    # Gated on `uncovered_shas` (not `verdict == "WARN"`): C10 replaced the
    # binary UNCOVERED verdict with a coverage RATIO over the code partition
    # (DEFAULT_COVERAGE_RATIO_THRESHOLD = 0.66), so a corpus can carry
    # uncovered code commits yet still resolve COVERED once ratio clears
    # threshold — e.g. 2 reviewed commits + 1 unreviewed integration commit,
    # ratio=0.67. Gating on WARN alone left that exact band, the one this
    # diagnostic exists to police, silently undiagnosed. Firing whenever
    # `uncovered_shas` is non-empty catches it while a genuinely clean run
    # (uncovered_shas empty) still pays nothing — same spawn-cost bound as
    # before: at most one lazy graph-build spawn (the injected resolver)
    # plus DAG mode's one lazy frontier-ancestry spawn, ceiling two. Only a
    # COVERED-with-uncovered-commits run newly pays that cost.
    # `verdict == "WARN"` is deliberately NOT part of this predicate: WARN is
    # only ever assigned in the `else` branch above, which itself requires
    # `uncovered_shas` truthy — so `verdict == "WARN"` implies `uncovered_shas`
    # always, and the disjunct could never change this predicate's result.
    # `uncovered_shas` alone is the whole condition; if a future refactor adds
    # another path that sets verdict="WARN" without uncovered_shas, this gate
    # must be revisited, not silently reinstated as a disjunct.
    if uncovered_shas:
        notes.extend(
            _diagnose_open_review_loop(
                trail_paths,
                chain_set,
                reviewed_set,
                uncovered_shas,
                chain_ordered,
                cwd,
                graph_range,
            )
        )

    return CoverageResult(
        verdict=verdict,
        verdict_line=vline,
        chain_commits=len(chain_list),
        covered=covered_count,
        uncovered=len(uncovered_shas),
        exit_code=0,
        notes=notes,
        uncovered_shas=uncovered_shas,
        bookkeeping_shas=bookkeeping_shas,
        planning_shas=planning_shas,
        coverage_ratio=coverage_ratio,
        dag_ordered_ancestry=dag_ordered_ancestry,
        dag_node_attribution=dag_node_attribution,
        unattributable_shas=dag_unattributable_shas,
    )
