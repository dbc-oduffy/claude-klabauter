"""Select and throttle the Group EM's nudge population (roadmap `gem-01`,
baton `gem-14`).

Ported in-plane from `coordinator/skills/group-em/send_pass.py` in the
sibling DoE-claude repo (resolve via `repos.doe_claude`, not a hardcoded
drive path; plan `docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md`,
chunk C2).
Rationale, measurements, the superseded first shape, and the PM ruling that
licensed the original: `docs/decisions/DR-group-em-send-narrows-on-the-obligation-ledger.md`
(DoE-claude tree). Tripwire: `A-PAUSED-ROSTER-IS-NOT-A-NUDGE-LIST`. The rules
alone are below.

**It selects and throttles. It does not send.** GATE 1/GATE 2 are declared
per entry, in prose, by the Group EM. GATE 2 has no instrument, so no code can
clear it; a module that sent anyway would clear a gate it cannot evaluate.

**The roster is the population** -- `read_pass` bounds it, a human adjudicates
it. This module adds throttling and the gate, never another filter.

**The obligation ledger ranks; it does not admit.** An undischarged, unfired
record orders the digest most-owed-first. `None` means no ledger exists at all
-- a producer coverage gap, never evidence the peer owes nothing, and never
grounds to exclude. Gating on it was built first and measured inert.

**The `fired` latch is honoured, not just `discharged_at`** -- the ledger
hook's own predicate (`discharged_at is None and not fired`), implemented here
rather than called, held equivalent against the real producer by a wire-path
test. A fired record already reached that peer once; re-presenting it is the
repeat-fire class AC6 forbids.

**Infers NOTHING.** Every input is a record something else concretely
observed. No elapsed-time, idle-duration, session-age, or transcript-shape
predicate exists in the eligibility path -- `send_suppression_reason`, the
admission rule the emission path calls, takes no clock; pinned. The one clock
is the cooldown, throttling this session's own offers, never a peer.

NEGATIVE SPEC -- deliberately absent:

- **No send, no write to any peer's state.** Only this session's own log.
- **No `PAUSED:away` nudge, ever** -- excluded by name and by allow-list, and
  reported as `never-send-reason` ahead of any bookkeeping cause. `away` was
  unobserved in the 2026-08-30 window, so the exclusion is structural and
  untested against live traffic.
- **No shouldn't-be adjudication.** An open obligation says the peer resolved
  a next move and has not invoked it -- not that it is stuck.
- **No GATE 2 instrument.** `peer_roster.status` is never read (negative-
  spec'd; 1465 s stale measured, unbounded to 6.9 h), and the obligation
  ledger is not repurposed as a receiver-state proxy -- it answers a different
  question. Every entry carries `gate1`/`gate2` as `None`.
- **No CPU-delta leg** -- the band-separation finding is RETRACTED.
- **No `Stop` registration**, no re-derivation of the stood-down watcher's
  restore recipe (AC6). Invoked from the composed `groupem.enter` op, never a
  hook.
- **No per-peer entry point.** `build_send_digest` is the sole route to an
  entry; there is deliberately no function that offers one peer in isolation,
  so the per-peer firehose stays unreachable from this module's API rather
  than merely discouraged. Do not add one, even as a private helper.

ADDRESS RESOLUTION (`resolve_addressee`, plan `2026-08-31-the-group-em-tick-
carries-standing-obligations.md` chunk C9). A digest entry's `session_id` is
read-side truth as of the tick that built it; a re-point (the same NAME
resolving to a different, newer session) between digest build and the
prose-gated send is a live, dated, first-party failure mode -- not a
theorised one -- and it propagates silently: a stale binding read as current
looks delivered even when it lands on the wrong session. `resolve_addressee`
is the one function that re-resolves a name against the LIVE registry, at
call time, rather than trusting any held or logged binding.

**It is a refusal function, not a lookup with a fallback.** It returns the
current `name` for `peer_session_id` only if today's live roster still maps
that exact session id to a name; every other case -- the session id is gone,
unresolved, or the roster cannot be read -- is `None`. `None` must never
degrade to "send to the peer_session_id anyway" or "send to the last-known
name anyway": both are the exact failure this function exists to close.

NEGATIVE SPEC:

- **No caching, no memoized binding.** Every call re-reads
  `peer_roster.build_roster`; there is no reuse of a name resolved on an
  earlier tick or an earlier call in the same digest.
- **Not a sender.** This module still sends nothing (see the module-level
  NEGATIVE SPEC above) -- `resolve_addressee` only answers "what name is
  this session id live under right now", for a caller elsewhere to use as
  the send target or to refuse the send on `None`.
- **No name-keyed lookup.** The only key this function accepts is a session
  id; it never accepts or resolves a bare name, which would reintroduce the
  exact stale-binding hazard it exists to close.

DECLINATION (plan `2026-08-31-the-group-em-tick-carries-standing-obligations.md`
chunk C3, wording pinned to `coordinator/skills/group-em/SKILL.md` sha
`8583cf8f5`, DoE-claude tree): "Each tick records a DECLINATION for every
roster entry it does not message -- which gate failed and why. A tick that
closes on 'nothing sent' with no declination is indistinguishable from a
tick that never looked." `decline()` writes that record, on the SAME log
`_record_offer` writes to, distinguished by an `outcome` discriminator now
carried on both row shapes (`"offer"` / `"declination"`). `gate` and
`reason` are supplied by the caller -- the Group EM -- never inferred: the
declination is a stated act, not a computed one.

NEGATIVE SPEC:

- **`decline` records; it does not gate, suppress, or auto-resolve.** It
  must never become a path that decides not to send.
- **It does not arm or extend a cooldown.** Declining is not offering --
  conflating them would silently throttle a peer the EM chose not to
  message this tick, via a mechanism that has nothing to do with declining.

OPEN OBLIGATIONS. `build_send_digest` now also reports, as
`open_obligations`, every peer session id this tick observed (emitted or
held under cooldown) whose most recent log event -- across BOTH rows this
module writes -- is an offer with no later declination. An entry emitted
THIS tick is definitionally open (it was just offered; a declination for it
cannot yet exist). This is the belt to C2's suspenders: a peer offered on
an earlier tick, then held by cooldown on every tick since with the EM
never declining it, keeps surfacing here rather than going quiet the moment
cooldown starts suppressing its entry.

DWELL TIME. Every EMITTED entry (never a suppressed row) carries
`dwell_seconds` -- how long that peer has sat since its last observed
activity, derived from its receiver-state `stamped_at` cross-checked
against `read_pass._transcript_mtime_epoch` (the one place that turns a
session id into a transcript mtime; reused rather than re-derived). The
more RECENT of the two -- a stale `stamped_at` with a transcript that kept
moving is evidence of later activity than the stamp alone would report --
is treated as the last-activity instant, and `dwell_seconds` is `now` (the
same clock this module already threads through cooldown arithmetic; no
second clock is introduced and the wall clock is never asked directly)
minus that instant. `None` when neither source resolves, or when the
result would be negative (clock skew) -- never `0`, which would read as
"just stopped" rather than "unknown". RANKS AND INFORMS ONLY: no threshold
is applied here and no peer is withheld or reordered for its dwell -- the
EM weighs it against what it knows, per GATE 2; a hard floor here would
repeat the cooldown-as-eligibility conflation this module already avoids
elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Callable, Optional

from coordinator_core.group_em import read_pass
from coordinator_core.session import peer_roster

#: `gate` values `decline()` accepts -- which gate the EM declared against.
#: No other value is written; `decline()` refuses anything else.
DECLINE_GATES = frozenset({"gate1", "gate2"})

#: Reader/fallback `reason` strings a nudge may be offered for. Both spell the
#: same condition -- the peer's turn closed -- on the two `read_pass` legs
#: (`turn-ended` from the receiver-state ladder, `tail-turn-duration` from the
#: bounded transcript-tail marker). Enumerated, never pattern-matched.
SEND_ELIGIBLE_REASONS = frozenset({"turn-ended", "tail-turn-duration"})

#: Excluded by name so the exclusion is greppable rather than implied by the
#: allow-list. 17 of 23 paused sessions in the durable 30-row dataset are
#: `away`, and no Director prods an `away` session (roadmap Sec5.2).
NEVER_SEND_REASONS = frozenset({"away"})

#: Per-peer cooldown: a peer offered in one digest is suppressed from later
#: digests in this session until it elapses. Throttle, not a classifier.
DEFAULT_COOLDOWN_SECONDS = 3600

#: Rate ceiling: the most entries one digest may carry, whatever the roster
#: size. A digest at the ceiling is reported truncated rather than silently
#: cut, so the Group EM knows the population exceeded it.
#:
#: `truncated` IS REDUNDANT AND STAYS. Overengineering review (Kira, finding 6,
#: 2026-08-30) is correct that it is derivable -- it is exactly
#: `eligible_before_ceiling > len(entries)`, and the per-peer `rate-ceiling`
#: rows in `suppressed` carry strictly more information than either scalar.
#: EM ruling: keep all three. These payload keys are a NEGOTIATED CROSS-REPO
#: SURFACE, frozen with doe-claude-em at sha 7b0b827f; the DoE-side consumer
#: reads them, so trimming one here is a contract break, not a cleanup. The
#: finding's own suggested_fix says so and defers the call to the EM against
#: the contract memo. Revisit only by renegotiating the contract with that
#: consumer, never by a local tidy-up.
DEFAULT_MAX_ENTRIES = 5

_SEND_LOG_FILENAME = "group-em-send-log.jsonl"
_LEDGER_FILENAME = "next-move-ledger.jsonl"

#: A session id arrives from `claude agents --json` (peers) and the
#: environment (the caller), and is joined straight into a path
#: `_record_offer` will `makedirs`. The sibling reader
#: (`receiver_state_reader.receiver_state_path`) rejects an unsafe component,
#: a bare `.`/`..` the character class alone would pass included; the same
#: guard applies here rather than trusting the producer.
_SAFE_SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_session_id(session_id: Any) -> bool:
    return (
        isinstance(session_id, str)
        and bool(session_id)
        and session_id not in (".", "..")
        and bool(_SAFE_SID_RE.match(session_id))
    )


def _session_share_dir(repo_root: str, session_id: str) -> str:
    return os.path.join(repo_root, "state", "subagent-share", session_id)


def undischarged_obligations(repo_root: str, session_id: str) -> Optional[int]:
    """Count this peer's open, unfired obligations; `None` if it has no ledger.

    `None` (no ledger file at all) and `0` (a ledger saying nothing is owed)
    are deliberately distinct -- the first is a producer coverage gap.
    Unparseable lines are skipped: a malformed ledger degrades to a lower
    count, never to a crash or an inferred obligation.
    """
    if not _safe_session_id(session_id):
        return None
    path = os.path.join(_session_share_dir(repo_root, session_id), _LEDGER_FILENAME)
    if not os.path.exists(path):
        return None
    count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("discharged_at") is None and not record.get("fired"):
                    count += 1
    except OSError:
        return None
    return count


def send_suppression_reason(verdict: dict[str, Any]) -> Optional[str]:
    """Why the send path must not offer this verdict, or `None` to admit it.

    The single admission rule, the one `build_send_digest` itself calls, so the
    pins bind what entries actually have. Doubles as the `suppressed[].why`
    label. Takes no clock and no obligation count -- the ledger ranks, never
    admits. Fails closed on every unrecognised shape.
    """
    if not verdict.get("candidate"):
        return "not-a-candidate"
    reason = verdict.get("reason")
    if reason in NEVER_SEND_REASONS:
        return "never-send-reason"
    if reason not in SEND_ELIGIBLE_REASONS:
        return "reason-not-eligible"
    return None


def send_log_path(repo_root: str, caller_session_id: str) -> str:
    """This session's own record of which peers it has already offered.

    Per-session bookkeeping beside `advisory-fire-counts.jsonl`. Session-
    scoped: a new Group EM starts with an empty cooldown, matching the DACI
    ruling that the Driver role ends with the session.
    """
    return os.path.join(
        _session_share_dir(repo_root, caller_session_id), _SEND_LOG_FILENAME
    )


def offer_key(caller_session_id: str, peer_session_id: str) -> str:
    """The cooldown's key: a salted digest, never the peer's session id.

    A peer session id IS an address here -- its receiver-state path, share
    directory, and transcript path are all built from that string -- so
    storing one would breach the no-persisted-address rule. The caller's own
    id salts it; the log answers only "did I offer this, when".
    """
    return hashlib.sha256(
        (caller_session_id + "|" + peer_session_id).encode("utf-8")
    ).hexdigest()


def read_send_log(repo_root: str, caller_session_id: str) -> list[dict[str, Any]]:
    """Every offer this session has recorded. `[]` when there is no log yet."""
    path = send_log_path(repo_root, caller_session_id)
    if not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _record_offer(
    repo_root: str,
    caller_session_id: str,
    peer_session_id: str,
    now: Optional[float] = None,
) -> bool:
    """Append one offer, starting its cooldown. `False` if the write failed.

    Internal: `build_send_digest` calls this per emitted entry, so the cooldown
    arms itself rather than depending on the caller. Failure is reported, never
    raised -- the caller must be able to say so. There is deliberately no
    public counterpart -- do not expose this as a per-peer entry point.
    """
    now = time.time() if now is None else now
    if not _safe_session_id(caller_session_id) or not _safe_session_id(peer_session_id):
        return False
    path = send_log_path(repo_root, caller_session_id)
    line = json.dumps(
        {
            "outcome": "offer",
            "offer_key": offer_key(caller_session_id, peer_session_id),
            "offered_at": now,
        },
        sort_keys=True,
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return False
    return True


def decline(
    repo_root: str,
    caller_session_id: str,
    peer_session_id: str,
    gate: str,
    reason: str,
    now: Optional[float] = None,
) -> bool:
    """Record a DECLINATION for `peer_session_id` on the shared send log.

    `gate` is the gate the EM declared against (`"gate1"` or `"gate2"`);
    `reason` is free prose. Both are supplied by the caller and refused if
    absent or malformed -- neither is inferred, because the whole point is
    that the declination is a stated act, not a computed one. Returns
    `False` on any refusal (bad ids, bad gate, empty reason) or write
    failure, never raises -- matching `_record_offer`'s failure contract.

    NEGATIVE SPEC: records only. Never gates, suppresses, or auto-resolves
    a send; never arms or extends the offer cooldown (see module
    docstring's DECLINATION section) -- declining is not offering.
    """
    now = time.time() if now is None else now
    if not _safe_session_id(caller_session_id) or not _safe_session_id(peer_session_id):
        return False
    if gate not in DECLINE_GATES:
        return False
    if not isinstance(reason, str) or not reason.strip():
        return False
    path = send_log_path(repo_root, caller_session_id)
    line = json.dumps(
        {
            "outcome": "declination",
            "offer_key": offer_key(caller_session_id, peer_session_id),
            "declined_at": now,
            "gate": gate,
            "reason": reason,
        },
        sort_keys=True,
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return False
    return True


def _log_key_is_open(log: list[dict[str, Any]], key: str) -> bool:
    """Is this offer_key's most recent event an offer with no later declination?

    Legacy rows written before this chunk carry no `outcome` at all; they
    are read as `"offer"` rows (their only prior meaning) so an existing log
    does not spuriously go quiet the moment this ships.
    """
    last_offer_at: Optional[float] = None
    last_decline_at: Optional[float] = None
    for record in log:
        if record.get("offer_key") != key:
            continue
        outcome = record.get("outcome", "offer")
        if outcome == "declination":
            at = record.get("declined_at")
            if isinstance(at, (int, float)) and not isinstance(at, bool):
                if last_decline_at is None or at > last_decline_at:
                    last_decline_at = at
        else:
            at = record.get("offered_at")
            if isinstance(at, (int, float)) and not isinstance(at, bool):
                if last_offer_at is None or at > last_offer_at:
                    last_offer_at = at
    if last_offer_at is None:
        return False
    if last_decline_at is not None and last_decline_at >= last_offer_at:
        return False
    return True


# Review: overengineering-reviewer (finding #6, confidence 8 AUTO-FIX,
# EM-in-scope discretionary application) -- the local `_parse_iso_stamp` copy
# is deleted. Its stated reason for staying local ("that symbol is private to
# its own module") was already contradicted three lines below by this same
# module importing `read_pass._transcript_mtime_epoch`, also private. Calls
# `read_pass._parse_iso_stamp` directly, the same way `_transcript_mtime_epoch`
# already is.


def _dwell_seconds(
    repo_root: str,
    peer_session_id: str,
    now: float,
    cwd: Optional[str] = None,
) -> Optional[float]:
    """Seconds since this peer's last observed activity, or `None`.

    Reads its own receiver-state `stamped_at` and cross-checks it against
    `read_pass._transcript_mtime_epoch(peer_session_id, cwd or repo_root)` --
    the later of the two is the last-activity instant (a transcript that kept
    moving after the stamp was written is evidence of activity the stamp
    alone would miss). `now` is the SAME clock `build_send_digest` already
    threads through cooldown arithmetic -- never re-asked here. `None` when
    neither source resolves or the result would be negative (clock skew);
    never `0`, which would misreport "unknown" as "just stopped".

    `cwd` is the peer's OWN cwd (the roster row's `cwd`, threaded onto the
    verdict dict by `read_pass.classify_peer`), never assumed to equal
    `repo_root` -- `_transcript_path_for` encodes the exact cwd into the
    harness's per-project transcript directory, so a peer whose cwd is a
    subdirectory of `repo_root` (permitted by `build_roster`'s "within
    repo_root" filter) needs its own cwd here, matching the fallback
    `peer.get("cwd") or repo_root` pattern `classify_peer`/
    `_transcript_moved_since` already use. Review: coordinator:code-reviewer
    (finding 1) -- this previously always passed `repo_root`, silently
    degrading `dwell_seconds` to `None` forever for any such peer.
    """
    effective_cwd = cwd or repo_root
    candidates: list[float] = []
    record = read_pass.read_receiver_state(peer_session_id, repo_root)
    if record is not None:
        stamp_dt = read_pass._parse_iso_stamp(record.get("stamped_at"))
        if stamp_dt is not None:
            candidates.append(stamp_dt.timestamp())
    transcript_epoch = read_pass._transcript_mtime_epoch(peer_session_id, effective_cwd)
    if transcript_epoch is not None:
        candidates.append(transcript_epoch)
    if not candidates:
        return None
    last_activity = max(candidates)
    dwell = now - last_activity
    if dwell < 0:
        return None
    return dwell


def _cooldown_remaining(
    records: list[dict[str, Any]],
    key: str,
    now: float,
    cooldown_seconds: int,
) -> float:
    """Seconds left on this peer's cooldown; `0.0` when it may be offered.

    Degenerate timestamps are neutralised, not trusted: non-numeric ignored,
    future (skew, ms-epoch) ignored, result clamped to the window. A corrupt
    log must not silently suppress a peer forever -- nothing would surface it.
    """
    remaining = 0.0
    for record in records:
        if record.get("offer_key") != key:
            continue
        offered_at = record.get("offered_at")
        if not isinstance(offered_at, (int, float)) or isinstance(offered_at, bool):
            continue
        if offered_at > now:
            continue
        left = min(cooldown_seconds - (now - offered_at), float(cooldown_seconds))
        if left > remaining:
            remaining = left
    return remaining


def _suppressed(session_id, why, reason=None, obligations=None, remaining=None, dwell=None):
    """One `suppressed` row. Every row carries the same keys -- `None` where
    inapplicable -- so a consumer never has to key-check by variant.

    Review: overengineering-reviewer (finding #4, EM-ratified partial) --
    `obligation`/`dwell_seconds` folded in here rather than round-tripped
    through a separate `declined` row. Per-peer declination was a pure
    projection of this row (`reason` was verbatim `row["why"]`); a consumer
    wanting the per-peer declination now reads it off `suppressed` directly.
    `obligation` is the same `f"message peer {session_id}"` shape every
    reader already derived from `session_id` alone.
    """
    return {
        "session_id": session_id,
        "why": why,
        "reason": reason,
        "undischarged_obligations": obligations,
        "cooldown_remaining_seconds": remaining,
        "obligation": f"message peer {session_id}",
        "dwell_seconds": dwell,
    }


def resolve_addressee(
    repo_root: str,
    peer_session_id: str,
    build_roster: Optional[Callable[..., list]] = None,
) -> Optional[str]:
    """The live `name` bound to `peer_session_id` right now, or `None`.

    Re-reads the live registry (`peer_roster.build_roster`) on every call --
    never a cached or previously-logged binding -- and returns the `name`
    off the row whose `session_id` still equals `peer_session_id` today.
    `None` covers every other case: the session id is not in today's roster,
    the row it's still in carries no usable `name`, or the roster read
    itself failed. `None` is a REFUSAL -- the caller must not fall back to
    addressing `peer_session_id` (a bare session id is not a `SendMessage`
    target -- see the `no-persisted-address` rule this module already
    carries) or to any name recorded earlier.

    `build_roster` is a test-injection seam: `(repo_root=...) -> list[PeerRow]`,
    standing in for `peer_roster.build_roster` without touching the live
    registry in a test. `None` (the default) calls the real thing.
    """
    if not _safe_session_id(peer_session_id):
        return None
    roster_fn = build_roster if build_roster is not None else peer_roster.build_roster
    try:
        rows = roster_fn(repo_root=repo_root)
    except Exception:
        return None
    for row in rows:
        if getattr(row, "session_id", None) == peer_session_id:
            name = getattr(row, "name", None)
            return name if isinstance(name, str) and name else None
    return None



def _declinations(
    roster: list[dict[str, Any]],
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Tick-level declinations only -- one row per thing the TICK ITSELF declined,
    never a per-peer row.

    Review: overengineering-reviewer (finding #4, EM-ratified partial) -- the
    per-peer declination previously emitted here was a pure projection of
    `suppressed` (`reason` was verbatim `row["why"]`, `obligation` was derivable
    from `session_id` alone); those rows are gone from this function, and a
    consumer now reads a peer's declination off `suppressed` directly, which
    now carries `obligation`/`dwell_seconds` itself (see `_suppressed`).

    What survives is genuinely new information, not derivable from `entries` +
    `suppressed`: THE EMPTY ROSTER IS ITSELF A DECLINATION, and it is the case
    this function exists for. DoE's SKILL.md: "Each tick records a DECLINATION
    for every roster entry it does not message -- which gate failed and why. A
    tick that closes on 'nothing sent' with no declination is indistinguishable
    from a tick that never looked, which is the failure this whole mechanism
    exists to end." A per-entry list is legitimately empty when there are no
    entries -- at which point the digest must still say WHY, which is exactly
    the "closed on nothing sent" shape the criterion forbids. So a tick that
    considered nobody, or sent to nobody, declines the standing obligation to
    look/send and says so; that fact belongs to the tick, not to any peer row,
    and `declined` stays non-empty on a tick that sent nothing (the promoted
    acceptance test's own exit criterion).
    """
    declined: list[dict[str, Any]] = []
    if not roster:
        declined.append(
            {
                "obligation": "look at the peers this tick",
                "reason": "roster-empty: no candidate peers were produced for this tick",
                "session_id": None,
                "dwell_seconds": None,
            }
        )
    elif not entries:
        declined.append(
            {
                "obligation": "send to any peer this tick",
                "reason": (
                    f"no-eligible-peer: {len(roster)} roster entr"
                    f"{'y' if len(roster) == 1 else 'ies'} considered, every one suppressed"
                ),
                "session_id": None,
                "dwell_seconds": None,
            }
        )
    return declined


def build_send_digest(
    repo_root: str,
    roster: list[dict[str, Any]],
    caller_session_id: str,
    now: Optional[float] = None,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict[str, Any]:
    """One digest per invocation -- the batching discipline itself (AC5).

    The only shape this module emits, and the only route to an entry: no
    per-peer entry point exists, so the firehose is unreachable from this API
    rather than discouraged by it. **Emitting an entry IS the offer and arms
    its cooldown here** -- a throttle left to the actor it throttles is not
    one. A cooldown that could not be written is named in `unrecorded` and its
    entry still stands, so the caller learns the throttle is unarmed.

    Entries carry `gate1`/`gate2` as `None`; both are checked per send, in
    prose. `suppressed` says why each held peer was held, verdict reasons
    ahead of bookkeeping ones -- `away` is never filed under a ledger detail.
    Each entry also carries `dwell_seconds` (module docstring's DWELL TIME
    section). `open_obligations` names every session id this tick observed
    -- emitted or cooldown-held -- that this session offered without ever
    declining (module docstring's OPEN OBLIGATIONS section); it ranks and
    informs, it is never gated on.

    `declined` names what the TICK ITSELF declined and why -- roster-empty or
    no-eligible-peer only (§ `_declinations`, finding #4). A peer's own
    declination is a projection of its `suppressed` row and lives there
    (`obligation`/`dwell_seconds` on each `suppressed` entry) rather than
    round-tripped through a second collection. `declined` is never empty on a
    tick that sent nothing, which is the whole point: a digest that closed on
    nothing sent, with nothing saying why, would be indistinguishable from a
    tick that never looked.

    Known limitation -- no lock spans the log read and the per-entry appends,
    so this assumes one caller at a time per `caller_session_id`. Violate it
    and two calls both read the pre-write log, both see zero cooldown for the
    same peer, and both offer it. Bounded: the log path is caller-scoped, so
    it cannot cross sessions.
    """
    now = time.time() if now is None else now
    log = read_send_log(repo_root, caller_session_id)

    eligible: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []

    for verdict in roster:
        raw_session_id = verdict.get("session_id")
        if not isinstance(raw_session_id, str) or not _safe_session_id(raw_session_id):
            suppressed.append(_suppressed(raw_session_id, "unusable-session-id"))
            continue
        peer_session_id: str = raw_session_id

        reason = verdict.get("reason")
        why = send_suppression_reason(verdict)
        if why is not None:
            suppressed.append(_suppressed(peer_session_id, why, reason))
            continue

        # Corroboration, not a gate. `None` is a producer coverage gap, never
        # evidence the peer owes nothing; gating on it emptied the digest on
        # absence (5 of 5 measured) and shipped the feature inert.
        obligations = undischarged_obligations(repo_root, peer_session_id)

        remaining = _cooldown_remaining(
            log, offer_key(caller_session_id, peer_session_id), now, cooldown_seconds
        )
        if remaining > 0:
            suppressed.append(
                _suppressed(
                    peer_session_id,
                    "cooldown",
                    reason,
                    obligations,
                    remaining,
                    dwell=_dwell_seconds(
                        repo_root, peer_session_id, now, cwd=verdict.get("cwd")
                    ),
                )
            )
            continue

        eligible.append(
            {
                "session_id": peer_session_id,
                "state": verdict.get("state"),
                "reason": reason,
                "source": verdict.get("source"),
                "undischarged_obligations": obligations,
                "trigger": "paused-turn-ended-uncontradicted-by-live-status",
                "gate1": None,
                "gate2": None,
                "cwd": verdict.get("cwd"),
            }
        )

    # Deterministic before the ceiling cuts: most-owed first, then session id.
    # `claude agents --json` order is arbitrary and unstable between ticks, so
    # an unsorted cut makes ceiling survival random between digests.
    eligible.sort(
        key=lambda e: (-(e["undischarged_obligations"] or 0), e["session_id"])
    )

    entries = eligible[:max_entries]
    for entry in eligible[max_entries:]:
        suppressed.append(
            _suppressed(
                entry["session_id"],
                "rate-ceiling",
                entry["reason"],
                entry["undischarged_obligations"],
            )
        )

    # Dwell is attached only to the entries actually emitted (post-ceiling) --
    # see module docstring's DWELL TIME section. A row cut by the ceiling
    # never had its dwell computed at all, not merely discarded.
    for entry in entries:
        entry["dwell_seconds"] = _dwell_seconds(
            repo_root, entry["session_id"], now, cwd=entry.get("cwd")
        )

    unrecorded = [
        entry["session_id"]
        for entry in entries
        if not _record_offer(repo_root, caller_session_id, entry["session_id"], now=now)
    ]

    # OPEN OBLIGATIONS -- see module docstring. Every session id this tick
    # observed with a known, safe id (emitted this tick, or held under
    # cooldown from an earlier one) is checked against the log AS IT STOOD
    # before this tick's own offer writes above: an entry just emitted is
    # open by construction (it cannot yet have a declination), and a
    # cooldown-suppressed peer is open exactly when its last log event is an
    # offer with no later declination -- the belt to C2's suspenders.
    open_obligations = [entry["session_id"] for entry in entries]
    for row in suppressed:
        session_id = row["session_id"]
        if row["why"] != "cooldown" or not _safe_session_id(session_id):
            continue
        key = offer_key(caller_session_id, session_id)
        if _log_key_is_open(log, key):
            open_obligations.append(session_id)

    return {
        "entries": entries,
        "suppressed": suppressed,
        "declined": _declinations(roster, entries),
        "truncated": len(eligible) > max_entries,
        "roster_size": len(roster),
        "eligible_before_ceiling": len(eligible),
        "unrecorded": unrecorded,
        "gate_declaration_required": True,
        "open_obligations": open_obligations,
    }
