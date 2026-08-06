"""
coordinator_core.session.liveness — the coordinator session hub's LIVENESS
module.

Port of: liveness.sh (example-doctrine-repo 6aa77d4b, 2026-07-21).

This is a PURE IN-PROCESS LIBRARY, not an IPC op — it self-registers nothing
and touches none of the shared op-registry files. It provides the two-layer
session-liveness model and the claim-layer liveness/identity predicates that
the whole claim + reaper + enumeration stack routes through.

TWO-LAYER LIVENESS MODEL (ported verbatim from the bash header):
    Layer 1 — PPID-authoritative process-aliveness (when ``stable_pid`` is
      present in meta.json): ``session_live`` delegates to
      ``core.stable_pid_alive`` on the ``stable_pid`` + ``stable_pid_lstart``
      / ``stable_pid_start_epoch`` fields. Process alive + birth-instant match
      -> LIVE (authoritative; recency NOT consulted). Process gone or lstart
      mismatch (PID recycled) -> DEAD within seconds of process exit.
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
    "always dead in-harness"), never for a session-liveness verdict.

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
      clamp; the divergence is faithful to the bash originals.
    - Do NOT let a meta-less/unparseable-meta session dir read confirmed-DEAD
      by defaulting its recency to epoch-0 — that let a peer wrongfully take
      over a session that was merely mid-write (example-doctrine-repo 642195ba, follow-up
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

import os
import re
from pathlib import Path
from typing import FrozenSet, Optional

from coordinator_core.session import core

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
#: Found on this repo's real on-disk corpus 2026-07-21 while diagnosing the
#: meta.json-glob invisibility defect this constant exists to close.
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
    }
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
    (example-doctrine-repo 642195ba, follow-up 88929bea) -- see the call site's comment for the
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

    Two-layer decision:
      Layer 1 (PPID-authoritative): if ``stable_pid`` is non-empty in
        meta.json AND ``stable_pid_lstart`` is present, delegate to
        ``core.stable_pid_alive`` and RETURN its verdict — recency is NOT
        consulted. ``stable_pid`` present but ``stable_pid_lstart`` ABSENT
        (partial write / TOCTOU between the two meta writes) is NOT treated as
        dead — fall through to Layer 2 to preserve the safety net (A-F1).
      Layer 2 (recency fallback): read ``pid`` + ``last_activity``, convert via
        ``core.iso_to_epoch``, ``elapsed = now - last`` clamped ``>= 0``,
        then ``is_session_live(pid, elapsed)``. When ``last_activity`` is
        EMPTY (no meta.json, unparseable meta.json, or the field itself is
        missing/null -- ``read_meta_field`` returns "" on all three),
        ``last_epoch`` is substituted via ``_dir_recency_fallback_epoch``
        (example-doctrine-repo 642195ba / 88929bea) rather than defaulting to epoch-0 -- see
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
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return False
    if not Path(sdir).is_dir():
        return False

    # Layer 1: PPID-authoritative process check (when stable_pid captured at init).
    stable_pid = core.read_meta_field(sdir, "stable_pid")
    if stable_pid:
        stable_pid_lstart = core.read_meta_field(sdir, "stable_pid_lstart")
        stable_pid_start_epoch = core.read_meta_field(sdir, "stable_pid_start_epoch")
        # lstart absent != process dead — fall through to Layer 2 (A-F1).
        if stable_pid_lstart:
            return core.stable_pid_alive(
                stable_pid, stable_pid_lstart, stable_pid_start_epoch
            )
        # stable_pid present but lstart absent — fall through to Layer 2.

    # Layer 2: recency fallback (stable_pid absent, legacy meta, or Guard-1 miss).
    pid = core.read_meta_field(sdir, "pid")
    last_iso = core.read_meta_field(sdir, "last_activity")
    last_epoch = core.iso_to_epoch(last_iso)
    if not last_iso:
        # Wrongful-takeover fallback (example-doctrine-repo 642195ba, follow-up 88929bea): a
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


def live_session_verdicts(
    cwd: Optional[str] = None,
) -> dict[str, tuple[bool, str, Optional[int]]]:
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
      - ``stable_pid`` present AND ``stable_pid_lstart`` present -> Layer 1
        (``core.stable_pid_alive``, matching ``session_live``'s own Layer-1
        arm exactly). ``age_sec`` is always ``None`` here — evidence is not
        meaningful on a process-identity verdict, and per this module's own
        contract it must never influence ``live``. An exception from
        ``core.stable_pid_alive`` (e.g. psutil unavailable) is caught HERE
        (not left to propagate as ``session_live`` does) and resolves to
        ``(True, "unknown", None)`` — fail OPEN, never asserted-dead, and
        never asserted as a stronger basis than was actually established.
      - ``stable_pid`` present but ``stable_pid_lstart`` absent -> Layer-2
        fallthrough (A-F1), using ``session_live``'s CLAMPED elapsed
        arithmetic (``elapsed = max(now - last_epoch, 0)``), with the
        meta-less/mid-write recency-SOURCE substitution
        (``_dir_recency_fallback_epoch``, example-doctrine-repo 642195ba/88929bea) when
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
    verdicts: dict[str, tuple[bool, str, Optional[int]]] = {}
    for sdir_path in basep.iterdir():
        if not sdir_path.is_dir():
            continue
        sid = sdir_path.name
        if sid in _NON_SESSION_DIR_NAMES:
            continue
        sdir = str(sdir_path)
        stable_pid = core.read_meta_field(sdir, "stable_pid")
        if stable_pid:
            stable_pid_lstart = core.read_meta_field(sdir, "stable_pid_lstart")
            if stable_pid_lstart:
                stable_pid_start_epoch = core.read_meta_field(
                    sdir, "stable_pid_start_epoch"
                )
                try:
                    live = core.stable_pid_alive(
                        stable_pid, stable_pid_lstart, stable_pid_start_epoch
                    )
                    basis = "stable-pid"
                except Exception:
                    live = True
                    basis = "unknown"
                verdicts[sid] = (live, basis, None)
                continue
            # stable_pid present, lstart absent -- session_live's Layer-2
            # fallthrough (A-F1): CLAMPED elapsed.
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
            verdicts[sid] = (is_session_live(pid, elapsed), basis, elapsed)
            continue

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
        verdicts[sid] = (is_session_live(pid, elapsed), basis, elapsed)
    return verdicts


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
