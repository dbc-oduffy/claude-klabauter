"""
coordinator_core.session.liveness — the coordinator session hub's LIVENESS
module.

Port of: liveness.sh (DoE 6aa77d4b, 2026-07-21).

This is a PURE IN-PROCESS LIBRARY, not an IPC op — it self-registers nothing
and touches none of the shared op-registry files. It provides the two-layer
session-liveness model and the claim-layer liveness/identity predicates that
the whole claim + reaper + enumeration stack routes through.

THREE SOURCES IN PRECEDENCE ORDER (Review: staff-eng, C2 — the registry
branch below sits in FRONT of the two-layer model, not inside it):
    Source 0 — the harness's own session registry
      (``coordinator_core.session.harness_registry``, ``<claude-config>/
      sessions/<pid>.json``): consulted first, via the single
      ``harness_registry.lookup(sid)`` helper shared verbatim by
      ``session_live`` and ``live_session_verdicts`` (AC5) — a difference
      between the two on which scan/view they see is a defect, not a
      deliberate divergence. It is an UNDOCUMENTED HARNESS INTERNAL: preferred
      as a source, never depended on, unversioned, no stability guarantee.
      Its ABSENCE IS NOT EVIDENCE OF DEATH — a miss is the fallback trigger to
      Layer 1 and nothing else. A hit still routes through the SAME tolerant
      birth-instant compare as Layer 1, at that layer's existing single
      ``core.stable_pid_alive`` seam — this module never gains a second call
      site for that comparison.
    Layer 1 — PPID-authoritative process-aliveness (when ``stable_pid`` is
      present in meta.json): ``session_live`` delegates to
      ``core.stable_pid_alive`` on the ``stable_pid`` + ``stable_pid_lstart``
      / ``stable_pid_start_epoch`` fields. Process alive + birth-instant match
      -> LIVE (authoritative; recency NOT consulted). Process gone or lstart
      mismatch (PID recycled) -> DEAD within seconds of process exit. A raise
      from ``core.stable_pid_alive`` itself (e.g. ``MissingPsutilError``) is
      caught here and fails OPEN (True), matching ``live_session_verdicts``'
      own Layer-1 arm exactly (Review: staff-eng-review B) — never propagate,
      never fall through to Layer 2 on the exception, never DEAD.
    Layer 2 — recency fallback (when ``stable_pid`` is absent/empty): falls
      through to ``is_session_live`` — ``elapsed_sec < 30 min`` -> live. The
      unchanged path for non-harness runs, legacy meta.json without
      ``stable_pid``, and Guard-1 comm-miss.

RAW-PID-LIVENESS floor (load-bearing invariant, docs/wiki/coordinator-tripwires.md):
    Do NOT call ``ps -p`` / ``kill -0`` / ``psutil.pid_exists`` on the meta
    ``pid`` field for a LIVENESS decision — that field is a dead
    per-hook-subshell ``$$``, not the long-lived session process. Process-
    identity liveness goes ONLY through ``core.stable_pid_alive`` (keyed on the
    separate ``stable_pid`` field + stored lstart/epoch). The bash is forbidden
    from gating on ``pid`` and so is this port. ``core.pid_alive`` on ``pid``
    survives ONLY on the legacy pid-only claim-dir fallback (structurally
    "always dead in-harness"), never for a session-liveness verdict. The same
    floor applies to the registry record introduced above: its ``pid`` is
    admissible ONLY paired with ``start_epoch`` through
    ``core.stable_pid_alive`` — a bare ``pid_exists``/``kill -0`` on it is the
    same prohibited shape as on the meta ``pid`` field, not an exception to
    this floor (Review: staff-eng, Finding 9).

Single-liveness-key invariant (D5, pcore-03): ``core.stable_pid_alive`` is
called from exactly ONE place here — ``session_live`` — never from the claim
takeover / reap / sweep paths, which all route back through ``session_live``
so they provably agree on what "stale" means. No PID fields are duplicated
into claim dirs or any new structure.

``live_session_verdicts`` (Review: staff-eng second pass, Finding 7) is THE
ONE shared per-id ``id -> (live, basis, age_sec)`` seam consumed both by
``live_session_ids`` (below, a thin derived wrapper) and by
``pickup_assemble.holder_evidence._liveness_basis`` — closing the fourth
independent liveness derivation that used to call ``core.stable_pid_alive``
directly from outside this module. It duplicates ``session_live``'s Layer-1
process check inline (rather than delegating) SOLELY so it can also report
which basis produced the verdict; the boolean outcome is unchanged.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4a-g1
Also: docs/plans/2026-06-27-liveness-first-claim-staleness.md (the two-layer model)
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § liveness.py

Negative-spec:
    - Do NOT memoize the live-set inside ``live_session_ids`` — a cached
      live-set reopens the wrong-attribution race (AC8 rejected caching HERE;
      the 2.0s TTL cache lives one layer up in the old bridge, not in this
      function).
    - Do NOT reintroduce a ``pid_alive`` gate on the ``pid`` field anywhere in
      the enumeration/liveness path — it silently zeroes the live set
      in-harness (the 2026-06-23 regression).
    - Do NOT clamp negative elapsed in ``live_session_ids`` (the non-stable
      branch) — bash routes it through ``_cs_is_session_live`` UNCLAMPED, whose
      ``^[0-9]+$`` guard rejects a negative string -> not-live. This DIFFERS
      from ``session_live``'s Layer-2 arm and ``active_sessions``, which DO
      clamp. Honest accounting (Review: staff-eng-review D, 2026-08-10): the
      bash originals this ported are gone, so "faithful to the bash
      originals" is no longer a live rationale — this arm fails DEAD under
      backward clock skew, contrary to this module's own fail-open-never-
      fail-dead bias elsewhere. The behaviour is preserved anyway because
      changing it changes ``live_session_ids``, which feeds
      ``compute_scope`` and ``_rm_peer_claim_of`` — a separate blast radius
      that must not ride along with this pass. Tracked as its own
      debt-backlog entry pending a dedicated assessment of
      ``live_session_ids``' hot consumers.
    - Do NOT let a meta-less/unparseable-meta session dir read confirmed-DEAD
      by defaulting its recency to epoch-0 — that let a peer wrongfully take
      over a session that was merely mid-write (DoE 642195ba, follow-up
      88929bea; ``_dir_recency_fallback_epoch``, ``session_live``'s Layer 2).
      This is a recency-SOURCE substitution, not a threshold change — a
      genuinely stale meta-less dir must still read DEAD.
    - Do NOT enumerate ``live_session_ids`` via a ``*/meta.json`` glob — a
      session dir that exists but has not yet flushed its meta.json is then
      never VISITED (not merely misclassified), so a live-but-mid-write
      session is silently absent from the live set (2026-07-21, second
      instance of the bug class above; see the production incident recorded
      in ``state/improvement-queue/2026-07-17-reaper-reclaims-live-executing-
      sessions-d48b81b41d52.yaml``). Enumerate every subdirectory and exclude
      known non-session children via ``_NON_SESSION_DIR_NAMES`` instead.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import FrozenSet, Optional

from coordinator_core.session import core
from coordinator_core.session import harness_registry

logger = logging.getLogger(__name__)

#: Rollback lever for Layer 1 (docs/reference/layer1-liveness-activation.md):
#: when set to a truthy value, ``session_live`` skips the Layer 1
#: PPID-authoritative check entirely and falls straight through to Layer 2,
#: the same recency-window path every session took before C3. C3
#: (``73b21f35b``) has landed -- Layer 1 now gates a LIVE code path, and
#: sessions on this box already carry a non-empty ``stable_pid``
#: (docs/reference/layer1-liveness-activation.md § 4). Comment authored
#: pre-C3, when the harness-process name check rejected every session and
#: reading this lever changed nothing; that history is preserved for
#: context only -- do not read it as describing current behavior. Truthy
#: values: "1", "true", "yes" (case-insensitive); anything else (including
#: unset) is falsy.
_LAYER1_DISABLE_ENV = "COORDINATOR_SESSION_LAYER1_DISABLE"
_LAYER1_DISABLE_TRUTHY = frozenset({"1", "true", "yes"})

#: Review: staff-eng F5 -- ``session_live``'s Source-0 registry consult
#: (below) now runs on EVERY missing-sdir sid, including inside the reaper's
#: and memo-sweep's per-claim-dir loops (``harness_registry``'s own module
#: docstring: "hundreds of parses per invocation on a loaded box"). Rather
#: than re-scan ``harness_registry.snapshot()`` per call, this memoizes ONE
#: snapshot -- but on a 2 s TTL, not for the lifetime of the process (C8,
#: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md),
#: mirroring the 2 s TTL shape ``resolve_live_session_ids``/the old bridge
#: layer used before the native port. Under spawn-per-call this TTL is inert
#: -- a process lives milliseconds, so it never outlives 2 s and behaves
#: identically to the old unconditional-pin. Under a WARM engine a
#: process-lifetime pin would mean a session that registers into the
#: harness registry AFTER the first snapshot is invisible for the rest of
#: the server's life (the characterization test this fix flips,
#: ``coordinator_core/warm/tests/test_process_global_characterization.py``
#: Site 4). The registry read here is already advisory (confirmed via
#: ``core.stable_pid_alive`` before being trusted, so a stale snapshot
#: cannot manufacture a false LIVE any more than a fresh one could -- both
#: still require the recorded pid+start_epoch to pass the birth-instant
#: compare); a 2 s TTL bounds that staleness rather than eliminating the
#: cache. `None` means "not yet fetched, or the last fetch has expired"; a
#: fetch that raises or finds an empty registry caches `{}` (with a fresh
#: timestamp), not `None`, so a miss is never re-fetched every call within
#: the TTL window.
_registry_snapshot_cache: Optional[dict] = None

#: Wall-clock (``time.monotonic()``) stamp of the last successful
#: ``_registry_snapshot_cache`` fetch, or `None` before the first fetch.
#: Paired with ``_registry_snapshot_cache`` above -- read together, written
#: together, reset together (see the test-suite reset note below).
_registry_snapshot_cache_at: Optional[float] = None

#: TTL in seconds for ``_registry_snapshot_cache`` -- mirrors the 2 s TTL
#: shape ``resolve_live_session_ids``' own caching layer used historically
#: (see docstring above).
_REGISTRY_SNAPSHOT_TTL_SEC = 2.0


def _cached_registry_lookup(sid: str):
    """``harness_registry.lookup(sid)`` via a 2 s-TTL-memoized ``snapshot()``
    (see cache docstring above) -- ``session_live`` is the ONLY caller; other
    module call sites (``_verdict_for_sdir``'s batch scan, ``session_verdict``'s
    per-sid path) are unrelated to F5's hot loops and are left untouched.

    Cross-test-file staleness window (Review: coordinator:code-reviewer P2):
    this cache lives at most ``_REGISTRY_SNAPSHOT_TTL_SEC`` seconds, and only
    ``coordinator_core/session/tests/test_liveness.py`` resets it early (autouse
    fixture). ``coordinator_core/pickup_assemble/tests/test_brief_claim_lease.py``
    and ``test_pickup_claim_stage_stamp_evidence.py`` also exercise
    ``session_live``/``claim_holder_live`` and now carry their own matching
    autouse reset for the same reason -- a snapshot cached by one test (in
    any of these files, sharing a pytest worker process) would otherwise
    silently outlive a later test's ``monkeypatch.setattr(harness_registry,
    "registry_dir", ...)`` for up to the TTL. Still production-safe
    (spawn-per-call or warm-with-TTL); this is a test-suite-only staleness
    window."""
    global _registry_snapshot_cache, _registry_snapshot_cache_at
    now = time.monotonic()
    expired = (
        _registry_snapshot_cache_at is None
        or (now - _registry_snapshot_cache_at) >= _REGISTRY_SNAPSHOT_TTL_SEC
    )
    if _registry_snapshot_cache is None or expired:
        try:
            _registry_snapshot_cache = harness_registry.snapshot()
        # snapshot()'s own contract never raises (it catches internally and
        # returns {} on any failure); this guard is retained purely as a
        # belt-and-braces backstop against that contract changing underfoot,
        # not because snapshot() is currently believed to raise.
        except Exception:
            _registry_snapshot_cache = {}
        _registry_snapshot_cache_at = now
    return _registry_snapshot_cache.get(sid)


#: Process-local counter for the Layer 1 fail-open ("unknown" basis) arm --
#: see ``docs/reference/layer1-liveness-activation.md`` § Observability. Never
#: persisted, never influences a verdict; a later reader can read it directly
#: off this module (``coordinator_core.session.liveness.layer1_unknown_count``)
#: for a process-lifetime tally, e.g. from a health-check op or test.
layer1_unknown_count = 0

#: Process-local counter for the SIBLING fail-open arm -- ``_verdict_for_sdir``'s
#: own ``"unknown"``-basis except-arm (C4a-2, closing the gap C4a's § Scope
#: note left open; see ``docs/reference/layer1-liveness-activation.md``
#: § Observability). Deliberately a SEPARATE counter from
#: ``layer1_unknown_count`` rather than sharing it: this arm feeds a distinct
#: consumer surface (``live_session_verdicts``/``session_verdict``, which
#: render into pickup briefs) from ``session_live``'s arm (the claim layer),
#: so a reader who sees one counter moving and not the other can tell WHICH
#: path degraded -- a shared counter would collapse that distinction into a
#: single fleet-health number that cannot answer "is it the claim layer or
#: the verdict/pickup surface that's seeing psutil pressure".
verdict_layer1_unknown_count = 0


def _layer1_disabled() -> bool:
    """Read the Layer 1 rollback lever (see ``_LAYER1_DISABLE_ENV`` above).

    A fresh ``os.environ.get`` read per call, matching every other env-toggle
    seam in this package (e.g. ``shape._CS_SHAPE_LOCK_STALE_SEC``) -- no
    caching, so a fleet-wide flip via env is visible to the next call, not
    just the next process.
    """
    return os.environ.get(_LAYER1_DISABLE_ENV, "").strip().lower() in _LAYER1_DISABLE_TRUTHY

#: Thirty minutes in seconds — the recency liveness boundary (Layer 2).
_THIRTY_MIN = 30 * 60

#: Port of the bash ``[[ "$elapsed_sec" =~ ^[0-9]+$ ]]`` guard — matches a
#: non-negative decimal integer string ONLY. A negative or non-numeric value
#: fails the match and is treated as not-live, exactly as the bash regex does.
_DIGITS_RE = re.compile(r"^[0-9]+$")

#: Reserved top-level children of the sessions dir that are NOT session
#: directories -- ``live_session_ids`` enumerates ALL subdirectories (see
#: that function's docstring for why a meta.json-glob silently drops a
#: mid-write session), so these must be excluded explicitly rather than
#: falling out for free the way a ``*/meta.json`` glob excluded them.
#: Session ids are caller-supplied strings (env-var derived; NOT guaranteed
#: to be UUID-shaped -- test overrides legitimately use non-UUID ids), so
#: this is a denylist of KNOWN infra dirs, not a positive shape check:
#:   - ``.archive`` / ``.agents`` -- pre-existing exclusions (archived
#:     sessions / findings-agent self-persist dirs).
#:   - ``handoff-claims`` / ``memo-claims`` / ``plan-claims`` -- the
#:     claim-class lock dirs (mirrors
#:     ``coordinator_core.ops.fleet._common._CLAIM_SUBDIRS``, duplicated as
#:     a literal here rather than imported -- session/ is a lower layer than
#:     ops/fleet/ and must not import upward).
#:   - ``agent-sessions-locks`` -- legacy advisory-lock bookkeeping dir used
#:     by the retired ``append-plan-session.sh`` (see
#:     docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md).
#:   - ``logs`` -- the shared audit-log dir (e.g. ``agent-audit.jsonl``).
#:   - ``no-session`` -- the literal sentinel id substituted for an unset
#:     ``CLAUDE_SESSION_ID`` env var (see ``bash_guards/dispatch_checks.py``
#:     and ``write_guards/block_subagent_plan_body_write.py``); it is a
#:     fallback bucket, never a real session.
#:   - ``decisions`` -- pickup/judgment decision objects written by
#:     ``ops/pickup_assemble`` alongside the session registry (2026-08-08
#:     phantom-peer defect: this dir is actively mtime-touched, so an
#:     unfiltered walk read it as a Layer-2-recent LIVE "session").
#:   - ``reconcile-history`` -- reconciliation audit trail, same sibling
#:     infra class as ``decisions``.
#: Found on this repo's real on-disk corpus 2026-07-21 while diagnosing the
#: meta.json-glob invisibility defect this constant exists to close; extended
#: 2026-08-08 for ``decisions``/``reconcile-history`` (same corpus, same
#: enumerate-then-exclude shape -- see TestLiveSessionIdsCorpus's
#: real-registry walk in test_liveness.py, which turns a future hole here
#: into a red test rather than another silent phantom-peer).
_NON_SESSION_DIR_NAMES = frozenset(
    {
        ".archive",
        ".agents",
        "handoff-claims",
        "memo-claims",
        "plan-claims",
        "agent-sessions-locks",
        "logs",
        "no-session",
        "decisions",
        "reconcile-history",
    }
)

#: Allowlist of characters permitted in a session id passed to
#: ``session_verdict`` (Review: coordinator:code-reviewer, colon/drive-letter
#: gap). A prior blocklist rejecting `/`, `\`, `..`, NUL did not reject a
#: bare drive-letter/colon component (e.g. ``"C:evil"``); on Windows,
#: ``ntpath.join(base, "C:evil")`` DISCARDS ``base`` entirely, a full
#: containment escape out of the sessions corpus. Restricting to
#: ``[A-Za-z0-9_-]`` -- UUID-shaped tokens and hyphen/underscore test-fixture
#: slugs like ``"test-session-abc123"`` -- closes colon, reserved device
#: names, trailing dot/space, path separators, ``..``, and NUL by
#: construction, and stays compatible with every sid already live on disk.
_SID_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


# ---------------------------------------------------------------------------
# Layer 2 — pure recency gate
# ---------------------------------------------------------------------------


def is_session_live(pid="", elapsed_sec=0) -> bool:
    """Port of ``_cs_is_session_live <pid> <elapsed_sec>``.

    Pure Layer-2 recency gate: live iff ``elapsed_sec < 30*60``. The signature
    ``(pid, elapsed_sec)`` is UNCHANGED from bash for stability, but the
    ``pid`` argument is DIAGNOSTIC-ONLY — it is DELIBERATELY NOT part of the
    liveness decision (a ``kill -0`` on the meta ``pid`` reports EVERY session
    dead in-harness; see the module docstring's RAW-PID-LIVENESS floor).

    ``elapsed_sec`` is validated against ``^[0-9]+$`` before the comparison,
    mirroring the bash guard: a non-numeric value (or a negative one, whose
    string form carries a ``-``) fails the match and returns False (not-live),
    NOT a crash. This is the exact edge that keeps ``live_session_ids`` from
    ever treating a clock-skew-negative elapsed as live.
    """
    if not _DIGITS_RE.match(str(elapsed_sec)):
        return False
    return int(elapsed_sec) < _THIRTY_MIN


def _dir_recency_fallback_epoch(sdir: str) -> int:
    """Meta-less/unparseable recency fallback for ``session_live``'s Layer 2
    (DoE 642195ba, follow-up 88929bea) -- see the call site's comment for the
    wrongful-takeover rationale this closes.

    Returns the newest mtime among ``sdir``'s top-level REGULAR files (reusing
    the already-ported ``core.mtime_epoch``, never a new stat helper), or
    ``sdir``'s own directory mtime if it contains no regular files at all
    (e.g. a session dir created but not yet populated). Returns 0 only if
    even the directory stat fails (TOCTOU: ``sdir`` removed between the
    caller's ``is_dir()`` check and this call) -- callers already treat 0 as
    "no recency signal" via the existing ``iso_to_epoch`` contract.
    """
    newest = 0
    try:
        with os.scandir(sdir) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False):
                    candidate = core.mtime_epoch(entry.path)
                    if candidate > newest:
                        newest = candidate
    except OSError:
        newest = 0
    if newest:
        return newest
    try:
        return int(Path(sdir).stat().st_mtime)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# THE shared claim-layer liveness key — O(1) single-session
# ---------------------------------------------------------------------------


def session_live(sid: str, cwd: Optional[str] = None) -> bool:
    """Port of ``_cs_session_live <session_id>``.

    True iff ``sid`` is a LIVE session. THE shared key for the claim layer —
    claim takeover, release holder-check, the reaper, and the enumeration
    functions all call through here so they provably agree on what "stale"
    means. O(1): reads that one session's meta.json directly (never scans every
    dir — that is ``live_session_ids``' job).

    Source 0 + two-layer decision: Source 0 (the harness's own session
    registry, ``harness_registry.lookup(sid)``) is consulted FIRST, ahead of
    both layers below. A registry hit is a CANDIDATE, not proof of
    reachability -- the harness itself socket-probes it and lazily unlinks
    unreachable records -- so a hit is confirmed here via the same
    ``core.stable_pid_alive`` seam Layer 1 uses before it is trusted; only a
    confirmed hit returns True early. A miss, or a hit that fails
    confirmation, falls through unchanged to Layer 1:
      Layer 1 (PPID-authoritative): if ``stable_pid`` is non-empty in
        meta.json AND EITHER ``stable_pid_lstart`` OR
        ``stable_pid_start_epoch`` is present (either is a sufficient
        birth-instant witness for ``core.stable_pid_alive``), delegate to
        ``core.stable_pid_alive`` and RETURN its verdict — recency is NOT
        consulted. POSIX ``init()`` stopped writing ``stable_pid_lstart``
        2026-07-27 (dca0e3e80) but still writes ``stable_pid_start_epoch``.
        ``stable_pid`` present but BOTH witnesses ABSENT (partial write /
        TOCTOU between the meta writes) is NOT treated as dead — fall
        through to Layer 2 to preserve the safety net (A-F1).
      Layer 2 (recency fallback): read ``pid`` + ``last_activity``, convert via
        ``core.iso_to_epoch``, ``elapsed = now - last`` clamped ``>= 0``,
        then ``is_session_live(pid, elapsed)``. When ``last_activity`` is
        EMPTY (no meta.json, unparseable meta.json, or the field itself is
        missing/null -- ``read_meta_field`` returns "" on all three),
        ``last_epoch`` is substituted via ``_dir_recency_fallback_epoch``
        (DoE 642195ba / 88929bea) rather than defaulting to epoch-0 -- see
        that helper's docstring. A ``last_activity`` value that IS present
        but fails ISO parsing (e.g. corrupt-but-non-empty) is NOT covered by
        this fallback and still reads DEAD, unchanged from before.

    Empty/unknown sid or missing session dir -> not live. A meta-less or
    unparseable-meta session dir now falls back to on-disk mtime recency
    (see above) rather than reading instantly-dead. Negative elapsed is
    clamped to 0 (unlike the non-stable branch of ``live_session_ids`` — see
    module negative-spec).

    ``core.stable_pid_alive`` is called ONLY here (single-liveness-key
    invariant) — never from the claim takeover / reap / sweep paths.
    """
    if not sid:
        return False

    # Source 0: harness session registry (preferred, never depended on) --
    # consulted BEFORE the local sdir existence check below (2026-08-14 fix,
    # cross-repo/inbox/2026-08-11-example-market-data-repo-em-reclaim-labels-a-
    # live-session-dead-without-checking.md). Source 0 is keyed on sid alone
    # and is REPO-INDEPENDENT (the harness registry record carries its own
    # pid/start_epoch, not a path under `cwd`'s `.git`), so it must not be
    # gated behind "this repo happens to have a session dir for sid" --
    # doing so silently starved it of the one case it exists to catch: a
    # session whose OWN coordinator session dir is not visible under THIS
    # cwd/repo (a foreign-cwd holder, or a dir a caller resolves via a
    # different `cwd` than the one the holder wrote it under) read
    # instantly DEAD here without Source 0 ever being asked, even though
    # `session_verdict`'s own "no-verdict arm" (C1, docs/plans/2026-08-13-
    # liveness-stops-conflating-dead-with-elsewhere.md) already established
    # that exact case is not evidence of death and added a matching
    # registry consult THERE -- this was the one seam of the two that
    # never got the same fix, so `session_live` (and therefore
    # `claim_holder_live`, which calls only this function -- reaper,
    # memo sweep, and claim takeover all route through it, D5) kept the
    # gap `session_verdict` had already closed for its own callers.
    #
    # BEHAVIOUR CHANGE for every caller of session_live/claim_holder_live
    # (claim takeover, the reaper, the memo sweep -- claim_holder_live's own
    # docstring): a claim/session whose sid has NO locally-visible session
    # dir under `cwd`'s repo, but a confirmed (stable_pid_alive-verified)
    # harness-registry record, now reads LIVE instead of DEAD. This is a
    # widening of "live", not a narrowing -- it can only turn a false-DEAD
    # into a true-LIVE (a stale/orphaned registry record still requires a
    # LIVE process at the recorded pid+start_epoch to pass
    # `core.stable_pid_alive`'s birth-instant compare, so a genuinely dead
    # process is unaffected), matching this module's existing fail-open
    # bias rather than introducing a new one.
    #
    # Reaping consequence (Review: staff-eng, undocumented blast radius --
    # the widening above names WHAT changed but not this downstream effect):
    # an `apply`-stage claim held by a foreign-cwd sid with a genuinely live
    # process is now UNREAPABLE by `reap_stale_claims`/the memo sweep for
    # that process's entire lifetime -- apply-stage claims carry no lease
    # (unlike `brief`-stage, which `claim_artifact`'s
    # `claim_holder_live(...) and not lease_expired` still bounds), and the
    # reaper has none either, so nothing ever ages it out while the holder
    # stays alive. Pre-fix, such a claim was reaped within a scan (the
    # local-sdir-missing case read DEAD unconditionally). This is the
    # correct trade -- a live process's claim should not be stolen out from
    # under it -- but it is a real change to reaping behaviour, not just to
    # narration honesty, and is worth knowing before debugging a claim dir
    # that "should have" been reaped and wasn't.
    try:
        record = _cached_registry_lookup(sid)
    except Exception:
        record = None
    if record is not None:
        # Review: fail-open parity with live_session_verdicts' registry arm —
        # a raise from the compare (e.g. MissingPsutilError) must fall
        # through to Layer 1/2 unchanged, never propagate out of
        # session_live and never itself mean DEAD.
        try:
            if core.stable_pid_alive(str(record.pid), "", str(int(record.start_epoch))):
                return True
        except Exception:
            pass

    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return False
    if not Path(sdir).is_dir():
        return False

    # Layer 1: PPID-authoritative process check (when stable_pid captured at init).
    # Rollback lever (docs/reference/layer1-liveness-activation.md): when set,
    # skip Layer 1 entirely and fall through to Layer 2. C3 (73b21f35b) has
    # landed, so this now gates a live code path -- Layer 1 actually engages
    # for sessions carrying a non-empty stable_pid (was inert pre-C3, when
    # stable_pid was empty fleet-wide).
    stable_pid = "" if _layer1_disabled() else core.read_meta_field(sdir, "stable_pid")
    if stable_pid:
        stable_pid_lstart = core.read_meta_field(sdir, "stable_pid_lstart")
        stable_pid_start_epoch = core.read_meta_field(sdir, "stable_pid_start_epoch")
        # Birth-instant witness absent != process dead — fall through to
        # Layer 2 (A-F1) only when BOTH lstart and start_epoch are missing.
        # POSIX init() stopped writing stable_pid_lstart 2026-07-27
        # (dca0e3e80) but still writes stable_pid_start_epoch, which
        # core.stable_pid_alive already accepts as a sufficient witness.
        if stable_pid_lstart or stable_pid_start_epoch:
            # Review: staff-eng-review B — a raise here (e.g. MissingPsutilError)
            # must fail OPEN (True), matching live_session_verdicts' own
            # Layer-1 arm exactly ((True, "unknown", None)). Do NOT fall
            # through to Layer 2 on the exception — that would create a new
            # undocumented divergence while fixing one, and
            # MissingPsutilError's own docstring rules out any path where a
            # psutil failure reads DEAD.
            try:
                return core.stable_pid_alive(
                    stable_pid, stable_pid_lstart, stable_pid_start_epoch
                )
            except Exception as exc:
                # Fail open, but never silently (docs/reference/
                # layer1-liveness-activation.md § Observability): a
                # fleet-wide psutil failure must be DETECTABLE, not silently
                # read as universal liveness. Process-local counter + a single
                # debug log line -- no file write, no lock, no verdict
                # change. Never raises; never alters the returned verdict.
                # Non-atomic read-modify-write; safe under the current
                # spawn-per-call, single-threaded-per-process model. A future
                # caller adding a thread pool or async loop around this
                # function would need to make this atomic to avoid
                # under-counting.
                global layer1_unknown_count
                layer1_unknown_count += 1
                try:
                    logger.debug(
                        "coordinator_core.session.liveness: Layer 1 fail-open "
                        "(unknown basis) for sid=%s: %s (count=%d)",
                        sid,
                        exc,
                        layer1_unknown_count,
                    )
                except Exception:
                    pass
                return True
        # Neither witness present — fall through to Layer 2.

    # Layer 2: recency fallback (stable_pid absent, legacy meta, or Guard-1 miss).
    pid = core.read_meta_field(sdir, "pid")
    last_iso = core.read_meta_field(sdir, "last_activity")
    last_epoch = core.iso_to_epoch(last_iso)
    if not last_iso:
        # Wrongful-takeover fallback (DoE 642195ba, follow-up 88929bea): a
        # session dir with NO meta.json, an unparseable meta.json, or a
        # meta.json missing ``last_activity`` makes ``read_meta_field``
        # return "" -> ``iso_to_epoch("")`` returns 0 -> elapsed would be
        # ~epoch-now (~1.7e9s), reading confirmed-DEAD for a session that is
        # merely mid-write (meta.json not yet flushed) and letting a peer
        # wrongfully take it over. Substitute the newest mtime among the
        # session dir's top-level REGULAR files as the recency source; if
        # the dir has no regular files yet, fall back to the dir's own
        # mtime. This swaps the recency SOURCE only -- the 30-min liveness
        # THRESHOLD below is unchanged, so a genuinely stale meta-less dir
        # still reads DEAD and stays reapable.
        last_epoch = _dir_recency_fallback_epoch(sdir)
    now_epoch = core.now_epoch()
    elapsed = now_epoch - last_epoch
    if elapsed < 0:
        elapsed = 0
    return is_session_live(pid, elapsed)


# ---------------------------------------------------------------------------
# Claim-layer liveness + identity predicates
# ---------------------------------------------------------------------------


def claim_holder_live(cdir: str, cwd: Optional[str] = None) -> bool:
    """Port of ``_cs_claim_holder_live <claim_dir>``.

    True iff the session HOLDING ``cdir`` is live — THE single liveness
    decision for the claim layer's stale/takeable/reapable question, shared by
    claim takeover, the reaper, and the memo sweep so all provably agree.

    Canonical key: session_id-bearing claim dirs cross-reference the held
    ``session_id`` against the registry via ``session_live``. LEGACY fallback:
    a pid-only claim dir (pre-upgrade, no ``session_id`` file) uses
    ``core.pid_alive`` on the ephemeral ``pid`` field for that dir only —
    structurally "always dead in-harness", self-heals to ``session_id`` on
    first takeover.

    Defensive TOCTOU: the ``session_id`` (or ``pid``) file can be ``rm``'d
    between the ``is_file()`` test and the read (a concurrent takeover /
    reaper) — treat a read failure as ``""`` -> ``session_live("")`` returns
    not-live (safe), mirroring the bash ``|| echo ""``.

    ``cdir`` is a REQUIRED argument — an empty/missing value raises ValueError
    (mirrors the bash ``${1:?claim_dir required}``).
    """
    if not cdir:
        raise ValueError("claim_dir required")
    p = Path(cdir)
    if (p / "session_id").is_file():
        try:
            sid = (p / "session_id").read_text(encoding="utf-8").strip()
        except OSError:
            sid = ""
        return session_live(sid, cwd)
    try:
        pid = (p / "pid").read_text(encoding="utf-8").strip()
    except OSError:
        pid = ""
    return core.pid_alive(pid)


def claim_held_by_me(
    claim_dir: str, my_sid: str = "", cwd: Optional[str] = None
) -> bool:
    """Port of ``_cs_claim_held_by_me <claim_dir> [my_sid]``.

    IDENTITY predicate (distinct from ``claim_holder_live``'s LIVENESS
    predicate) — the holder-check for claim release. True iff THIS session is
    the recorded holder of ``claim_dir``.

    session_id-bearing claim dir: recorded ``session_id`` equals ``my`` AND
    ``my`` is non-empty. Legacy pid-only dir: recorded ``pid`` equals
    ``os.getpid()`` (the ``$$`` compare) — a PERMANENT no-op in-harness (every
    call has a fresh ``$$``), preserved as-is so pid-only dirs self-heal to
    ``session_id`` on first takeover.

    ``my_sid`` (optional): the caller's PRE-RESOLVED session id. Pass it so a
    two-call TOCTOU release sequence keys both reads off ONE identity — the
    second read then varies only on the claim-dir CONTENT (the actual race),
    never on a re-resolution of my own id. If omitted, resolves via
    ``core.resolve_session_id``. Re-reads the dir on each call, so calling it
    twice IS the release TOCTOU re-read.

    ``claim_dir`` is REQUIRED — empty/missing raises ValueError (mirrors the
    bash ``${1:?claim_dir required}``).
    """
    if not claim_dir:
        raise ValueError("claim_dir required")
    my = my_sid or ""
    if not my:
        my = core.resolve_session_id(cwd)
    p = Path(claim_dir)
    if (p / "session_id").is_file():
        try:
            recorded = (p / "session_id").read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        return bool(my) and recorded == my
    try:
        recorded_pid = (p / "pid").read_text(encoding="utf-8").strip()
    except OSError:
        recorded_pid = ""
    return recorded_pid == str(os.getpid())


# ---------------------------------------------------------------------------
# Enumeration — live set + human-readable listing
# ---------------------------------------------------------------------------


#: Basis vocabulary shared by ``live_session_verdicts`` and (via that seam)
#: ``pickup_assemble.holder_evidence._liveness_basis``:
#:   "harness-registry"      — Source 0, the harness's own session registry
#:                              (``harness_registry.lookup``/``snapshot``),
#:                              matched via the same tolerant birth-instant
#:                              compare as "stable-pid". ``age_sec`` is always
#:                              ``None`` here — evidence is not meaningful on
#:                              a process-identity verdict and must never
#:                              influence ``live`` (AC7). This is STRONGER
#:                              evidence than "stable-pid" (harness-written
#:                              process identity vs. our own derived stamp),
#:                              not weaker — a consumer must not render it in
#:                              a hedged/unknown arm.
#:   "stable-pid"            — Layer 1 (PPID-authoritative) was consulted.
#:   "recency-window"        — Layer 2 recency fallback, ``last_activity``
#:                              present and parseable.
#:   "recency-window-mtime"  — Layer 2 recency fallback, but ``last_activity``
#:                              was absent/unparseable so the recency SOURCE
#:                              was substituted via ``_dir_recency_fallback_epoch``
#:                              (the meta-less/mid-write case).
#:   "unknown"               — ``stable_pid``/``stable_pid_lstart`` were
#:                              present but the underlying process check
#:                              itself raised; never launder this into a
#:                              stronger claim than can be supported. Fails
#:                              OPEN (``live=True``) rather than toward a
#:                              wrongful takeover, matching this module's
#:                              established ambiguous-failure bias (see
#:                              ``core.stable_pid_alive``'s own
#:                              ``_WinLivenessAmbiguous`` handling).
#:   "harness-registry-elsewhere" — ``session_verdict``-ONLY (C1, docs/plans/
#:                              2026-08-13-liveness-stops-conflating-dead-
#:                              with-elsewhere.md); never produced by
#:                              ``live_session_verdicts``'/``_verdict_for_sdir``'s
#:                              whole-corpus scan, which only ever visits
#:                              THIS repo's own session dirs. Means: no
#:                              session dir for this sid exists in THIS
#:                              repo, but a confirmed harness-registry
#:                              record for it does. This is inferred from
#:                              dir-absence alone (Review: staff-eng-review
#:                              Finding 6), NOT from comparing the peer's
#:                              cwd to this repo's -- a session whose dir was
#:                              reaped, deleted, or not yet created HERE
#:                              while live HERE takes this same branch, so
#:                              "the session is live, working in ANOTHER
#:                              repo" is the common case, not a proven one.
#:                              The THIRD tuple slot carries that peer's
#:                              ``cwd`` (``str`` or ``None``) on this basis
#:                              ONLY, not an ``age_sec`` int -- see
#:                              ``session_verdict``'s own docstring for the
#:                              full derivation and why this is worded
#:                              "elsewhere", never "confirmed reachable".


#: Review: staff-eng-review Finding 4 -- named aliases for the punned third
#: tuple slot. Both collapse to ``tuple[bool, str, int | str | None]`` under
#: any type checker (a plain ``tuple[bool, str, Optional[int]] |
#: tuple[bool, str, Optional[str]]`` union provides zero static protection
#: against the arithmetic-on-a-cwd-string hazard), so this buys nothing
#: mechanically -- it exists purely to give the pun a greppable name so a
#: reader/reviewer/grep can find every producer and consumer of each shape.
#: ``SessionVerdict`` is every ordinary basis (age_sec int or None in slot
#: 3); ``ElsewhereVerdict`` is the ONE "harness-registry-elsewhere" basis
#: (peer cwd str or None in slot 3, never an elapsed-seconds value).
SessionVerdict = tuple[bool, str, Optional[int]]
ElsewhereVerdict = tuple[bool, str, Optional[str]]


def _verdict_for_sdir(
    sid: str,
    sdir: str,
    record: Optional[harness_registry.RegistryRecord],
    now_epoch: int,
) -> "SessionVerdict":
    """One id's verdict, factored out of ``live_session_verdicts``' loop body
    (Review: staff-eng-review C) so ``session_verdict`` below can compute the
    SAME per-id verdict without an O(n) whole-corpus scan. ``record`` is the
    already-resolved harness-registry record for ``sid`` (``None`` on a miss)
    -- callers own how they obtained it (a batch ``snapshot()`` for the loop,
    a single ``lookup(sid)`` for the per-sid path), so this helper performs no
    registry I/O itself. See ``live_session_verdicts``'s own docstring for the
    full per-arm derivation this reproduces exactly.

    Rollback-lever scope (C4a-2, docs/reference/layer1-liveness-activation.md
    § Scope): ``_layer1_disabled()`` is deliberately NOT consulted here.
    ``COORDINATOR_SESSION_LAYER1_DISABLE`` gates ``session_live``'s Layer 1
    arm ONLY -- setting it truthy does not skip this function's equivalent
    stable-pid check. Wiring both would need a second-order decision this
    chunk did not take on (whether the two seams sharing one lever value is
    even the right shape, given they already keep separate fail-open
    counters -- see ``verdict_layer1_unknown_count`` above); left for a
    follow-up if the counters show this arm firing at fleet scale."""
    if record is not None:
        try:
            if core.stable_pid_alive(
                str(record.pid), "", str(int(record.start_epoch))
            ):
                return (True, "harness-registry", None)
        except Exception:
            pass

    stable_pid = core.read_meta_field(sdir, "stable_pid")
    if stable_pid:
        stable_pid_lstart = core.read_meta_field(sdir, "stable_pid_lstart")
        stable_pid_start_epoch = core.read_meta_field(
            sdir, "stable_pid_start_epoch"
        )
        if stable_pid_lstart or stable_pid_start_epoch:
            try:
                live = core.stable_pid_alive(
                    stable_pid, stable_pid_lstart, stable_pid_start_epoch
                )
                basis = "stable-pid"
            except Exception as exc:
                # Fail open, but never silently (C4a-2, docs/reference/
                # layer1-liveness-activation.md § Observability) -- the
                # sibling arm to session_live's Layer 1 except-arm (C4a).
                # Same ruling (§ 2 of that doc): never raise, never change
                # the returned verdict, no file write, no lock.
                live = True
                basis = "unknown"
                # Non-atomic read-modify-write; safe under the current
                # spawn-per-call, single-threaded-per-process model. A future
                # caller adding a thread pool or async loop around this
                # function would need to make this atomic to avoid
                # under-counting.
                global verdict_layer1_unknown_count
                verdict_layer1_unknown_count += 1
                try:
                    logger.debug(
                        "coordinator_core.session.liveness: "
                        "_verdict_for_sdir Layer 1 fail-open (unknown basis) "
                        "for sid=%s: %s (count=%d)",
                        sid,
                        exc,
                        verdict_layer1_unknown_count,
                    )
                except Exception:
                    pass
            return (live, basis, None)
        # stable_pid present, neither witness present -- session_live's
        # Layer-2 fallthrough (A-F1): CLAMPED elapsed.
        pid = core.read_meta_field(sdir, "pid")
        last_iso = core.read_meta_field(sdir, "last_activity")
        last_epoch = core.iso_to_epoch(last_iso)
        if not last_iso:
            last_epoch = _dir_recency_fallback_epoch(sdir)
            basis = "recency-window-mtime"
        else:
            basis = "recency-window"
        elapsed = now_epoch - last_epoch
        if elapsed < 0:
            elapsed = 0
        return (is_session_live(pid, elapsed), basis, elapsed)

    # stable_pid absent -- live_session_ids' own Layer-2 arm: UNCLAMPED
    # elapsed (module negative-spec — do NOT clamp here).
    pid = core.read_meta_field(sdir, "pid")
    last_iso = core.read_meta_field(sdir, "last_activity")
    last_epoch = core.iso_to_epoch(last_iso)
    if not last_iso:
        last_epoch = _dir_recency_fallback_epoch(sdir)
        basis = "recency-window-mtime"
    else:
        basis = "recency-window"
    elapsed = now_epoch - last_epoch  # UNCLAMPED — see module negative-spec
    return (is_session_live(pid, elapsed), basis, elapsed)


def live_session_verdicts(
    cwd: Optional[str] = None,
) -> dict[str, "SessionVerdict"]:
    """THE ONE shared per-id liveness seam: ``id -> (live, basis, age_sec)``.

    Ships to close the fourth-derivation defect (Review: staff-eng second
    pass, Finding 7): ``pickup_assemble.holder_evidence._liveness_basis`` used
    to re-derive the basis independently by calling ``core.stable_pid_alive``
    directly, violating this module's D5 single-liveness-key invariant
    (``core.stable_pid_alive`` "called from exactly ONE place here —
    ``session_live``"). ``_liveness_basis`` now reads its basis off THIS
    function instead. ``live_session_ids`` below is a thin derived wrapper
    over this same function, so there is exactly one per-id liveness
    computation left in the module.

    **This is NOT a per-id loop over ``session_live``, reproduced as a
    wrapper.** That natural-looking implementation would CLAMP negative
    elapsed on the Layer-2 arm taken when ``stable_pid`` is absent, silently
    ERASING this module's own negative spec: "Do NOT clamp negative elapsed in
    ``live_session_ids`` (the non-stable branch) ... This DIFFERS from
    ``session_live``'s Layer-2 arm ... the divergence is faithful to the bash
    originals." So this function reproduces BOTH arms exactly as they exist
    today, per id:
      - ``stable_pid`` present AND (``stable_pid_lstart`` OR
        ``stable_pid_start_epoch``) present -> Layer 1
        (``core.stable_pid_alive``, matching ``session_live``'s own Layer-1
        arm exactly). ``age_sec`` is always ``None`` here — evidence is not
        meaningful on a process-identity verdict, and per this module's own
        contract it must never influence ``live``. An exception from
        ``core.stable_pid_alive`` (e.g. psutil unavailable) is caught HERE
        (not left to propagate as ``session_live`` does) and resolves to
        ``(True, "unknown", None)`` — fail OPEN, never asserted-dead, and
        never asserted as a stronger basis than was actually established.
      - ``stable_pid`` present but BOTH witnesses absent -> Layer-2
        fallthrough (A-F1), using ``session_live``'s CLAMPED elapsed
        arithmetic (``elapsed = max(now - last_epoch, 0)``), with the
        meta-less/mid-write recency-SOURCE substitution
        (``_dir_recency_fallback_epoch``, DoE 642195ba/88929bea) when
        ``last_activity`` is empty/unparseable.
      - ``stable_pid`` absent -> ``live_session_ids``'s OWN Layer-2 arm, using
        the SAME UNCLAMPED ``elapsed = now - last_epoch`` it uses today (the
        module negative-spec divergence above), with the identical
        meta-less/mid-write recency-SOURCE substitution.
    ``age_sec`` on both Layer-2 arms is that arm's own ``elapsed`` value
    (clamped or unclamped as appropriate) — evidence-only, never re-derived a
    second way.

    **The seam stays PER-ID ONLY.** A call-level "no sessions dir" vs "all
    peers dead" distinction cannot be carried inside an ``id -> verdict``
    dict without a new per-id unknown-THIRD-value hazard — narrowing
    ``compute_scope``'s ``my_scope`` safely while raising a stand-down prompt
    in ``pickup_assemble``'s gate for the very same signal, a different kind
    of wrong for the same input. ``compute_scope``'s ``peer_dir_seen``
    disambiguator stays exactly where it is, outside this seam. This function
    introduces no per-id third live/dead/unknown value — ``live`` is always a
    plain ``bool``; only ``basis`` carries the richer vocabulary.

    A session id absent from the returned dict (not in a git repo, sessions
    dir absent, or the dir was skipped as a reserved non-session name such as
    the ``no-session`` sentinel) has no verdict — treat it as not-live, same
    as today's callers already do via absence from ``live_session_ids()``'s
    frozenset.

    NO memoization here (AC8 — a cached live-set reopens the wrong-attribution
    race; the 2.0s TTL lives one layer up in the old bridge, not in this
    function). Fetched once per caller invocation, not per candidate.
    """
    base = core.sessions_dir(cwd)
    if not base:
        return {}
    basep = Path(base)
    if not basep.is_dir():
        return {}

    now_epoch = core.now_epoch()
    # Belt-and-braces over C1's own AC11 contract (harness_registry never
    # raises to its caller): an unexpected exception here must still degrade
    # to "no registry data" rather than propagate.
    try:
        registry = harness_registry.snapshot()
    except Exception:
        registry = {}
    verdicts: dict[str, "SessionVerdict"] = {}
    for sdir_path in basep.iterdir():
        if not sdir_path.is_dir():
            continue
        sid = sdir_path.name
        if sid in _NON_SESSION_DIR_NAMES:
            continue
        sdir = str(sdir_path)

        # Source 0: harness session registry, ONE scan (fetched above) --
        # never a per-id lookup(). Same tolerant compare as the "stable-pid"
        # arm below; a miss or mismatch falls through unchanged.
        record = registry.get(sid)
        verdicts[sid] = _verdict_for_sdir(sid, sdir, record, now_epoch)
    return verdicts


def session_verdict(
    sid: str, cwd: Optional[str] = None
) -> Optional["SessionVerdict | ElsewhereVerdict"]:
    """ONE-id verdict, computed via the SAME per-arm derivation
    ``live_session_verdicts`` uses (``_verdict_for_sdir``) — never a second
    computation — but without that function's whole-corpus scan (Review:
    staff-eng-review C: ``holder_evidence.liveness_basis`` used to call
    ``live_session_verdicts`` and discard every entry but one). Resolves
    ``sid``'s directory via ``core.session_dir`` directly, O(1) like
    ``session_live``.

    No-verdict arm (C1, docs/plans/2026-08-13-liveness-stops-conflating-
    dead-with-elsewhere.md): when this repo has no session dir for ``sid``
    at all, that used to mean bare ``None`` whether ``sid`` is a live
    session working in ANOTHER repo or does not exist anywhere -- 9/51
    under-reports in the oracle audit were every single one this case
    (``state/audits/2026-08-13-session-live-vs-listagents-oracle.md``).
    Before returning ``None`` in that case, this function now also consults
    ``harness_registry`` (the SAME Source 0 read below, via the SAME
    ``lookup()`` helper -- no second parser, AC3) and, on a confirmed hit
    (routed through the one ``core.stable_pid_alive`` seam, D5), returns
    ``(True, "harness-registry-elsewhere", record.cwd)`` -- the THIRD tuple
    slot carries the peer's ``cwd`` (a ``str`` or ``None``) on this ONE
    basis only, not an ``age_sec`` int; existing callers that unpack and
    discard the third field (e.g. ``holder_evidence.liveness_basis``) are
    unaffected. A registry record is a CANDIDATE, not proof of reachability
    -- the harness probes the socket separately and lazily unlinks -- so
    this basis is worded "elsewhere", never "confirmed live" or "reachable".
    An unconfirmed or absent record falls through to the unchanged
    ``None``. This branch never touches ``session_live()``'s own boolean
    arm (AC1) and introduces no DEAD arm (module negative-spec).

    Otherwise returns ``None`` when ``sid`` would have no entry in
    ``live_session_verdicts``' dict: empty sid, no sessions dir, the session
    dir doesn't exist, or ``sid`` is one of ``_NON_SESSION_DIR_NAMES`` (the
    per-sid path does NOT inherit that filtering for free the way the
    whole-corpus loop does — applied explicitly here so e.g. ``"no-session"``
    still resolves to "no verdict" / basis ``"unknown"``, matching the loop).
    A ``sid`` containing any character outside ``_SID_ALLOWED_CHARS`` also
    returns ``None`` (Review: staff-eng slice-A P2; tightened to an
    allowlist per coordinator:code-reviewer's colon/drive-letter gap
    finding) — ``core.session_dir`` is a bare join with no validation of its
    own, and the whole-corpus loop this function is meant to equal could
    never produce such a value (it only ever sees real child directory
    names). Without this guard,
    ``holder_evidence.liveness_basis`` (reached with a ``holder_sid`` read
    off disk) would ``is_dir()`` and read ``meta.json`` fields from an
    arbitrary directory outside the sessions corpus.

    NOT a corpus-scan equivalent on NTFS: ``live_session_verdicts`` keys its
    dict by the on-disk directory name, so a case-variant ``sid`` (e.g.
    ``"S-ABC"`` when the directory is ``"s-abc"``) is absent from that dict
    (``None`` from ``.get``). ``core.session_dir`` + ``Path.is_dir()`` is
    case-INSENSITIVE on Windows, so this function resolves a full verdict for
    the same case-variant input where the whole-corpus dict would not. This
    divergence is undocumented behaviour inherited from ``Path.is_dir()``,
    not a deliberate design choice; see the P2 finding in
    ``state/review-trail/findings/residuals-sliceA-liveness-findings.md`` for
    the open question of whether to resolve the real on-disk name instead.
    """
    if not sid or sid in _NON_SESSION_DIR_NAMES:
        return None
    if not _SID_ALLOWED_CHARS.issuperset(sid):
        return None
    sdir = core.session_dir(sid, cwd)
    if not sdir or not Path(sdir).is_dir():
        # No-verdict arm (C1, docs/plans/2026-08-13-liveness-stops-
        # conflating-dead-with-elsewhere.md): this repo has no session dir
        # for `sid` at all, which used to return bare None regardless of
        # whether `sid` is a live session working in ANOTHER repo or does
        # not exist anywhere. Consult harness_registry -- the SAME Source 0
        # this function already reads below, via the SAME `lookup()` helper
        # (no second parser, AC3) -- before giving up. A hit still routes
        # through the one core.stable_pid_alive seam (D5); an unconfirmed
        # or absent record falls through to the unchanged `None` (AC1:
        # session_live's own boolean arm is untouched by this branch).
        try:
            record = harness_registry.lookup(sid)
        except Exception:
            record = None
        if record is not None:
            try:
                if core.stable_pid_alive(
                    str(record.pid), "", str(int(record.start_epoch))
                ):
                    # `cwd` is carried in the age_sec slot ONLY on this
                    # basis -- it is not an elapsed-seconds value, and no
                    # existing caller reads that slot for this basis (see
                    # "harness-registry-elsewhere" in the vocabulary block
                    # above `_verdict_for_sdir`). A registry record is a
                    # CANDIDATE, not proof of reachability -- the harness
                    # probes the socket separately and lazily unlinks -- so
                    # this is worded as "elsewhere", never "confirmed".
                    return (True, "harness-registry-elsewhere", record.cwd)
            except Exception:
                pass
        return None
    try:
        record = harness_registry.lookup(sid)
    except Exception:
        record = None
    now_epoch = core.now_epoch()
    return _verdict_for_sdir(sid, sdir, record, now_epoch)


def live_session_ids(cwd: Optional[str] = None) -> FrozenSet[str]:
    """Port of ``cs_live_session_ids`` — Q24 HOTSPOT.

    Return the frozenset of currently-live session ids. THIN DERIVED WRAPPER
    over ``live_session_verdicts()`` (the ONE shared per-id liveness seam) —
    existing callers are untouched, and the output is set-identical to before
    this wrapping for every fixture, including the negative-elapsed/
    clock-skew case (``live_session_verdicts`` reproduces this function's own
    UNCLAMPED Layer-2 arm exactly; see that function's docstring for why a
    naive per-id loop over ``session_live`` would have silently clamped it
    away). This is the NATIVE replacement for the ``coordinator_core.liveness``
    bash bridge.

    Directory iteration order is UNSORTED (the frozenset return makes order
    immaterial anyway). Returns an empty frozenset when not in a git repo /
    the sessions dir is absent (matches the bash early ``return 0`` with no
    output).
    """
    return frozenset(
        sid for sid, (live, _basis, _age_sec) in live_session_verdicts(cwd).items()
        if live
    )


def resolve_live_session_ids() -> FrozenSet[str]:
    """Native replacement for ``coordinator_core.liveness.resolve_live_session_ids``.

    Zero-argument entry point — the name ``core.resolve_session_id`` imports
    for its tier-4 ambiguity guard (core.py:441 currently imports the bash-
    bridged ``coordinator_core.liveness``; wave 3 repoints it to THIS module).
    Delegates to ``live_session_ids()`` with cwd-current resolution, returning
    the same frozenset the bash bridge produced (RAW-PID-LIVENESS preserved).
    """
    return live_session_ids()


def active_sessions(cwd: Optional[str] = None) -> list:
    """Port of ``cs_active_sessions``.

    Human-readable Live/Stale listing (one string per active, non-archived
    session), liveness delegated to ``session_live`` (two-layer). Returns a
    list of formatted lines rather than printing — the caller renders.

    Line format (matches the bash ``printf``):
        ``<sid padded to 60>  Live (last activity Nm ago)``
        ``<sid padded to 60>  Stale (last activity Nh ago, reap threshold is 24h)``

    Elapsed buckets: ``<60s`` -> ``Ns``; ``<3600s`` -> ``Nm``; ``<86400s`` ->
    ``Nh``; else ``Nd``. Negative elapsed is clamped to 0 against clock skew
    (NTP jump / VM resume / DST). PID is NOT part of the key — diagnostic only.
    Skips ``.archive`` / ``.agents``.

    Special returns mirror the bash echoes:
      - not in a git repo -> ``[]`` (bash ``return 0`` with no output).
      - sessions dir absent -> ``["(no coordinator-sessions dir yet)"]``.
      - dir present but no active sessions -> ``["(no active sessions)"]``.

    "Stale" here is the 30-min LIVENESS boundary, NOT the reap threshold
    (session archival requires 24h inactivity) — a 30m-24h session shows Stale
    but is not yet reapable.

    Skips ``_NON_SESSION_DIR_NAMES`` (the same denylist ``live_session_ids``
    filters on — ``.archive``/``.agents`` plus the claim-lock dirs, the legacy
    advisory-lock dir, the shared log dir, and the ``no-session`` sentinel).
    This function reuses the module-level constant rather than a local literal
    pair so the two enumeration paths cannot re-diverge (state/improvement-
    queue/2026-07-21-active-sessions-mislabels-infra-dirs-as-6f843f92699b.yaml
    — this listing used to only exclude ``.archive``/``.agents`` and so
    surfaced ``handoff-claims``, ``memo-claims``, ``plan-claims``,
    ``agent-sessions-locks``, ``logs``, and ``no-session`` as if they were
    sessions).
    """
    base = core.sessions_dir(cwd)
    if not base:
        return []
    basep = Path(base)
    if not basep.is_dir():
        return ["(no coordinator-sessions dir yet)"]

    now_epoch = core.now_epoch()
    lines: list = []
    found = False
    # bash pathname expansion (`base/*/`) is alphabetically sorted — preserve it.
    for sdir_path in sorted(basep.glob("*/")):
        if not sdir_path.is_dir():
            continue
        sid = sdir_path.name
        if sid in _NON_SESSION_DIR_NAMES:
            continue
        found = True
        sdir = str(sdir_path)

        last_iso = core.read_meta_field(sdir, "last_activity")
        last_epoch = core.iso_to_epoch(last_iso)
        elapsed_sec = now_epoch - last_epoch
        if elapsed_sec < 0:
            elapsed_sec = 0

        if elapsed_sec < 60:
            label = f"{elapsed_sec}s ago"
        elif elapsed_sec < 3600:
            label = f"{elapsed_sec // 60}m ago"
        elif elapsed_sec < 86400:
            label = f"{elapsed_sec // 3600}h ago"
        else:
            label = f"{elapsed_sec // 86400}d ago"

        if session_live(sid, cwd):
            lines.append(f"{sid:<60}  Live (last activity {label})")
        else:
            lines.append(
                f"{sid:<60}  Stale (last activity {label}, reap threshold is 24h)"
            )

    if not found:
        return ["(no active sessions)"]
    return lines
