"""
coordinator_core.session.reachability — live UUID -> SendMessage-address resolver.

Purpose: work-state artifacts record their owner as a session UUID in one of
several conventions (`claimed_by`, `authoring_session`, `created_by_session`,
`agent_sessions` entries, `state/subagent-share/<id>/` directory names).
`SendMessage`/`ListAgents` address a peer by name, `f"{name} [{ref}]"`. Nothing
in the tree mapped one onto the other until this module. Builds a live answer
per call over `coordinator_core.session.harness_registry.snapshot()` — never a
published or cached-to-disk registry (Anti-scope,
`state/handoffs/2026-08-13-session-owner-reachability-registry.md`).

Spec backlink: `state/handoffs/2026-08-13-session-owner-reachability-registry.md`
§ 1, verified against `state/audits/2026-08-13-session-live-vs-listagents-oracle.md`
§ "The join".

Derivation rules (independently verified 5/5 against a live `ListAgents`
capture, per the handoff):

    ref  = sha256(("session:" + messagingSocketPath).encode()).hexdigest()[:6],
           lengthened one hex char at a time (max 12) while it shares a
           prefix with any OTHER live candidate's own 12-hex-truncated hash.
    name = slug(basename(cwd)) + "-" + one-random-byte-hex (opaque; this
           module never attempts to derive, validate, or reverse the suffix).
    address = f"{name} [{ref}]" UNCONDITIONALLY for a non-self candidate --
           the harness requires the ref-qualified form for a cross-session
           `SendMessage` target regardless of whether the bare name collides
           with another live candidate's name (measured live 2026-08-13: a
           uniquely-named peer's bare name was refused by the harness, which
           demanded the ref-qualified form to confirm the target). Collision
           still governs how LONG the ref is (see `_widen_ref`), never
           whether it is present.

Caller-side recovery note: a live cross-repo test (peer session, 2026-08-13)
observed that when a `SendMessage` to a bare name is refused, the harness's
refusal text hands back the correct ref-qualified form for a retry. This
module does not read, parse, or retry on that text -- it is an affordance
for the calling consumer, not a contract to pattern-match.

Three outcomes, distinguishable by the caller as structured data — collapsing
them is the defect this module exists to close:

    "own_session"   — the input resolves to the CALLING session's own record,
                       via EITHER of two independent signals:
                       `harness_registry.self_record()` (primary, pid-keyed),
                       or `CLAUDE_CODE_MESSAGING_SOCKET` matching the record's
                       `messaging_socket_path` as an opaque string (second
                       signal; exists because the pid resolver can decline
                       for a correct pid -- measured live 2026-08-13,
                       `env-miss:name-mismatch` with `CLAUDE_PID` correctly
                       set -- see `resolve_address`'s own docstring). Never
                       mislabelled `not_reachable`; a session cannot
                       `SendMessage` itself via this address form regardless.
    "reachable"      — exactly one live record matches; `address` is set.
    "not_reachable"  — no live record matches. NEVER a fallback guess.
    "ambiguous"      — the input's own `sessionId` maps to more than one
                       registry record; `candidates` lists every match's own
                       resolved address, and the caller must not pick. Kept
                       as a distinguishable contract arm even though no input
                       reaching this function today can produce it -- see
                       `resolve_address`'s own docstring for why.

Negative-spec:
    - No cross-call snapshot, cache, or file is written. `harness_registry.
      snapshot()` performs its own single fresh directory scan per call
      (see that module) — this module reads it exactly once per
      `resolve_address()` call and holds no state between calls.
    - Never falls back to the refuted first-two-hex-digits heuristic, or any
      other guess, when a record is missing `name`/`messaging_socket_path` —
      that degrades to `not_reachable`, same as a registry miss.
    - Never treats `session_live()`/liveness as an input. Reachability here
      is "has a live harness registry record with a usable address", a
      distinct question from process liveness (handoff § 2) — this module
      does not import `coordinator_core.session.liveness`.
    - Never mutates or reads `~/.claude/teams/` — measured strictly worse
      than the registry as a reachability oracle (audit § "May NOT rely on").
    - The `CLAUDE_CODE_MESSAGING_SOCKET` self-signal never matches on a
      `None == None` coincidence: an absent/empty env var and a record
      whose `messaging_socket_path` is `None` are each treated as
      non-matching in their own right, before the comparison runs -- a
      match here would silently classify an arbitrary peer as the caller,
      strictly worse than the `self_record()` decline this signal exists
      to cover.
    - An `ambiguous` candidate that itself lacks a usable name/socket carries
      `address=None`, never the raw session id -- printing `.address`
      unconditionally must never emit a bare UUID as though it were a real
      `SendMessage` address (Review: code-reviewer -- P3, "confidently wrong
      address" shape the spec's Anti-scope forbids).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from coordinator_core.session import harness_registry

_SOCKET_ENV_VAR = "CLAUDE_CODE_MESSAGING_SOCKET"

_REF_MIN_LEN = 6
_REF_MAX_LEN = 12


@dataclass(frozen=True)
class Candidate:
    """One live registry record resolved to its `SendMessage` address.

    `address` is `None` for an `ambiguous`-arm match that itself lacks a
    usable `name`/`messaging_socket_path` (that arm's own unresolvable
    slot) -- never the raw session id. A caller printing `.address`
    unconditionally must see an unmistakable non-address marker, not a bare
    UUID that could be mistaken for a real `SendMessage` address (Anti-scope,
    `state/handoffs/2026-08-13-session-owner-reachability-registry.md`).

    For that same unresolvable slot, `name` and `ref` are `""` (empty
    string), not `None` -- deliberately distinct from `address`'s `None`
    contract. Nothing derives a `SendMessage` name/ref for an id lacking a
    usable record, so there is no "real value withheld" case to signal with
    `None`; `""` states plainly that no name/ref was computed, while
    `address` reserves `None` specifically to prevent a raw UUID being
    mistaken for a resolved address."""

    session_id: str
    name: str
    ref: str
    address: str | None


@dataclass(frozen=True)
class ResolveResult:
    """The resolver's structured answer -- `outcome` is the caller's switch.

    `session_id`/`address` are populated only for "own_session"/"reachable".
    `candidates` is populated only for "ambiguous", one entry per matching
    live session id, each carrying its own already-resolved address (or
    `None` if that candidate itself lacks a usable name/socket -- the
    `Candidate.address` slot is `None` for that entry, never the raw
    session id, so a caller printing `.address` unconditionally never
    prints a bare UUID as though it were a real `SendMessage` address).
    """

    outcome: str
    session_id: str | None = None
    address: str | None = None
    candidates: list[Candidate] = field(default_factory=list)


def _full_hash12(messaging_socket_path: str) -> str:
    """The 12-hex-char truncated sha256 of `"session:" + messagingSocketPath`.

    The widening loop's own comparanda -- each candidate's `ref` is a prefix
    of this value, never a value in its own right.
    """
    digest = hashlib.sha256(("session:" + messaging_socket_path).encode("utf-8"))
    return digest.hexdigest()[:_REF_MAX_LEN]


def _widen_ref(full12: str, other_full12s: list[str]) -> str:
    """Lengthen `full12`'s prefix from 6 to 12 hex chars until it no longer
    shares a prefix with any OTHER candidate's own full 12-hex hash.

    Mirrors the harness's own collision-widening loop exactly (handoff § 1) --
    do not hardcode a length of 6.

    Negative-spec: two candidates whose `messaging_socket_path` is byte-
    identical hash identically at every length, so this loop returns the
    full 12-hex prefix for BOTH -- non-unique, by construction, since there
    is no length at which they diverge. Documented as a stated PRECONDITION
    violation, not handled: each live session carries its own distinct
    socket, so two genuinely distinct live sockets can never reach this
    branch; a misconfigured pair sharing one socket string is out of scope
    and gets no speculative recovery logic here (Review: code-reviewer --
    P2, degenerate-input awareness only).
    """
    for length in range(_REF_MIN_LEN, _REF_MAX_LEN + 1):
        candidate_ref = full12[:length]
        if not any(other[:length] == candidate_ref for other in other_full12s):
            return candidate_ref
    return full12[:_REF_MAX_LEN]


def _resolve_one(sid: str, snapshot: dict) -> Candidate | None:
    """Build one session id's `Candidate`, or `None` if it lacks a usable
    `name`/`messaging_socket_path` (degrades to `not_reachable`/a `None`
    slot in `candidates` -- never a guessed address)."""
    record = snapshot.get(sid)
    if record is None or not record.name or not record.messaging_socket_path:
        return None

    full12 = _full_hash12(record.messaging_socket_path)
    other_full12s = [
        _full_hash12(other.messaging_socket_path)
        for other_sid, other in snapshot.items()
        if other_sid != sid and other.messaging_socket_path
    ]
    ref = _widen_ref(full12, other_full12s)

    address = f"{record.name} [{ref}]"

    return Candidate(session_id=sid, name=record.name, ref=ref, address=address)


def resolve_candidates(snapshot: dict) -> list[Candidate]:
    """Resolve every live session in `snapshot` to its own `Candidate`.

    A public seam over `_resolve_one` for callers that need EVERY live
    session's own resolved address, not one owner id's match set (the
    `resolve_address()` question) -- built for
    `coordinator_core.session.peer_roster`
    (`state/handoffs/2026-08-13-live-peer-roster.md` § 2). `resolve_address`
    itself is unchanged by this addition and does not call it.

    Ref-widening is computed by `_resolve_one` over the WHOLE `snapshot`
    passed in here -- never a filtered subset -- so a caller that filters
    the RETURNED list (e.g. by `cwd`) after this call gets the same
    ref/address every unfiltered caller would see for that session.
    Filtering `snapshot` itself before calling this function would silently
    change other sessions' refs and is a caller error, not a supported
    input shape.

    A session id whose record lacks a usable `name`/`messaging_socket_path`
    is omitted from the result entirely (matching `_resolve_one`'s own
    `None` return) rather than emitting a placeholder `Candidate` with a
    guessed or blank address.
    """
    candidates: list[Candidate] = []
    for sid in snapshot:
        candidate = _resolve_one(sid, snapshot)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _matching_session_ids(owner_id: str, snapshot: dict) -> list[str]:
    """Every live session id exactly equal to `owner_id`.

    Exact match only: the four recording conventions this module accepts
    (`claimed_by`, `authoring_session`, `created_by_session`,
    `agent_sessions` entries, `subagent-share/<id>/` directory names) all
    record full UUIDs today (state/handoffs/
    2026-08-13-session-owner-reachability-registry.md § 1's governing
    criterion is "accepts owner ids in every recording convention already
    in the tree", not every possible substring of one). A short-prefix
    tolerance was removed here deliberately: it was never in the spec, and
    `snapshot` is `sessionId`-keyed, so an exact-match lookup can only ever
    yield zero or one candidate -- see `resolve_address`'s docstring for
    why `ambiguous` is retained anyway.
    """
    if owner_id in snapshot:
        return [owner_id]
    return []


def _socket_env_self_match(sid: str, snapshot: dict) -> bool:
    """True if `CLAUDE_CODE_MESSAGING_SOCKET` exactly equals `sid`'s own
    `messaging_socket_path` -- the second, independent self-signal.

    Exists because `harness_registry.self_record()` can decline for a
    session whose `CLAUDE_PID` IS correctly set: measured live
    2026-08-13, `_resolve_claude_pid_from_env()` returned
    `env-miss:name-mismatch` for a verified-correct pid, which silently
    degraded `resolve_address`'s `own_session` arm into `reachable` and
    handed the caller their own address as if it were a peer's (state/
    handoffs/2026-08-13-session-owner-reachability-registry.md § 1). The
    harness's own exclusion mechanism keys on this socket path, not the
    pid, so it is an independent oracle rather than a re-derivation of
    the same signal.

    Compared as an OPAQUE STRING ONLY -- no path normalisation, no
    symlink resolution, no `stat`, no POSIX assumption (Windows is
    first-class here). An empty/missing env var or a `None` record
    socket never match each other: a bare `None == None` compare would
    classify an arbitrary peer as the caller, which is a worse defect
    than the one this closes, so both sides are required to be a
    non-empty string before comparison.
    """
    env_socket = os.environ.get(_SOCKET_ENV_VAR)
    if not env_socket:
        return False
    record = snapshot.get(sid)
    if record is None or not record.messaging_socket_path:
        return False
    return env_socket == record.messaging_socket_path


def resolve_advisory_address(session_id: str | None) -> str:
    """Best-effort bare `SendMessage` address for `session_id`, or `""` on
    any resolution outcome that isn't a usable address.

    The shared resolution core `baton_assemble._resolve_claimed_by_address_
    suffix` and `pickup_assemble.compute_competing_claim`/
    `compute_successor_handoffs` each format into their own caller-specific
    shape (a parenthetical suffix vs. a bare `send_message_address` field) --
    this function owns only the `ResolveResult.outcome` -> bare-string
    mapping, once, so neither caller re-derives it.

    Spec backlink: `state/handoffs/2026-08-13-session-owner-reachability-
    registry.md` § 3; `cross-repo/inbox/2026-08-13-doe-claude-em-peer-
    roster-doctrine-reply.md` § Counter 2.

    Negative-spec: never raises on a well-formed `session_id` -- `""` on
    `not_reachable`/`ambiguous` is a normal outcome, not a failure path.
    Does NOT catch an exception from `resolve_address` itself (e.g. a
    `harness_registry.snapshot()` read failure): that stays the CALLER's
    responsibility, mirroring `_resolve_claimed_by_address_suffix`'s own
    proven shape of a caller-local `try/except Exception: return ""` around
    a caller-local `from coordinator_core.session import reachability` --
    this function does not itself decide whether its own import should be
    advisory; only a caller importing it that way is.
    """
    if not session_id:
        return ""
    result = resolve_address(session_id)
    if result.outcome == "reachable" and result.address:
        return result.address
    if result.outcome == "own_session":
        return "<this session>"
    return ""


def resolve_addresses_bulk(session_ids: list[str]) -> dict[str, str]:
    """Resolve many session ids to their `resolve_advisory_address` values
    off ONE `harness_registry.snapshot()` read, not one per id.

    Built for `pickup_assemble.compute_competing_claim`/
    `compute_successor_handoffs`, whose `candidates` list routinely carries
    ~18 entries on the live corpus -- calling `resolve_address` once per
    candidate would re-snapshot the live registry 18 times for one brief
    (`harness_registry.snapshot()`'s own contract is a fresh directory scan
    per call, Anti-scope above). This function snapshots once and reuses
    `resolve_candidates` (already whole-snapshot-scoped, per its own
    docstring) for the non-self roster, plus one `self_record()` read for
    the two-signal self-classification `resolve_address` itself performs
    per id.

    Spec backlink: `state/handoffs/2026-08-13-session-owner-reachability-
    registry.md` § 3; `cross-repo/inbox/2026-08-13-doe-claude-em-peer-
    roster-doctrine-reply.md` § Counter 2 (performance).

    Negative-spec: a `session_id` present in `session_ids` but absent from
    the live snapshot maps to `""` in the returned dict, never a raised
    `KeyError` for a caller indexing by that id. A FALSY `session_id`
    (`""`/`None`) is instead SKIPPED by the loop (`if not sid: continue`)
    and never appears as a key in the returned dict at all -- distinct from
    the absent-but-truthy case above, and unlike `resolve_advisory_address`
    (which accepts a falsy id and returns `""` inline). A caller indexing
    the returned dict directly with a falsy key, rather than going through
    `.get(sid, "")`, gets a `KeyError`, not `""` (Review: code-reviewer --
    P3, docstring/loop mismatch).
    """
    snapshot = harness_registry.snapshot()
    self_info = harness_registry.self_record()
    self_sid = self_info[0] if self_info is not None else None
    live_candidates = {c.session_id: c for c in resolve_candidates(snapshot)}

    result: dict[str, str] = {}
    for sid in session_ids:
        if not sid:
            continue
        if sid == self_sid or _socket_env_self_match(sid, snapshot):
            result[sid] = "<this session>"
            continue
        candidate = live_candidates.get(sid)
        result[sid] = candidate.address if candidate is not None and candidate.address else ""
    return result


def resolve_address(owner_id: str) -> ResolveResult:
    """Resolve `owner_id` (a full session UUID) to its live `SendMessage`
    address.

    Queries `harness_registry.snapshot()` exactly once -- a single fresh
    directory scan, per that module's own single-scan contract -- and
    persists nothing beyond this call's return value.

    Self-classification uses TWO independent signals; either firing is
    sufficient for `own_session`:

      1. `harness_registry.self_record()` -- the primary, pid-keyed signal.
      2. `CLAUDE_CODE_MESSAGING_SOCKET` compared, as an opaque string,
         against the matched record's `messaging_socket_path` -- a second
         signal because signal 1 can decline for a correct pid (measured
         live: `_resolve_claude_pid_from_env()` -> `env-miss:name-mismatch`
         with `CLAUDE_PID` correctly set). See `_socket_env_self_match`.

    Negative-spec: an absent/empty env var, or a record with
    `messaging_socket_path is None`, never contributes a match on signal 2
    -- guarded explicitly so two different sessions can never compare
    equal via a `None == None` coincidence.

    Negative-spec, `ambiguous`: `_matching_session_ids` matches `owner_id`
    against `harness_registry.snapshot()`'s dict exactly, and that dict is
    `sessionId`-keyed -- an exact-match lookup against it can therefore
    yield at most ONE candidate. `harness_registry.snapshot()`'s own
    docstring states that if two files in the registry directory carry the
    same `sessionId`, "the later one in `Path.glob`'s OS-dependent
    iteration order wins (last writer wins, order unspecified)" -- i.e. it
    de-duplicates at parse time, before this function ever sees the
    result. So no input reaching `resolve_address` today can produce
    `ambiguous`: it is retained as a deliberate contract arm, not dead
    code awaiting a sweep, because a future non-deduplicating read path
    (a raw multi-record scan bypassing `snapshot()`'s last-writer-wins
    collapse) would make it live again, and callers already switch on it.
    A short-prefix input that used to trigger it now resolves to
    `not_reachable` instead, since no live session id will equal a short
    string outright.
    """
    if not owner_id:
        return ResolveResult(outcome="not_reachable")

    snapshot = harness_registry.snapshot()

    self_info = harness_registry.self_record()
    self_sid = self_info[0] if self_info is not None else None

    matches = _matching_session_ids(owner_id, snapshot)

    if not matches:
        return ResolveResult(outcome="not_reachable")

    if len(matches) == 1 and (
        matches[0] == self_sid or _socket_env_self_match(matches[0], snapshot)
    ):
        return ResolveResult(outcome="own_session", session_id=matches[0])

    if len(matches) > 1:
        candidates = [
            _resolve_one(sid, snapshot) or Candidate(sid, "", "", None)
            for sid in sorted(matches)
        ]
        return ResolveResult(outcome="ambiguous", candidates=candidates)

    sid = matches[0]
    candidate = _resolve_one(sid, snapshot)
    if candidate is None:
        return ResolveResult(outcome="not_reachable")

    return ResolveResult(outcome="reachable", session_id=sid, address=candidate.address)
