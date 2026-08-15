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

Four outcomes, distinguishable by the caller as structured data — collapsing
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
    "not_reachable"  — no ADDRESS can be built for the input. NEVER a
                       fallback guess. `reason` names which of the five
                       distinct facts produced it (see `NotReachableReason`
                       below); collapsing them reports "no such session"
                       for a session that is demonstrably live and busy.
    "ambiguous"      — the input's own `sessionId` maps to more than one
                       registry record; `candidates` lists every match's own
                       resolved address, and the caller must not pick. Kept
                       as a distinguishable contract arm even though no input
                       reaching this function today can produce it -- see
                       `resolve_address`'s own docstring for why.

The harness's messaging inbox is a RUNTIME capability, not a given
(measured live 2026-08-14, Claude Code 2.1.232, Windows, 44/44
`<claude-config>/sessions/*.json` records). `messagingSocketPath` is
written into a session record from `CLAUDE_CODE_MESSAGING_SOCKET`, and
that env var is set at exactly one place in the harness: a SUCCESSFUL
`startCrossSessionInbox()` bind, which the harness itself skips when its
cross-session-messaging gate is off (`"[uds-messaging] Skipped:
cross-session messaging gate off"`). With the gate off, no record on the
box carries the field at all. That is not a renamed key and not a drifted
parser -- the harness's OWN peer enumeration filters its session records
on the same field (`.filter(r => r.sock && r.sock !== self)`), so when it
is absent everywhere the harness lists ZERO local peers and refuses every
`SendMessage` to one, whatever address a caller supplies. There is
therefore no address to compute, and no key to correct: the field's
absence IS the capability's absence, and this module reports that rather
than manufacturing an address string the harness would reject.

The gate's DEFAULT is a remote GrowthBook flag (telemetry name
`agents_cross_session_inbox`) -- re-verified 2026-08-15 against Claude
Code 2.1.233 (`~/.local/share/claude/versions/2.1.233`; see
`coordinator_core.session.harness_registry`'s module docstring for the
full measurement, including a LATE-BIND path where a mid-session
GrowthBook refresh can turn messaging ON for an already-running session).

It is NOT remote-only, and an earlier revision of this docstring was
wrong to say there is no local workaround: from bundle 2.1.232 the gate
predicate short-circuits `true` on the `CLAUDE_CODE_HARBOR_KITE`
environment variable ahead of both the platform check and the GrowthBook
read, and `coordinator/bin/claude-doe.py` sets it for every session it
launches. Nothing in THIS module can pin or force the gate -- that is a
launch-time concern, not a resolver concern -- but a caller reading a
`not_reachable` from here must not conclude the switch is out of the
fleet's hands. Ownership detail lives in `harness_registry`'s module
docstring; why that launcher default reached no interactive session on
Windows is answered in
`state/audits/2026-08-15-why-the-harbor-kite-default-binds-nothing.md`.

2.1.233 also stamps every record with `peerProtocol: <int>` (measured
2026-08-15: 42/42 records, all `peerProtocol: 1`, 0/42 carrying
`messaging_socket_path` -- same gate-off signature as 2026-08-14's
44/44). This module does NOT read `peerProtocol` and `messaging_
available()` below MUST NOT be changed to key on it: the harness's own
FleetView peer listing uses `peerProtocol` (plus liveness, plus a 24h
recency window) to decide peer VISIBILITY, a materially looser question
than the DELIVERABILITY question this module answers -- a peer can be
`peerProtocol >= 1` and listed while still having no `messaging_socket_
path` and thus no address `SendMessage` could actually reach (that is
precisely today's box-wide state). `messaging_available()` stays scoped
to `messaging_socket_path` on purpose.

    → `messaging_available()`, and the `NotReachableReason` vocabulary.

Negative-spec:
    - `messaging_socket_path` is never SUBSTITUTED when absent. No
      `sessionId`, `pid`, `cwd`, or record-file path is hashed in its
      place to produce a stand-in `ref`: the harness hashes its own live
      socket path and nothing else, so any substitute yields a
      confidently-wrong address -- the exact shape the spec's Anti-scope
      forbids ("a confident wrong address is worse than no address: it
      injects into an unrelated session's context and the intended
      recipient never learns anything was sent").
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
    - `fallback_channel` is never guessed toward `FallbackChannel.PEER_NOTICE`
      on an unconfirmed target location -- an absent/null `cwd`, or a
      `cwd` this call cannot prove is inside its own working tree, resolves
      to `FallbackChannel.CROSS_REPO_MEMO` instead, same as a confirmed
      different repo. `peer_notice.send`'s delivery is same-working-tree
      only, so a wrong `PEER_NOTICE` sends the reader down a channel that
      structurally cannot deliver -- worse than the collapsed-reason defect
      this addition exists to close, because it reads as actionable
      (`cross-repo/inbox/2026-08-15-example-retrieval-repo-em-peer-session-messaging-
      unavailable.md`'s EM Response).
    - `fallback_channel` is set ONLY for `NotReachableReason.
      MESSAGING_UNAVAILABLE` -- never for `NO_OWNER_ID`, `NO_LIVE_RECORD`,
      `PEER_INBOX_ABSENT`, or `NO_PEER_NAME`. Those four have no channel
      remedy (a different peer, or nothing at all, not a different
      transport), and giving them one would suggest a fix that does not
      apply.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from coordinator_core.session import harness_registry

_SOCKET_ENV_VAR = "CLAUDE_CODE_MESSAGING_SOCKET"

_REF_MIN_LEN = 6
_REF_MAX_LEN = 12


class NotReachableReason:
    """The `not_reachable` arm's five distinct causes, named.

    A caller that prints "not reachable" without one of these tells a
    reader "there is no such session" for a session that may be live,
    busy, and simply unaddressable because the harness's messaging inbox
    is not bound. Each constant states a different fact about a different
    subject, and no two are interchangeable:

        NO_OWNER_ID          — the caller passed an empty/falsy id. Says
                               nothing about the registry, which is not
                               read on that path.
        NO_LIVE_RECORD       — the id has no live registry record. The
                               original, exact meaning of "not reachable".
        MESSAGING_UNAVAILABLE— the id HAS a live record, and NO record in
                               the whole registry carries a messaging
                               socket: the harness's cross-session inbox
                               is unbound box-wide, so no peer has an
                               address, this one included. A property of
                               the HARNESS, not of the target session.
        PEER_INBOX_ABSENT    — the id has a live record with no messaging
                               socket while other records have one: peer
                               messaging works here, but this session
                               never registered an inbox. A property of
                               the TARGET, not of the harness.
        NO_PEER_NAME         — the id has a live record WITH a messaging
                               socket but no harness-rendered `name`, so
                               the `f"{name} [{ref}]"` form has no name
                               half to build from. The pre-existing
                               missing-`name` degrade (module docstring's
                               "never falls back to the refuted
                               first-two-hex-digits heuristic"), now
                               named instead of silent.

    Split rather than merged because the remedies differ and a caller
    surfacing the wrong one sends its reader somewhere useless: the
    harness-wide arm is not fixed by picking a different peer, and the
    per-peer arm is not fixed by anything about the harness.
    """

    NO_OWNER_ID = "no-owner-id"
    NO_LIVE_RECORD = "no-live-record"
    MESSAGING_UNAVAILABLE = "peer-messaging-unavailable"
    PEER_INBOX_ABSENT = "peer-inbox-absent"
    NO_PEER_NAME = "no-peer-name"


class FallbackChannel:
    """The two surviving channels once `SendMessage` itself is unreachable,
    named for the field `ResolveResult.fallback_channel` carries.

    Populated ONLY alongside `NotReachableReason.MESSAGING_UNAVAILABLE` --
    the harness-wide arm, never a per-peer one (see `ResolveResult.
    fallback_channel`'s own docstring for why). Repo-conditional by
    construction, per `cross-repo/inbox/2026-08-15-example-retrieval-repo-em-peer-
    session-messaging-unavailable.md`'s EM Response: a flat pointer is
    wrong for roughly half its readers, since `peer_notice.send` delivery
    is same-working-tree only (`docs/reference/peer-notice-channel.md`
    § "Delivery is same-working-tree only").

        PEER_NOTICE       — the target's `cwd` is within THIS call's own
                             working tree. `peer_notice.send` can reach it.
        CROSS_REPO_MEMO    — the target's `cwd` is confirmed outside this
                             working tree, OR could not be confirmed at all
                             (absent/null `cwd`, or no live record for the
                             target). `peer_notice.send` cannot be trusted
                             here -- sending a same-working-tree-only
                             pointer to an unconfirmed or cross-repo target
                             is the exact failure this module exists to
                             avoid (a confidently-wrong channel is worse
                             than none: it reads as actionable and is not).
                             `coordinator/bin/cross-repo-memo` is the one
                             channel that does not need to know where the
                             target lives.
    """

    PEER_NOTICE = "peer_notice.send"
    CROSS_REPO_MEMO = "cross-repo-memo"


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

    `reason` is populated ONLY on the "not_reachable" arm, always, with
    one of `NotReachableReason`'s five constants -- it is `None` on every
    other outcome, where the outcome itself is the whole answer. Additive
    with a `None` default: an existing caller switching on `outcome`
    alone is unaffected, and one that wants to say WHY reads this instead
    of re-deriving it from a second registry read it does not have.

    `fallback_channel` is populated ONLY when `reason ==
    NotReachableReason.MESSAGING_UNAVAILABLE` -- the harness-wide arm,
    the one whose remedy is "use a different channel" rather than "pick a
    different peer" (module docstring, `NotReachableReason`'s own
    docstring). `None` on every other `not_reachable` reason (a per-peer
    arm has no channel remedy -- see `FallbackChannel`) and on every
    non-`not_reachable` outcome. One of `FallbackChannel`'s two constants
    when set, never a guessed or asserted-safe default when the target's
    repo location could not be confirmed -- see `FallbackChannel.
    CROSS_REPO_MEMO`'s own docstring for why an unconfirmed `cwd` resolves
    to the memo channel rather than being left `None` or defaulting to
    `peer_notice.send`.
    """

    outcome: str
    session_id: str | None = None
    address: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    reason: str | None = None
    fallback_channel: str | None = None


def messaging_available(snapshot: dict) -> bool:
    """True if ANY live record carries a `messaging_socket_path` -- i.e.
    the harness's cross-session messaging inbox is bound somewhere on this
    box and peer addresses can exist at all.

    Mirrors the harness's own local-peer enumeration, which filters its
    session records on exactly this field (`r.sock && r.sock !== self`)
    before probing each socket: a registry in which no record carries one
    yields an EMPTY peer list from the harness itself, so every address
    this module could compute would be refused. False is therefore a fact
    about the harness's capability, not about any session's liveness --
    every record in the snapshot may be a live, busy process.

    Takes the snapshot as an argument rather than reading one: `resolve_
    address` already holds the single fresh scan its own contract pins
    (one `harness_registry.snapshot()` per call), and a second scan here
    would both break that count and give a torn view across the two
    reads.

    Negative-spec: this is NOT a liveness signal and NOT a per-peer
    reachability test. A `True` return says only that an address form
    exists to be computed; the harness still probes the socket and lazily
    unlinks unreachable records, so a resolved address remains a
    candidate, never a delivery guarantee (spec § 3, "Record presence is
    not reachability").

    Negative-spec, 2.1.233: do NOT key this on `peerProtocol` (the field
    the harness's own FleetView listing filters on for peer VISIBILITY).
    Visibility and DELIVERABILITY are different predicates on this harness
    version -- measured 2026-08-15, every record on this box carried
    `peerProtocol: 1` while 0/42 carried `messaging_socket_path` -- so a
    peer can be listed and still be unaddressable. This function answers
    the deliverability question and must keep answering it with `messaging_
    socket_path` alone; see the module docstring for the full measurement.

    A `False` return does NOT say whether anything asked the harness to open
    the gate. That question is `coordinator_core.session.messaging_gate.
    classify()`, a separate self-scoped read of the calling session's own
    environment -- kept out of this predicate deliberately: box-level
    deliverability and this session's own request are different facts, and
    folding either into the other is the collapse that module exists to undo.
    """
    return any(record.messaging_socket_path for record in snapshot.values())


def _normalize_path(path: str) -> str:
    """Resolve symlinks, absolutize, and normalize-case a path for
    working-tree containment comparison.

    Deliberately mirrors `coordinator_core.session.peer_roster._normalize_
    path` (same rationale: `realpath` resolves symlinks on both sides
    before comparison, `normpath` cleans any residual `..`/`.` segments,
    `normcase` is the one cross-platform-correct way to compare two
    `cwd`-shaped strings without assuming either side's platform -- Windows
    is first-class here, per CLAUDE.md) rather than importing that
    module's private helper: the two modules are edited by different
    sessions in this shared tree this session, and a cross-module import of
    an underscore-prefixed name would couple this change to a file outside
    its own scope for four lines of logic neither side is likely to drift
    on independently.
    """
    return os.path.normcase(os.path.normpath(os.path.realpath(path)))


def _same_working_tree(target_cwd: str | None, this_repo_root: str) -> bool:
    """True only if `target_cwd` is confirmed to be `this_repo_root` or a
    subdirectory of it -- never a guess.

    A falsy `target_cwd` (absent/null on the registry record) returns
    `False`, not an exception and not an optimistic `True`: an unconfirmed
    location must resolve to the CROSS_REPO_MEMO fallback (see
    `FallbackChannel.CROSS_REPO_MEMO`'s docstring), the same safe default a
    confirmed-different-repo location gets, since `peer_notice.send`'s
    same-working-tree-only delivery makes a wrong `True` here strictly
    worse than a wrong `False`.
    """
    if not target_cwd:
        return False
    norm_target = _normalize_path(target_cwd)
    norm_root = _normalize_path(this_repo_root)
    return norm_target == norm_root or norm_target.startswith(norm_root + os.sep)


def _fallback_channel_for(sid: str, snapshot: dict, this_repo_root: str) -> str:
    """The `FallbackChannel` remedy for `sid`'s `MESSAGING_UNAVAILABLE`
    classification -- called only from that one branch of `resolve_address`.

    `this_repo_root` names THIS call's own working tree, not the target's --
    a peer's `SendMessage` unreachability is resolved against where the
    CALLER can act, which is the working tree `peer_notice.send` would need
    to write into. `snapshot.get(sid)` re-reads the same record `_not_
    reachable_reason` already inspected for `sid` (no second registry scan --
    both draw from the one `harness_registry.snapshot()` `resolve_address`
    already holds).
    """
    record = snapshot.get(sid)
    target_cwd = record.cwd if record is not None else None
    if _same_working_tree(target_cwd, this_repo_root):
        return FallbackChannel.PEER_NOTICE
    return FallbackChannel.CROSS_REPO_MEMO


def _not_reachable_reason(sid: str, snapshot: dict) -> str:
    """Classify WHY `sid` has no address, into one of
    `NotReachableReason`'s registry-derived four.

    Never called for a falsy owner id -- that path returns
    `NO_OWNER_ID` without reading a snapshot at all, since a caller's own
    empty input is not evidence about the registry.
    """
    record = snapshot.get(sid)
    if record is None:
        return NotReachableReason.NO_LIVE_RECORD
    if not record.messaging_socket_path:
        if not messaging_available(snapshot):
            return NotReachableReason.MESSAGING_UNAVAILABLE
        return NotReachableReason.PEER_INBOX_ABSENT
    return NotReachableReason.NO_PEER_NAME


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
    return _resolve_addresses_bulk_from_snapshot(session_ids, snapshot)


def _resolve_addresses_bulk_from_snapshot(session_ids: list[str], snapshot: dict) -> dict[str, str]:
    """The snapshot-scoped core of `resolve_addresses_bulk`, factored out so
    `resolve_addresses_bulk_with_availability` can pair it with one
    `messaging_available(snapshot)` read off the SAME snapshot -- two
    separate `harness_registry.snapshot()` calls one after another risk a
    torn view (the registry is a live directory scan, not a stable
    read), which would let a caller's address dict and its "is messaging
    on at all" verdict disagree about the instant they describe."""
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


def resolve_addresses_bulk_with_availability(session_ids: list[str]) -> tuple[dict[str, str], bool]:
    """`resolve_addresses_bulk`'s address dict, paired with `messaging_
    available(snapshot)` off the SAME snapshot read -- one scan answers both
    "what is each id's address" and "is there any address to be had at all".

    Built for a caller that renders `""` (this specific peer has no
    address) and box-wide messaging-unavailable (no peer on this box can
    have one) as two DIFFERENT facts -- `resolve_addresses_bulk` alone
    collapses both into `""`, which is exactly the defect named in
    `cross-repo/inbox/2026-08-15-example-retrieval-repo-em-peer-messaging-gate-off-vs-
    proven-round-trip.md` § "Smaller, concrete: the empty-string rendering":
    an empty `send_message_address` reads as "resolved to nothing" when the
    true fact is "the harness's cross-session inbox is unbound box-wide,
    same as `messaging_available()` already reports on the peer-roster
    surface".

    Negative-spec: a `False` availability flag does NOT mean every entry in
    the returned dict is `""` -- a `"<this session>"` self-match is
    unaffected by box-wide availability (a session always resolves itself).
    It DOES mean every OTHER falsy entry is falsy for the box-wide reason,
    not a per-peer one, which is exactly the distinction the caller needs to
    render `send_message_address` as `None` + a named reason instead of the
    ambiguous `""`.
    """
    snapshot = harness_registry.snapshot()
    addresses = _resolve_addresses_bulk_from_snapshot(session_ids, snapshot)
    return addresses, messaging_available(snapshot)


def _not_reachable_result(sid: str, snapshot: dict, this_repo_root: str) -> ResolveResult:
    """Build the `not_reachable` `ResolveResult` for `sid`, pairing `reason`
    with `fallback_channel` in the one place both of `resolve_address`'s
    `not_reachable` returns need it -- `sid` need not itself be a key in
    `snapshot` (the "no match at all" caller passes the raw `owner_id`
    straight through, same as `_not_reachable_reason` already accepts).
    """
    reason = _not_reachable_reason(sid, snapshot)
    fallback_channel = (
        _fallback_channel_for(sid, snapshot, this_repo_root)
        if reason == NotReachableReason.MESSAGING_UNAVAILABLE
        else None
    )
    return ResolveResult(
        outcome="not_reachable", reason=reason, fallback_channel=fallback_channel
    )


def resolve_address(owner_id: str, this_repo_root: str | None = None) -> ResolveResult:
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

    `this_repo_root` names THIS call's own working tree, used ONLY to
    classify `fallback_channel` when `reason == MESSAGING_UNAVAILABLE`
    (see `FallbackChannel`). Defaults to `os.getcwd()` when omitted,
    mirroring `peer_roster.build_roster`'s own default -- pass a caller's
    resolved git root (e.g. the JSON-RPC dispatch's own per-call
    `repo_root`) when one is already in hand, since it is a steadier
    signal than a raw `cwd` that may sit in a subdirectory.

    Every "not_reachable" return carries a `reason` from
    `NotReachableReason` -- there is no unexplained arm. The one this
    fleet hits today is `MESSAGING_UNAVAILABLE`: the harness's
    cross-session inbox gate is off, so no record carries a
    `messaging_socket_path`, no peer has an address, and the harness's own
    peer list is empty (module docstring, § runtime capability). A caller
    must not read that as "no such session" -- the target may be live and
    busy; nothing on this box can address it. That arm's `ResolveResult`
    also carries `fallback_channel`, repo-conditional per `FallbackChannel`'s
    own docstring -- the other four reasons never set it (their remedy is
    not a channel switch).

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
        return ResolveResult(
            outcome="not_reachable", reason=NotReachableReason.NO_OWNER_ID
        )

    root = this_repo_root if this_repo_root else os.getcwd()

    snapshot = harness_registry.snapshot()

    self_info = harness_registry.self_record()
    self_sid = self_info[0] if self_info is not None else None

    matches = _matching_session_ids(owner_id, snapshot)

    if not matches:
        return _not_reachable_result(owner_id, snapshot, root)

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
        return _not_reachable_result(sid, snapshot, root)

    return ResolveResult(outcome="reachable", session_id=sid, address=candidate.address)
