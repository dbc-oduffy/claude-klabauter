"""
coordinator_core.ops.session.resolve_chain_terminal_disposition —
session.resolve_chain_terminal_disposition op.

Purpose: native rewrite of the workstream-complete Step 0 session-shape
detection fence (example-doctrine-repo `coordinator/skills/workstream-complete/SKILL.md` Step 0,
the largest fence in the C0a corpus): resolve the calling session's id via the
5-way superset chain, then classify whether the session is chain-terminal
(it consumed a predecessor handoff) or single-session, via the dual-detector
scheme — live claim-stamp scan plus two OR'd archive detectors (archived
claim-stamp, git-provenance Session-Id trailer) with the foreign-consumer
restoration-commit spoof guard.  Per the parent plan's explicit mandate the
classification logic is RE-DERIVED as Python control flow — no grep/awk/case
transliteration survives; the ERE alternations collapse into shared-accessor
reads, the awk trailer parse into a line loop over `git log --name-status`.

Contract (B4 settlement — LOCKED post-DR-084, C6(iii) `cd1ca02a`,
contract 3.0.0): params `{session_id: Optional[str]}` →
`{disposition: str, chain_terminal: bool, evidence: dict}`.  `disposition`
uses ONLY the ratified DR-084 vocabulary — `open` / `claimed` / `continued` /
`closed` (+ `closed_reason` ∈ cancelled|displaced|stale in evidence) — never
`abandoned`, never `consumed`:

  - no claimed handoff found            → disposition "open",   chain_terminal False
    (the fence's `single-session` arm — no predecessor claim exists for this
    session, so its chain relationship is open/unclaimed)
  - live state/handoffs/ claim stamp    → disposition "claimed", chain_terminal True
  - archived handoff, deployment_state:
      "continued"                        → disposition "continued", chain_terminal True
      "closed"                           → disposition "closed",    chain_terminal True
                                            (closed_reason surfaced, enum-validated)
      "shipped" / non-terminal / absent  → disposition "claimed",   chain_terminal True
        (the claim stamp is the classification evidence; the record's own
        deployment_state rides in evidence untranslated — "shipped" is a
        ratified terminal but not a disposition token in the locked B4 enum)
      "abandoned" or any unknown token   → structured error (CC-7 — the DR-084
        C8 re-expression owns abandoned→continued/closed re-mapping via
        lineage inspection; a read op must never guess that mapping silently)

DR-084 single-accessor constraint (HARD): every lifecycle-field read
(claimed_by/consumed_by, and the spoof guard's preference read) routes through
the ONE shared claim accessor. As of the claim-state ledger-first plan's C5
chunk (docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md),
that accessor is `coordinator_core.claim_state.resolve_claim_state` (`_claim_holder`
below) — ledger-first with the tracked-frontmatter mirror as fallback, so a
branch-switch-desynced mirror no longer causes this op's dual detectors to miss
a live claim (this plan's own 2026-08-07 incident). `_claim_holder` still
contains zero raw `claimed_by`/`consumed_by` frontmatter reads and zero bare
status comparisons in THIS module, keeping
`coordinator/tests/test_dr084_single_accessor_guard.py`'s invariant intact —
the dual-tolerant `claimed_by`-wins-over-`consumed_by` reads now live inside
`coordinator_core.claim_state`'s mirror-fallback path, not here. Non-lifecycle
frontmatter fields (predecessor, deployment_state, closed_reason) read via the
shared `coordinator_core.ops._fm_util.extract_frontmatter_scalar`.

Session-id 5-way superset resolution (fence AC2b, behavior-preserving, with
the wire param as the direct-override tier):
  P1: params.session_id           — wire-param direct override (the op-call
                                    analog of the fence's `$em_sid` authority)
  P2: `em_sid` env var            — fence Priority 1 (Step 2.6.5 direct override)
  P3: `CLAUDE_SESSION_ID` env     — explicit test/brightline override; BEFORE
                                    CLAUDE_CODE_SESSION_ID, matching
                                    coordinator_core.ops.session_context
  P4: `CLAUDE_CODE_SESSION_ID`    — platform-injected, per-session
  P5: REMOVED (KS-3, 2026-08-07) — was the `.current-session-id` sentinel
      under <common_dir>/coordinator-sessions/. Unsound under concurrency
      (last-writer-wins across concurrent sessions sharing one worktree —
      see coordinator_core/bash_guards/guard_inprocess_search.py ~L84) AND
      its sole writer (session-init.py, the example-doctrine-repo SessionStart hook)
      was deleted by PM directive 2026-07-15 — no production writer
      survives. Falls through to P6 directly now.
  P6: REMOVED (KS-5, 2026-08-07) — was a last-6-digits-of-epoch fallback
      (the fence's `date +%s | tail -c 7 | head -c 6` last resort,
      natively `str(epoch)[-6:]`), fabricating a session id that names no
      session that ever existed — strictly worse than the P5 sentinel it
      sat below: a stale sentinel at least named a session that once
      existed, a fabricated epoch id is different on every invocation and
      indistinguishable to a downstream reader from a real id. With no
      env tier resolving, `_resolve_session_id` now returns "" (source
      "unresolved") and `_classify_sync` fails loud (CC-7 error result)
      instead of running the detectors against a value that cannot
      possibly match real claim-holder data — see `_classify_sync`'s
      unresolved-sid guard.

Scope keying: _OP_KEY_SCOPE = "common_dir" (B4 settlement, ratified —
`handoff.match_candidates` / `session.boot_sweep` precedent): reads
state/handoffs/ + archive/handoffs/ + git-provenance under the CALLER's repo
git_common_dir; a missing entry would classify claude-klabauter's own handoffs instead
of the caller's when invoked cross-repo from a example-doctrine-repo fence.

Idempotency (DEC-7 note, AC7): INHERENT — pure classification/read op; writes
nothing, spawns only read-only `git merge-base` / `git log` (list-argv, CC-1),
so re-invocation with identical inputs and unchanged disk returns the same
classification.  The CC-4 double-invocation test asserts exactly that.

Self-registration: importing this module calls
register_op("session.resolve_chain_terminal_disposition", _handler) as a
side-effect.  Registration surfaces (ops/__init__.py, op_scopes.py,
_registry_map.py) land in the separate EM-serial CC-3 pass — NOT this module's
landing commit.

Spec backlinks:
  - Settlement (binding): docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B4
  - Parent plan: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 3
  - Manifest row: state/audits/2026-07-22-command-payload-inventory/op-classification.tsv
    (op-key session.resolve_chain_terminal_disposition)
  - Fence source: example-doctrine-repo coordinator/skills/workstream-complete/SKILL.md Step 0 (~L55-176)
  - Vocabulary: example-doctrine-repo docs/decisions/DR-084-handoff-lifecycle-vocabulary-overhaul-open-claimed-continued-closed.md
  - Accessor: coordinator_core/claim_state.py::resolve_claim_state (ledger-first,
    C5 migration site — see docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md)

Negative-spec:
  - Does NOT write, stamp, archive, or mutate anything — classification only;
    the fence's `export WSC_*` side effect becomes the wire result.
  - Does NOT read claimed_by/consumed_by/claimed_at/consumed_at raw — every
    lifecycle read routes through the shared accessor (see above).
  - Does NOT re-map `abandoned` (or any unknown terminal token) to the new
    vocabulary — that is DR-084 C8's archival-sweep decision; this op
    fail-louds per CC-7 instead of guessing.
  - Does NOT spawn grep/awk/head/tail/sort or any shell interpreter — the two
    sanctioned subprocesses are read-only list-argv `git` invocations (CC-1).
  - Does NOT resolve any cross-repo path — the accessor load climbs to this
    module's OWN repo root (in-repo `__file__` climb, sanctioned by the parent
    plan's Mandated-resolvers boundary); all classified paths derive from the
    engine-supplied common_dir.
  - Does NOT prefer detector B over detector A — A (archived claim-stamp)
    wins, B fires only when A missed, and B's hit passes the spoof guard
    before it counts (fence semantics preserved).
  - Does NOT treat an absent claim holder on a Detector-B candidate as proof
    of this session's ownership, and does NOT treat a bulk/housekeeping
    archival commit (`fleet: archive …`, `session.boot_sweep: …`) as
    session-authored provenance — see the 2026-08-05 chain-terminal
    misattribution incident cited on `_git_provenance_shipped_handoff` and
    the ownership guard in `_classify_sync`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.ipc import register_op
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.win_portability import no_console_creationflags


_NO_WINDOW = no_console_creationflags()

_LOG = logging.getLogger(__name__)

# Ratified closed_reason subtype enum (B4 settlement / DR-084 addendum —
# `displaced`, not `superseded`).
_CLOSED_REASONS = frozenset({"cancelled", "displaced", "stale"})

# Archived deployment_state tokens that refine the disposition beyond the
# claim stamp.  Anything OUTSIDE this map that is also outside the
# still-"claimed" passthrough set (shipped / non-terminal / absent) is a
# CC-7 structured error — see _archived_disposition.
_DEPLOYMENT_TO_DISPOSITION = {
    "continued": "continued",
    "closed": "closed",
}

# Banned legacy tokens this op must never emit NOR silently re-map (DR-084).
_BANNED_DEPLOYMENT_TOKENS = frozenset({"abandoned"})

# Commit-subject prefixes emitted by automated bulk/housekeeping archival
# machinery, never by a session's own targeted action on its own predecessor
# (2026-08-05 chain-terminal misattribution incident, this session's own
# reproduction: a `session.boot_sweep` run inside the calling session
# archived ANOTHER session's handoff, and the resulting `fleet: archive 1
# completed handoff(s)` commit carried the calling session's Session-Id
# trailer purely because the sweep ran inside it — Detector B read that
# trailer as "I did this" and misattributed the archival).  Verified against
# the literal emitted text, not paraphrased:
#   - "fleet: archive {n} completed handoff(s)"  — fleet.archive_completed_handoffs
#     (coordinator_core/ops/fleet/archive_handoffs.py)
#   - "fleet: archive {n} shipped handoff(s)"    — fleet.archive_shipped_handoffs
#     (coordinator_core/ops/fleet/archive_shipped_handoffs.py)
#   - "session.boot_sweep: stamp {n} consumed handoff(s) metadata …" —
#     session.boot_sweep's own sibling metadata-stamp commit
#     (coordinator_core/ops/session/boot_sweep.py)
# `fleet.archive_completed_handoffs` and `fleet.archive_shipped_handoffs` are
# ALSO the exact same handlers `session.boot_sweep` delegates to for its own
# housekeeping pass — the commit shape is byte-identical whether the sweep
# ran standalone or nested inside this session, so subject text is the only
# signal available at the git-log layer; no distinguishing trailer exists
# (verified: archive_and_commit, coordinator_core/ops/fleet/_common.py,
# stamps no automation marker beyond the ambient Session-Id trailer already
# consulted above).  This mirrors the SAME prefixes' pre-existing treatment
# as non-evidentiary in coordinator_core/reconcile/commit_reality.py's
# `_DEFAULT_MECHANICAL_DENYLIST` (a handoff's "most recent toucher" being the
# fleet-archive sweep is already a known false-signal shape there).
#
# This gate is deliberately independent of the ownership check below (a
# sweep-shaped commit is rejected even when the swept record's claim holder
# happens to equal this session — a stray legitimate claim swept up in a
# bulk pass is still not evidence the SESSION performed the archival itself).
_SWEEP_ATTRIBUTED_SUBJECT_PREFIXES: tuple = (
    "fleet: archive ",
    "session.boot_sweep: ",
)


def _is_sweep_attributed_subject(subject: str) -> bool:
    """True when `subject` (a commit's first-line message) matches a known
    automated bulk/housekeeping archival prefix — see
    _SWEEP_ATTRIBUTED_SUBJECT_PREFIXES above."""
    return any(subject.startswith(prefix) for prefix in _SWEEP_ATTRIBUTED_SUBJECT_PREFIXES)

_GIT_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# Shared DR-084 accessor (coordinator/bin/lib/handoff_lifecycle.py) — loaded
# via coordinator_core.session.claims.handoff_lifecycle(), the single shared
# loader (both this module and coordinator_core.ops.handoff_author_fork
# previously carried independent copies of this importlib-load boilerplate;
# consolidated so a rename of the accessor's own load path has one edit site).
# ---------------------------------------------------------------------------


def _claim_holder(path: Path, common_dir: Optional[Path] = None) -> str:
    """Single routed lifecycle read: `coordinator_core.claim_state`'s
    ledger-first accessor (C1) resolved over a handoff file path, threading a
    pre-resolved `common_dir` where the caller already has one in hand (hot
    path — avoids a second `git_common_dir` resolution per call).  Returns ""
    when neither the ledger nor the frontmatter mirror carries a value
    (accessor contract) — migrated off the frontmatter-only
    `handoff_lifecycle.claim_holder` per this plan's C5 chunk so a
    branch-switch-desynced mirror no longer misses a live ledger claim (see
    module docstring's 2026-08-05 misattribution incident and this plan's own
    2026-08-07 branch-switch-desync incident)."""
    state = resolve_claim_state(path, common_dir=common_dir)
    return state.holder or ""


# ---------------------------------------------------------------------------
# Session-id resolution (5-way superset + wire param)
# ---------------------------------------------------------------------------


def _resolve_session_id(
    param_sid: Optional[str], common_dir: Path, environ: dict
) -> Tuple[str, str]:
    """Resolve the session id; returns (sid, source_label).

    Tier order documented in the module docstring (P1..P6).  Env reads take a
    caller-supplied mapping (os.environ at the call site) so tests can inject
    without process-global mutation races.
    """
    if param_sid and str(param_sid).strip():
        return str(param_sid).strip(), "param"
    for env_var, label in (
        ("em_sid", "em_sid"),
        ("CLAUDE_SESSION_ID", "CLAUDE_SESSION_ID"),
        ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"),
    ):
        val = str(environ.get(env_var, "") or "").strip()
        if val:
            return val, label
    # P5 (`.current-session-id` sentinel) REMOVED — KS-3, 2026-08-07: unsound
    # under concurrency (last-writer-wins) AND its sole writer was deleted
    # 2026-07-15 (PM directive). See module docstring for detail.
    # P6 (epoch-tail fabricated-id fallback) REMOVED — KS-5, 2026-08-07: a
    # fabricated id is different on every invocation and indistinguishable
    # from a real one downstream, which turned an unidentifiable session
    # into a passing "open"/not-terminal disposition (the same false-clean
    # failure mode the P5 removal fixed, reached by a different route — see
    # module docstring). No tier resolved; report unresolved honestly and
    # let `_classify_sync` fail loud instead.
    return "", "unresolved"


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _scan_claimed_by_session(
    scan_root: Path, sid: str, common_dir: Optional[Path] = None
) -> List[Path]:
    """Return every *.md under scan_root (recursive, sorted) whose accessor-read
    claim holder equals sid.  Sorted for determinism — a deliberate native
    tightening of the fence's fs-order `grep -rl | head -1` primary scan; its
    detector-A arm already piped through `sort`.  `common_dir` threads through
    to the ledger-first accessor (C1) so callers with a pre-resolved
    `common_dir` (the hot path) never trigger a second resolution."""
    if not scan_root.is_dir():
        return []
    hits: List[Path] = []
    for path in sorted(scan_root.rglob("*.md")):
        if not path.is_file():
            continue
        if _claim_holder(path, common_dir) == sid:
            hits.append(path)
    return hits


def _has_predecessor_field(path: Path) -> bool:
    """Well-formed-handoff guard: the file's frontmatter carries a
    `predecessor:` field (any value, including "none" — a session that
    consumed a root handoff via /pickup IS legitimately chain-terminal)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return extract_frontmatter_scalar(text, "predecessor") is not None


def _origin_session(path: Path) -> Optional[str]:
    """Non-lifecycle frontmatter read (routed via the shared `_fm_util`
    accessor, not the DR-084 lifecycle accessor — `origin_session` is not a
    claim-holder field): the record's own `origin_session:` value, when
    present.  A UUID naming the session that ORIGINATED the record (schema
    rule C2-1c, coordinator_core/frontmatter/schema_validate.py) — directly
    comparable to a resolved session id, unlike `authoring_session` (observed
    path-shaped, not session-id-shaped, in this corpus — deliberately NOT
    consulted here for that reason).  Returns None when absent/unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return extract_frontmatter_scalar(text, "origin_session")


def _run_git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Read-only list-argv git invocation (CC-1: named binary, no shell)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
        **_NO_WINDOW,
    )


#: Unit-separator byte (ASCII 0x1F) delimiting the marker line's fields —
#: chosen because it cannot occur in a commit subject or a Session-Id trailer
#: value, unlike whitespace (the prior format's split-on-whitespace parse).
_FIELD_SEP = "\x1f"


def _git_provenance_shipped_handoff(
    worktree: Path, sid: str, warnings: List[str], notes: List[str]
) -> Optional[str]:
    """Detector B: first archive/handoffs/*.md path added/renamed/copied by a
    commit carrying THIS session's Session-Id trailer since the origin/main
    merge-base, EXCLUDING commits whose subject matches a known automated
    bulk/housekeeping archival prefix (_SWEEP_ATTRIBUTED_SUBJECT_PREFIXES —
    2026-08-05 misattribution incident: a `session.boot_sweep` run nested
    inside this session archived ANOTHER session's handoff under this
    session's own Session-Id trailer).  Returns the repo-relative posix path
    or None.  A rejected sweep-attributed candidate is routed to `notes` (not
    silently dropped) and scanning continues past it — see the module's
    "Do not break the legitimate path" framing: a genuinely session-authored
    archival commit elsewhere in the window must still be found.

    Native re-derivation of the fence's `git log … --format='__SID__ %(trailers…)'
    | awk` pipeline, widened to also carry the commit subject on the same
    marker line (via the `_FIELD_SEP` delimiter): the marker line flips a
    per-commit match flag and records that commit's subject; name-status
    lines under a matching commit are tested on their LAST tab-field (awk
    $NF — the post-rename path for R rows, made explicit by --find-renames).
    """
    merge_base = _run_git(["merge-base", "origin/main", "HEAD"], worktree)
    base_sha = (merge_base.stdout or "").strip()
    if merge_base.returncode != 0 or not base_sha:
        warnings.append(
            "could not resolve origin/main merge-base — Detector B "
            "(git-provenance chain-terminal detection) skipped; if this session "
            "shipped/archived a handoff, chain-terminal may be missed"
        )
        return None
    log = _run_git(
        [
            "log",
            f"{base_sha}..HEAD",
            "--diff-filter=ARC",
            "--find-renames",
            "--name-status",
            # NOTE the field order: `%(trailers:…,valueonly)` MUST be last.
            # git's trailers formatter appends its OWN trailing newline after
            # the value (present even for a single trailer, unlike a plain
            # `%s`/literal token) — placing it mid-format would silently
            # truncate this marker line's later fields (verified live: with
            # the trailer placed before %s, `commit_subject` always parsed
            # as empty). Subject (`%s`, never itself multi-line) is safe in
            # the middle.
            "--format="
            f"__SID__{_FIELD_SEP}%s{_FIELD_SEP}%(trailers:key=Session-Id,valueonly)",
        ],
        worktree,
    )
    if log.returncode != 0:
        warnings.append(
            "git log for Detector B failed "
            f"(rc={log.returncode}) — git-provenance detection skipped"
        )
        return None
    commit_is_mine = False
    commit_subject = ""
    for line in log.stdout.splitlines():
        if line.startswith("__SID__"):
            fields = line.split(_FIELD_SEP)
            commit_subject = fields[1].strip() if len(fields) > 1 else ""
            trailer_sid = fields[2].strip() if len(fields) > 2 else ""
            commit_is_mine = bool(trailer_sid) and trailer_sid == sid
            continue
        if not commit_is_mine or "\t" not in line:
            continue
        last_field = line.split("\t")[-1].strip()
        if not (
            last_field.startswith("archive/handoffs/") and last_field.endswith(".md")
        ):
            continue
        if _is_sweep_attributed_subject(commit_subject):
            notes.append(
                f"Detector B candidate {last_field} rejected — archiving commit "
                f"subject ({commit_subject!r}) matches a known automated "
                "bulk/housekeeping archival prefix, not evidence this session "
                "performed the archival itself (2026-08-05 chain-terminal "
                "misattribution incident: a session.boot_sweep run nested "
                "inside this session carries this session's Session-Id "
                "trailer regardless of whose handoff it swept)"
            )
            continue
        return last_field
    return None


# ---------------------------------------------------------------------------
# Disposition mapping (B4 locked vocabulary)
# ---------------------------------------------------------------------------


def _archived_disposition(path: Path, relpath: str) -> Tuple[dict, Optional[dict]]:
    """Map an archived, claim-verified handoff to its locked-vocabulary
    disposition.  Returns (fields, error) where exactly one is meaningful:
    fields = {disposition, deployment_state, closed_reason} on success,
    error = the exit_code:1 envelope on a CC-7 unclassifiable token.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    deployment_state = extract_frontmatter_scalar(text, "deployment_state")
    if deployment_state in _BANNED_DEPLOYMENT_TOKENS:
        return {}, _error_result(
            f"archived handoff {relpath} carries banned legacy "
            f"deployment_state '{deployment_state}' — DR-084 C8 re-expression "
            "(continued-with-lineage vs closed+stale) is an archival-sweep "
            "decision, not this read op's to guess (CC-7 fail-loud)"
        )
    if deployment_state in _DEPLOYMENT_TO_DISPOSITION:
        disposition = _DEPLOYMENT_TO_DISPOSITION[deployment_state]
        closed_reason = None
        if disposition == "closed":
            closed_reason = extract_frontmatter_scalar(text, "closed_reason")
            if closed_reason is not None and closed_reason not in _CLOSED_REASONS:
                return {}, _error_result(
                    f"archived handoff {relpath} carries closed_reason "
                    f"'{closed_reason}' outside the ratified enum "
                    f"{sorted(_CLOSED_REASONS)} (CC-7 fail-loud)"
                )
        return {
            "disposition": disposition,
            "deployment_state": deployment_state,
            "closed_reason": closed_reason,
        }, None
    # shipped / non-terminal / absent: the claim stamp is the classification;
    # the record's own deployment_state rides in evidence untranslated.
    return {
        "disposition": "claimed",
        "deployment_state": deployment_state,
        "closed_reason": None,
    }, None


# ---------------------------------------------------------------------------
# Classification core (sync — runs off the event loop)
# ---------------------------------------------------------------------------


def classify_chain_terminal_disposition(
    common_dir: Path, param_sid: Optional[str], environ: dict
) -> dict:
    """Public entrypoint for the classification core — thin wrapper around
    `_classify_sync`, added so callers OUTSIDE this module (e.g.
    `coordinator_core.chain_ancestry_waivers.chain_reached_terminal_close`)
    have a documented, non-underscored seam onto the SAME classification
    logic the op handler uses, rather than reaching into a private name.

    Calling with an explicit `param_sid` (the `param_sid` tier) bypasses the
    5-way env-based session-id resolution entirely and classifies that exact
    id — the shape a caller with an explicit chain_id in hand wants, with no
    env-var ambient-session confusion. See `_classify_sync` for the full
    contract (disposition vocabulary, evidence shape, CC-7 error path).
    """
    return _classify_sync(common_dir, param_sid, environ)


def _classify_sync(common_dir: Path, param_sid: Optional[str], environ: dict) -> dict:
    """Full Step-0 classification: resolve sid, run the dual detectors + spoof
    guard, map to the locked disposition vocabulary."""
    worktree = main_worktree_root(common_dir)
    sid, sid_source = _resolve_session_id(param_sid, common_dir, environ)

    warnings: List[str] = []
    notes: List[str] = []
    evidence: dict = {
        "session_id": sid,
        "session_id_source": sid_source,
        "detector": None,
        "consumed_handoff": None,
        "deployment_state": None,
        "closed_reason": None,
        "warnings": warnings,
        "notes": notes,
    }

    # Unresolved-sid guard (KS-5): with the P6 fabricated-epoch fallback
    # gone, an unresolvable sid is "" — never let that empty value flow into
    # the detectors below. `_claim_holder` returns "" for a genuinely
    # unclaimed handoff too, so `"" == sid` would spuriously match every
    # unclaimed record as "claimed by this session", and even without that
    # false-match risk, no detector could possibly hit against a value that
    # names no session — the fall-through would read as a clean "open"/
    # not-terminal verdict identical to the false clean the P6 removal was
    # fixing. Fail loud instead (CC-7 shape) so this can never read as a
    # passing chain-end coverage gate.
    if not sid:
        error = _error_result(
            "session id could not be resolved via any tier (param/em_sid/"
            "CLAUDE_SESSION_ID/CLAUDE_CODE_SESSION_ID) — refusing to classify "
            "chain-terminal disposition against an unidentified session "
            "(KS-5: the fabricated-epoch fallback that used to paper over "
            "this was removed as strictly worse than reporting unresolved)"
        )
        error["evidence"] = evidence
        return error

    def _result(disposition: str, chain_terminal: bool) -> dict:
        return {
            "exit_code": 0,
            "disposition": disposition,
            "chain_terminal": chain_terminal,
            "evidence": evidence,
        }

    def _rel(path: Path) -> str:
        try:
            return path.resolve().relative_to(worktree.resolve()).as_posix()
        except ValueError:
            return str(path)

    # Primary detector — live state/handoffs/ claim stamp.
    live_hits = _scan_claimed_by_session(
        worktree / "state" / "handoffs", sid, common_dir
    )
    if live_hits:
        evidence["detector"] = "live_claim_stamp"
        evidence["consumed_handoff"] = _rel(live_hits[0])
        return _result("claimed", True)

    # Detector A — archived handoff naming this session as claim holder, with
    # the well-formed-handoff predecessor guard; first (sorted) hit wins.
    arch_hit: Optional[Path] = None
    for candidate in _scan_claimed_by_session(
        worktree / "archive" / "handoffs", sid, common_dir
    ):
        if _has_predecessor_field(candidate):
            arch_hit = candidate
            break
    if arch_hit is not None:
        evidence["detector"] = "archive_claim_stamp"

    # Detector B — git provenance (only consulted when A missed; the fence
    # computes it eagerly but only CONSUMES it on the A-miss branch, so the
    # native form defers the subprocess spawns to that branch).
    shipped_by_me: Optional[str] = None
    if arch_hit is None:
        shipped_by_me = _git_provenance_shipped_handoff(worktree, sid, warnings, notes)
        if shipped_by_me is not None:
            shipped_path = worktree / Path(shipped_by_me)
            # Ownership guard (widened 2026-08-05 — chain-terminal
            # misattribution incident, this session's own reproduction): a
            # commit carrying this session's Session-Id trailer is evidence
            # the trailer-stamping HOOK ran inside this session, never proof
            # this SESSION performed the archival — a nested automated sweep
            # (excluded above by subject) or a restoration/fix commit for
            # ANOTHER session's record (the original 2026-07-22 spoof-guard
            # incident) both carry it just as validly. Two independent
            # reads, EITHER of which rejects:
            #   - accessor PREFERENCE read (claim_holder): reject when it
            #     names a different session (2026-07-22 spoof guard, as
            #     before) — but an ABSENT claim holder is the absence of
            #     evidence, not proof of ownership, and must not fall
            #     through to acceptance by default (the 2026-08-05 bug: this
            #     record had no claim holder at all and was accepted anyway).
            #   - `origin_session:` (non-lifecycle field, additional negative
            #     evidence the prior guard never consulted): reject when
            #     present and naming a different session — the exact signal
            #     this session's own reproduction carried and the prior guard
            #     ignored because `consumer` was falsy.
            # Neither field being SET is not itself disqualifying (the
            # covered legitimate shape: a session ships/archives its own
            # predecessor with no claim ever stamped and no origin_session
            # recorded either) — only a field being set AND naming someone
            # else rejects.
            consumer = _claim_holder(shipped_path, common_dir)
            origin = _origin_session(shipped_path)
            if consumer and consumer != sid:
                notes.append(
                    f"Detector B hit {shipped_by_me} rejected — claim holder "
                    f"({consumer}) is another session (restoration-commit "
                    "spoof guard)"
                )
            elif origin and origin != sid:
                notes.append(
                    f"Detector B hit {shipped_by_me} rejected — no claim "
                    f"holder names this session, and origin_session ({origin}) "
                    "names a different session (unproven ownership fails "
                    "closed — absence of a claim holder is not evidence of "
                    "this session's ownership; 2026-08-05 chain-terminal "
                    "misattribution incident)"
                )
            elif _has_predecessor_field(shipped_path):
                arch_hit = shipped_path
                evidence["detector"] = "git_provenance"
                notes.append(
                    "chain-terminal resolved from archive (shipped/archived "
                    f"without a live consume): {shipped_by_me}"
                )
            else:
                notes.append(
                    f"Detector B hit {shipped_by_me} lacked a predecessor "
                    "frontmatter field (not a well-formed handoff) — not counted"
                )

    if arch_hit is not None:
        rel = _rel(arch_hit)
        fields, error = _archived_disposition(arch_hit, rel)
        if error is not None:
            error["evidence"] = evidence
            evidence["consumed_handoff"] = rel
            return error
        evidence["consumed_handoff"] = rel
        evidence["deployment_state"] = fields["deployment_state"]
        evidence["closed_reason"] = fields["closed_reason"]
        return _result(fields["disposition"], True)

    if shipped_by_me is not None:
        # Fence-parity WARN: Detector B saw this session archive a handoff,
        # but no detector produced an accepted hit — the caller's chain-end
        # coverage gate will be skipped; surface loudly, never silently.
        warnings.append(
            f"session {sid} archived a handoff this run ({shipped_by_me}) but "
            "disposition resolved single-session — chain-end coverage gating "
            "will be SKIPPED; if chain-terminal, re-invoke with an explicit "
            "session_id or classify manually"
        )
    return _result("open", False)


# ---------------------------------------------------------------------------
# Wire error envelope
# ---------------------------------------------------------------------------


def _error_result(reason: str) -> dict:
    """Build a structured-error result (exit_code:1) — nothing was classified."""
    _LOG.error("session.resolve_chain_terminal_disposition: %s", reason)
    return {
        "exit_code": 1,
        "error": reason,
        "disposition": None,
        "chain_terminal": False,
        "evidence": {},
    }


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("session.resolve_chain_terminal_disposition")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.resolve_chain_terminal_disposition — Step-0 session-shape classification.

    Pure read op: resolves the calling session's id (5-way superset chain +
    wire-param override) and classifies its chain-terminal disposition via the
    dual-detector scheme (live claim-stamp; archived claim-stamp OR
    git-provenance with the foreign-consumer spoof guard).

    params:
      session_id (str, optional): direct override — highest-priority tier of
                                  the resolution chain (the fence's `$em_sid`
                                  authority, in wire-param form).
      repo_root  (str, optional): D3 consistency check only — NOT the path source.

    repo_root handler arg: the git common dir supplied by the engine
    (_OP_KEY_SCOPE = "common_dir" — B4 settlement; `handoff.match_candidates`
    precedent).  All paths (state/handoffs/, archive/handoffs/, the session-id
    sentinel, git-provenance) derive from it via main_worktree_root.

    Wire output:
      exit_code      int   0=classified, 1=setup error or CC-7 unclassifiable state
      disposition    str|None   "open" | "claimed" | "continued" | "closed"
                                (locked DR-084 vocabulary — never "abandoned",
                                never "consumed"); None on exit_code:1
      chain_terminal bool  the fence's chain-terminal (True) vs single-session
                           (False) axis
      evidence       dict  {session_id, session_id_source, detector,
                            consumed_handoff, deployment_state, closed_reason,
                            warnings, notes}
      error          str   present only on exit_code:1

    Idempotency (DEC-7): inherent — no writes; both subprocesses are read-only
    git queries.  Identical inputs over unchanged disk classify identically
    (CC-4 double-invocation test asserts this).
    """
    if repo_root is None:
        return _error_result("repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root

    # D3: optional repo_root consistency check (check only, never the path source).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return _error_result(mismatch)

    param_sid = params.get("session_id")
    if param_sid is not None and not isinstance(param_sid, str):
        return _error_result(
            f"session_id must be a string when provided, got {type(param_sid).__name__}"
        )

    try:
        return await asyncio.to_thread(
            _classify_sync, common_dir, param_sid, dict(os.environ)
        )
    except RuntimeError as exc:
        return _error_result(str(exc))
    except subprocess.TimeoutExpired as exc:
        return _error_result(f"git subprocess timed out: {exc}")
    except OSError as exc:
        return _error_result(f"classification I/O failed: {exc}")
