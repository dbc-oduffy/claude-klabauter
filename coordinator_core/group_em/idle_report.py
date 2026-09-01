"""The oracle for fleet watching: peers, idle ages, liveness, verdicts, nudge context.

THIS MODULE IS THE ONLY SANCTIONED WAY A WATCHER READS FLEET STATE. A watcher
that opens a transcript by hand has failed, not been diligent: every judgement
the watch makes is encoded here so it cannot drift per wake, cannot be argued
with, and does not need re-teaching after a restart. If the watcher needs a
fact this does not emit, that is a defect in this module -- never a licence to
go and look.

Run it:

    python -m coordinator_core.group_em.idle_report --repo-root <root> --crown-session-id <sid>
    python -m coordinator_core.group_em.idle_report --repo-root <root> --crown-session-id <sid> --peer <sid-or-prefix>

THE CONSUMER OWNS THE OUTPUT SHAPE, and it is written down on their side, in
the DoE-claude sibling repo: `coordinator/docs/wiki/fleet-watch-idle-report-contract.md`,
read by `coordinator/agents/fleet-watch.md`. The verdict vocabulary, the
per-peer field names, the `push` trigger, the `UNADDRESSABLE` disposition and
the summary line are all theirs. Changing any of them here silently changes
their doctrine, so a change goes with a cross-repo memo, not alone.

WHY EACH JUDGEMENT IS SHAPED THE WAY IT IS -- each was learned the hard way,
and each is cheap to undo by accident:

- **The content clock is the MAX `timestamp` across the tail, never the last
  line.** Transcript records are NOT monotonic in timestamp; a late-flushed or
  sidechain record puts an older stamp last, and reading the last line reports
  a moving session as stalled.

- **Records with no `timestamp` are skipped, not counted as unreadable.**
  `last-prompt`, `ai-title`, `mode` and `permission-mode` records legitimately
  carry no stamp. Treating them as a parse failure condemns healthy files.

- **mtime is reported ONLY to expose divergence, and never enters a verdict.**
  A gap between mtime and the content clock means something touched the file
  without appending. An overnight suspend showed a fixed ~708-minute offset; a
  small fixed offset is the same shape forming. NEVER take the minimum across
  the two clocks -- that picks whichever one is corrupted and reports a
  suspended fleet as fully active, the exact failure this instrument exists to
  prevent.

- **The floor and the threshold are applied HERE**, by the script, not
  remembered by an agent between wakes. The measured failure a floor-as-advice
  produced was a genuine escalation fired at 80 seconds. They are printed on
  the summary line so a pasted report explains its own judgements.

- **A stalled clock alone is not a stopped session -- it may be a DEAD one.**
  An exited session's transcript stops growing exactly like a parked session's:
  same content clock, same shape, and nudging it addresses a process that no
  longer exists (this produced two false escalations in one shift before it was
  caught). Liveness comes from `session.harness_registry.snapshot()`, which is
  keyed by session id.

- **`EXITED` HAS A DERIVATION ORDER, and the clocks are not in it.**
    1. An OBSERVED exit transition for that peer, where one is available. The
       harness reporting something it saw beats any inference. It reaches this
       module only through `observed_exits`, because there is no queryable exit
       event anywhere in the engine -- the session-registry Monitor's
       `EXITED <name>` line exists in the crown's context, not in state. A
       caller that saw one passes it; nothing is cached to fake having seen one.
    2. Registry absence, as CORROBORATION, under a box-scoped conjunction: the
       transcript sits under THIS repo's project directory (the only place this
       module globs, so every row satisfies it by construction) AND the session
       is absent from THIS box's registry, read successfully.
    3. The clocks contribute NOTHING. A stalled content clock establishes
       idleness, never death.

  Registry absence is BOX-SCOPED: a peer running on another machine, or one
  whose messaging gate is off, is absent here and looks exactly like a corpse.
  (The same ambiguity is why a baton's `status: claimed` with no registry row is
  explicitly not proof of abandonment.) Anything short of the conjunction is
  `UNKNOWN` with reason `liveness-unresolved` -- and the reason set never gains
  a "probably terminated" member, because a confidence claim in a machine field
  invites the agent to decide how probable, which is the improvisation this
  design removes.

- **`push` is the narrow case and the question is the default.** A session can
  name its next move and still be sitting behind a gate it gave a reason for.
  Pushing a gate is the one harm in this role that does not undo, so `push`
  requires a named move AND no named reason for stopping.

- **`UNKNOWN` reasons are a closed key set, never prose.**
  `liveness-unresolved`, `transcript-unreadable`, `no-records`,
  `clock-unparseable`. Add a key when a genuinely new case turns up; never emit
  a sentence. Prose in a machine field is prose the agent interprets, and
  interpretation is the drift this whole design exists to remove.

- **`EXITED` rows are self-dating.** They carry `EXITED since <iso>`, taken
  from the last record in the transcript, because three corpses re-escalating
  for five consecutive ticks read as a fresh alarm every time -- which is how a
  report trains its reader to skim past the real one. The summary line carries
  the count for the same reason: a terminal state is never invisible.

- **Omission is impossible.** A peer that cannot be classified emits an
  `UNKNOWN` row carrying its reason. A classifier that silently drops rows
  makes a broken instrument and a quiet fleet emit identically. The one
  deliberate exclusion is scope, not classification: transcripts older than
  `STALE_FILE_MINUTES` / `STALE_CONTENT_MINUTES` are not this shift's fleet.

- **Failure must not read as quiet.** Exit 0 means a whole report, `peers=0`
  included. A non-zero exit means NO report -- nothing partial is printed, so
  the watcher says the oracle failed and stops rather than falling back to
  reading transcripts.

NAME RESOLUTION IS HONEST OR ABSENT, AND THE JOIN EXISTS. `harness_registry`
resolves a session id to the exact string `SendMessage` accepts -- `lookup(sid)`
asks it directly, and `snapshot()` answers the same question for every peer at
once, which is what this module uses because `lookup` is defined over that one
directory scan anyway (one scan for the whole roster, never one per row). The
consumer's contract says no join exists because a `RegistryRecord` carries no
session id: true of the record, false of the store, which is KEYED by it.

`snapshot()`/`lookup()` RESOLVE LIVE SESSIONS ONLY, and that is load-bearing.
A dead session is absent -- the registry has already forgotten it -- so absence
is an ANSWER ("not live on this box"), not a failure to resolve, and it is
exactly the corroborating leg the EXITED conjunction wants. It also means a dead
peer's name is not recoverable this way, which costs nothing: you only need an
address for a peer you might message, and you never message a corpse. Do not
extend this resolver to anything reading HISTORICAL records -- a stored claim
outlives the session that wrote it, so read-time resolution there misses exactly
the sessions that block people (memo 13354c9a9a).

Where the registry has no entry, the only fallback is a self-identification some
peer wrote into this peer's own transcript tail --
`claude-klabauter-a9 [3d18b2c0]` -- read off the tail this module already holds,
never a scan across the transcript corpus. A name is NEVER inferred from the
session-id prefix: that mapping is coincidence and is falsified on this very box
(`claude-klabauter-ad` runs on session `2374d3d0`). No name means
`UNADDRESSABLE`, and on an escalation that forces `nudge-shape: hold` plus
`report-to-crown: true` -- the crown reaches the peer, the watcher does not go
hunting for the name.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import time
from typing import Optional

from coordinator_core.group_em import repo_root_arg
from coordinator_core.ops.discover_working_repos import encode_projects_dir_name

#: Below this, a quiet session is simply between turns. Applied here, never remembered.
FLOOR_MINUTES = 5.0
#: At or above this, a quiet session is a candidate for a nudge, an exit, or an assignment.
THRESHOLD_MINUTES = 30.0
#: mtime/content gap wider than this is called out. Reported, never acted on.
DIVERGENCE_MINUTES = 5.0
#: Bounded tail read: enough for the last several turns, never the whole file.
TAIL_BYTES = 400_000
#: Hard cap on `last-said`, applied at the EMITTING end. An uncapped field puts
#: the token cost straight back into the agent's context, which is the whole
#: thing this instrument removes.
LAST_SAID_CHARS = 300
#: Files not touched within this are not this shift's fleet at all.
STALE_FILE_MINUTES = 1440.0
#: Content older than this is a finished session, not a stalled one.
STALE_CONTENT_MINUTES = 180.0

#: The clock. An unescaped `"timestamp"` key, so a JSON blob quoted inside a
#: message body cannot inject one.
_TIMESTAMP = re.compile(r'(?<!\\)"timestamp"\s*:\s*"([^"\\]+)"')

#: How many lines at the end of the tail are scanned for assistant prose. Deep
#: enough for the last few turns of a tool-heavy session, bounded because the
#: whole point is not to parse the tail.
_ASSISTANT_SCAN_LINES = 400

#: A session's own name as some peer wrote it: "claude-klabauter-a9 [3d18b2c0]".
_SELF_ID = re.compile(r"(claude-klabauter-[0-9a-z]{2})\s*\[([0-9a-f]{6,8})\]", re.IGNORECASE)

#: A session naming its own next move -- what a `push` names back at it.
_NEXT_MOVE = re.compile(
    r"(next (?:is|step|up|I)|I'?ll (?:now|next|run|dispatch|start)|about to|"
    r"remains? to|still (?:to|need)|then I)",
    re.IGNORECASE,
)

#: A session naming a REASON it stopped. A gate is a considered refusal with a
#: reason; hesitation is the absence of one. These phrases are the difference
#: between `push` and `hold`, so they live here and not in a prompt.
_NAMED_REASON = re.compile(
    r"(waiting (?:for|on)|blocked (?:by|on)|gated (?:by|on)|awaiting|"
    r"cannot proceed|can'?t proceed|until (?:the )?(?:PM|you|approval|a ruling)|"
    r"needs? (?:your|the PM'?s|PM |approval|a decision|a ruling)|"
    r"pending (?:your|the PM|approval|a decision)|handing (?:this )?(?:back|up))",
    re.IGNORECASE,
)

#: The two ceremonies that mean "this session finished its work", as they
#: appear in a transcript: the slash-command echo and the per-record skill
#: attribution the harness stamps. Matched on those structured spellings and
#: not on bare prose, so a session merely TALKING about workstream-complete
#: (as this file's own author session did) is not classified as out of work.
_COMPLETION_SKILLS = ("workstream-complete", "quick-wrap")
_COMPLETION_ATTRIBUTION = re.compile(
    r'"attributionSkill"\s*:\s*"[^"]*(?:%s)"' % "|".join(_COMPLETION_SKILLS)
)
_COMPLETION_COMMAND = re.compile(
    r"<command-name>[^<]*(?:%s)</command-name>" % "|".join(_COMPLETION_SKILLS)
)
#: How many lines at the end of the tail count as "what this session is doing
#: now". A completion ceremony further back than this is history: the session
#: wrapped, then took new work.
_COMPLETION_WINDOW = 40

VERDICT_BETWEEN_TURNS = "between-turns"
VERDICT_WATCH = "watch"
VERDICT_ESCALATE = "ESCALATE"
VERDICT_OUT_OF_WORK = "OUT-OF-WORK"
VERDICT_CROWN_MOVED = "CROWN-MOVED"
VERDICT_EXITED = "EXITED"
VERDICT_UNKNOWN = "UNKNOWN"

#: The CLOSED key set for `UNKNOWN` rows. Keys, never sentences: prose in a
#: machine field is prose the agent interprets, and interpretation is the drift
#: this design removes. Add a key when a genuinely new case turns up.
REASON_LIVENESS_UNRESOLVED = "liveness-unresolved"
REASON_TRANSCRIPT_UNREADABLE = "transcript-unreadable"
REASON_NO_RECORDS = "no-records"
REASON_CLOCK_UNPARSEABLE = "clock-unparseable"
#: The two DOWNGRADE keys. A MISSING ENRICHMENT DOWNGRADES TOWARD REPORTING,
#: NEVER TOWARD SENDING -- the rule that makes a partial build safe. Neither of
#: these omissions degrades into "the agent knows less"; both degrade into "the
#: agent does the wrong thing, confidently". Without out-of-work detection those
#: peers arrive as ESCALATE and the watcher nudges a session that has genuinely
#: run out, which no nudge fixes and only the crown can answer. Without the
#: crown's offer log, suppression vanishes and the watcher re-nudges peers the
#: crown answered an hour ago -- the exact thing the two-session-id split
#: exists to prevent. So the affected peers become `UNKNOWN` instead, which
#: routes to REPORT IT: the correct action under partial information, and
#: omission stays impossible.
REASON_OUT_OF_WORK_UNDETECTED = "out-of-work-undetected"
REASON_SUPPRESSION_UNAVAILABLE = "suppression-unavailable"
UNKNOWN_REASONS = frozenset({
    REASON_LIVENESS_UNRESOLVED,
    REASON_TRANSCRIPT_UNREADABLE,
    REASON_NO_RECORDS,
    REASON_CLOCK_UNPARSEABLE,
    REASON_OUT_OF_WORK_UNDETECTED,
    REASON_SUPPRESSION_UNAVAILABLE,
})

#: Divergence vocabulary. `fixed` (offset constant while both ages climb) means
#: the transcript stopped being written by its own session; `growing` means
#: something other than the session is still writing to it. Telling them apart
#: needs TWO observations and one invocation has one, so a single run emits
#: `unknown` with the minutes rather than guessing -- and no persistence file is
#: invented to manufacture a second sample.
#:
#: HARD CONSTRAINT -- THIS FIELD IS PURELY DESCRIPTIVE. It never becomes a
#: verdict, never feeds the EXITED derivation, and no consumer keys an action on
#: it. The tempting reading -- that a fixed offset corroborates termination --
#: was tested and REFUTED: of three confirmed corpses one showed 0.0m, one 1.4m
#: and one 7.4m, while live peers showed 0.0-0.5m. Divergence is neither
#: necessary nor sufficient for an exit. And the report never takes the minimum
#: across the two clocks, which would report a suspended fleet as active.
DIVERGENCE_NONE = "none"
DIVERGENCE_UNKNOWN = "unknown"
DIVERGENCE_FIXED = "fixed"
DIVERGENCE_GROWING = "growing"

SHAPE_PUSH = "push"
SHAPE_ASK = "ask-which-it-is"
SHAPE_ASSIGN = "assign"
SHAPE_HOLD = "hold"
#: CLOSED, exactly like the verdict set and for a sharper reason: a verdict the
#: watcher cannot place makes it report wrongly, but a shape it cannot place
#: makes it SEND wrongly, which is the highest-stakes improvisation available to
#: it. Nothing is added here without the consumer agreeing first.
NUDGE_SHAPES = frozenset({SHAPE_PUSH, SHAPE_ASK, SHAPE_ASSIGN, SHAPE_HOLD})

UNADDRESSABLE = "UNADDRESSABLE"


def projects_dir_for(repo_root: str, home: Optional[str] = None) -> str:
    """The `~/.claude/projects/<encoded-root>/` directory holding this repo's transcripts.

    Derived from `repo_root`, never hardcoded -- the same oracle has to answer
    for a sibling repo, and a hardcoded directory is a watcher that silently
    reports the wrong fleet. The encoding is
    `discover_working_repos.encode_projects_dir_name`, the forward direction of
    the decode that names those directories, deliberately imported rather than
    re-spelled: a second slugifier that drifts resolves to a directory that does
    not exist, which reads downstream as an empty fleet rather than an error.
    """
    base = home if home is not None else os.path.expanduser("~")
    return os.path.join(base, ".claude", "projects", encode_projects_dir_name(repo_root))


def _tail_text(path: str) -> tuple:
    """`(raw_text, readable)` for the last `TAIL_BYTES` of a transcript.

    ONE decode, and no JSON parse of the whole tail. Every question this module
    asks of a transcript -- the clock, the self-id, the completion ceremony --
    is answered by a pattern over this string; only the handful of lines that
    carry assistant prose are parsed as JSON. Parsing all ~15 tails in full was
    most of this module's process time, and it runs on a two-minute poll on a
    box ~50 peers share, so the cost is paid by all of them.

    `readable` is False only when the file itself could not be read -- that is
    `transcript-unreadable`, a different `UNKNOWN` reason from a file that read
    fine and held nothing (`no-records`). Collapsing the two loses the only
    signal separating an IO fault from an empty session.

    The first line of a mid-file seek is normally a fragment; nothing here
    depends on it being whole.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return "", False
    return raw.decode("utf-8", "replace"), True


def newest_timestamp(raw_text: str) -> Optional[float]:
    """The MAX `timestamp` in the tail as an epoch, or None if none is present.

    NOT the last one. Records are not monotonic in timestamp, so the last line
    is routinely not the latest moment in the file, and reading it reports a
    moving session as stalled. Records that carry no `timestamp` at all
    (`last-prompt`, `ai-title`, `mode`, `permission-mode`) simply contribute no
    match -- they are legitimately unstamped, not unreadable, and counting them
    as failures condemns healthy files.

    The pattern requires an UNESCAPED quote before the key, so a transcript
    quoting a JSON blob inside a message body cannot inject a timestamp.
    """
    newest = None
    for stamp in _TIMESTAMP.findall(raw_text):
        try:
            value = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if newest is None or value > newest:
            newest = value
    return newest


def _assistant_text(raw_text: str) -> list:
    """Everything the session actually SAID in the tail, oldest first.

    Only lines that look like assistant records are parsed -- the tail is mostly
    tool traffic, and paying a JSON parse for all of it to reach a handful of
    prose blocks is the cost this bounded scan removes.
    """
    said = []
    for line in raw_text.split("\n")[-_ASSISTANT_SCAN_LINES:]:
        if '"assistant"' not in line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        text = " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if text:
            said.append(" ".join(text.split()))
    return said


def _name_from_transcript(raw_text: str, session_id: str) -> Optional[str]:
    """A name only if the transcript states it beside THIS session's id.

    The fallback for sessions the registry no longer lists. The id in the match
    must prefix `session_id`: a transcript quoting some OTHER peer's name and id
    must not name this one, and a name is never inferred from the session-id
    prefix -- that mapping is coincidence and is falsified in the field.
    """
    for name, ref in _SELF_ID.findall(raw_text):
        if session_id.startswith(ref.lower()):
            return name.lower()
    return None


def _is_out_of_work(raw_text: str) -> bool:
    """Did this session just finish a completion ceremony?

    Structured spellings only (`attributionSkill`, `<command-name>`), within the
    last `_COMPLETION_WINDOW` lines. Bare prose is not evidence: sessions discuss
    these skills constantly, including the one that wrote this file. The window
    is what keeps this "now" rather than "ever" -- a session that wrapped and
    then took new work is not out of work.
    """
    tail = "\n".join(raw_text.split("\n")[-_COMPLETION_WINDOW:])
    return bool(_COMPLETION_ATTRIBUTION.search(tail) or _COMPLETION_COMMAND.search(tail))


def registry_names() -> Optional[dict]:
    """`{session_id: name}` for every live session, or None if unreadable.

    None and `{}` are NOT the same answer and must never be collapsed. None
    means liveness could not be established, and no peer may be called `EXITED`
    on it. `snapshot()` degrades to `{}` on internal failure by its own
    documented contract, so an empty result is treated as "could not establish"
    here too -- the safe direction, since an empty registry alongside live
    transcripts is not a credible reading.
    """
    try:
        from coordinator_core.session import harness_registry
        snapshot = harness_registry.snapshot()
    except Exception:
        return None
    if not snapshot:
        return None
    return {sid: getattr(record, "name", None) for sid, record in snapshot.items()}


def crown_moved(repo_root: str, crown_session_id: Optional[str]) -> bool:
    """Has the crown moved off the session this report was armed for?

    The watcher watches on the crown's standing, so a crown that has moved
    invalidates the whole tick -- which is why this lives in the oracle rather
    than as a step the agent remembers to run first. Crown-holding is
    established by the nomination record (`group_em.nomination.read_record`),
    the same record `group-em-enter` claims and the same one displacement is
    reported against; nothing here re-derives that from a roster.

    POSITIVE EVIDENCE ONLY. True requires a readable record naming a DIFFERENT
    session. No record, an unreadable one, or no `--crown-session-id` given all
    answer False: absence of a record is not evidence the crown moved, and
    stopping every tick on a missing file would be the same false-tidy failure
    as reporting a live peer dead.
    """
    if not crown_session_id:
        return False
    try:
        from coordinator_core.group_em import nomination
        record = nomination.read_record(repo_root)
    except Exception:
        return False
    holder = (record or {}).get("session_id")
    return bool(holder) and holder != crown_session_id


def _read_crown_log(repo_root: str, crown_session_id: Optional[str]) -> tuple:
    """`(log, available)` -- the crown's offer log, read ONCE per report.

    Per-peer reads meant one file open per peer for a file whose contents do not
    change mid-report: pure process time on a box the whole fleet shares.

    `available` is False only when the read itself failed. It is NOT False for
    an empty log, which is a real answer ("this crown has offered nobody"). A
    failed read means suppression cannot be established, and the peers that
    would have been nudged are downgraded to `UNKNOWN` rather than nudged
    again -- toward reporting, never toward sending.
    """
    if not crown_session_id:
        return [], True
    try:
        from coordinator_core.group_em import send_pass
        return send_pass.read_send_log(repo_root, crown_session_id), True
    except Exception:
        return [], False


def _crown_answer(log: list, crown_session_id: Optional[str], peer_session_id: str,
                  now: float) -> tuple:
    """`(answered_by_crown, within_cooldown)` from the crown's own offer log.

    Suppression rides the report so the watcher never has to remember who it
    nudged across a wake. Reads the SAME log and SAME key `send_pass` arms on
    every offer -- never a second mechanism and never an operator-maintained
    mute list. A crown we were not given cannot have answered anybody.
    """
    if not crown_session_id:
        return None, False
    try:
        from coordinator_core.group_em import send_pass
        key = send_pass.offer_key(crown_session_id, peer_session_id)
        within = send_pass._cooldown_remaining(
            log, key, now, send_pass.DEFAULT_COOLDOWN_SECONDS
        ) > 0
        stamps = [
            record.get("offered_at") for record in log
            if record.get("offer_key") == key and isinstance(record.get("offered_at"), (int, float))
        ]
    except Exception:
        return None, False
    if not stamps:
        return None, within
    latest = datetime.datetime.fromtimestamp(max(stamps), datetime.timezone.utc)
    return latest.strftime("%Y-%m-%dT%H:%M:%SZ"), within


def _verdict(age_minutes: Optional[float], in_registry: Optional[bool],
             out_of_work: bool, clock_reason: Optional[str] = None,
             observed_exit: bool = False) -> tuple:
    """`(verdict, reason)` from the content clock, registry presence and ceremony.

    `observed_exit` is the PRIMARY leg and short-circuits everything: the
    harness saw the session end, which beats any inference this module can make.
    `in_registry` is None when the registry could not be read; `age_minutes` is
    None when no record in the tail carried a timestamp. Liveness only changes
    the answer at or above the threshold: below it, a peer the registry has not
    caught up with is simply between turns, and calling it dead on a fresh clock
    would be absurd. At or above it, a peer the registry does not list has
    exited -- the second half of the conjunction, the first being that the
    transcript lives under this repo's project directory at all. A peer whose
    liveness could not be established is `UNKNOWN` with reason
    `liveness-unresolved`, never `EXITED`. Reasons are keys from
    `UNKNOWN_REASONS`, never sentences, and only `UNKNOWN` carries one.
    """
    if observed_exit:
        return VERDICT_EXITED, None
    if age_minutes is None:
        return VERDICT_UNKNOWN, clock_reason or REASON_CLOCK_UNPARSEABLE
    if age_minutes < FLOOR_MINUTES:
        return VERDICT_BETWEEN_TURNS, None
    if out_of_work:
        return VERDICT_OUT_OF_WORK, None
    if age_minutes < THRESHOLD_MINUTES:
        return VERDICT_WATCH, None
    if in_registry is None:
        return VERDICT_UNKNOWN, REASON_LIVENESS_UNRESOLVED
    if not in_registry:
        return VERDICT_EXITED, None
    return VERDICT_ESCALATE, None


def _nudge_shape(verdict: str, addressable: bool, within_cooldown: bool,
                 named_move: Optional[str], named_reason: bool) -> str:
    """Which of the two nudge sentences the watcher sends, or neither.

    `push` is the narrow case: an affirmatively named next move AND no named
    reason for stopping. A gate is a considered refusal with a reason;
    hesitation is the absence of one, and pushing a gate is the one harm in
    this role that does not undo -- so absent either condition the shape is the
    question. `hold` covers suppression, a gate-shaped stop, and an escalation
    with no address; `assign` belongs to `OUT-OF-WORK` alone and is addressed
    to the crown, not the peer.
    """
    if verdict == VERDICT_OUT_OF_WORK:
        return SHAPE_ASSIGN
    if verdict != VERDICT_ESCALATE:
        return SHAPE_HOLD
    if within_cooldown or not addressable or named_reason:
        return SHAPE_HOLD
    return SHAPE_PUSH if named_move else SHAPE_ASK


def _last_record_iso(raw_text: str) -> Optional[str]:
    """The newest record's timestamp as an ISO stamp -- what an EXITED row dates itself by."""
    newest = newest_timestamp(raw_text)
    if newest is None:
        return None
    return datetime.datetime.fromtimestamp(
        newest, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _divergence(age_minutes: Optional[float], mtime_age_minutes: float) -> tuple:
    """`(label, minutes)` for the gap between the two clocks.

    `fixed` vs `growing` is the discriminator that matters -- a fixed offset
    with both ages climbing means the transcript stopped being written by its
    own session -- but telling them apart needs two observations and one
    invocation has one. So a gap past tolerance is labelled `unknown` and
    carries its minutes; the label is never guessed from a single sample, and no
    persistence file is invented to manufacture a second one.

    Descriptive only: nothing here changes a verdict or feeds the EXITED
    derivation (the fixed-offset-means-dead reading was tested and refuted), and
    the minimum across the two clocks is never taken.
    """
    if age_minutes is None:
        return DIVERGENCE_NONE, None
    gap = abs(mtime_age_minutes - age_minutes)
    if gap <= DIVERGENCE_MINUTES:
        return DIVERGENCE_NONE, round(gap, 1)
    return DIVERGENCE_UNKNOWN, round(gap, 1)


def _peer_row(path: str, session_id: str, now: float, names: Optional[dict],
              crown_log: list, crown_session_id: Optional[str],
              observed_exits: frozenset = frozenset(),
              suppression_available: bool = True) -> Optional[dict]:
    """One roster row, or None when the transcript is out of scope entirely.

    None means SCOPE (too old to be this shift's fleet), never a failed
    classification -- a peer this module cannot classify gets an `UNKNOWN` row
    with a reason, because a classifier that drops rows makes a broken
    instrument and a quiet fleet emit identically.
    """
    try:
        mtime_age = (now - os.path.getmtime(path)) / 60
    except OSError:
        return None
    if mtime_age > STALE_FILE_MINUTES:
        return None

    raw_text, readable = _tail_text(path)
    newest = newest_timestamp(raw_text)
    age = None if newest is None else (now - newest) / 60
    if age is not None and age > STALE_CONTENT_MINUTES:
        return None
    clock_reason = None
    if age is None:
        clock_reason = (
            REASON_TRANSCRIPT_UNREADABLE if not readable
            else REASON_NO_RECORDS if not raw_text.strip()
            else REASON_CLOCK_UNPARSEABLE
        )

    # The EXITED conjunction: this transcript sits under THIS repo's project
    # directory (true of every path globbed here) AND the session is absent from
    # THIS box's registry, read successfully. `None` is "could not establish".
    in_registry = (session_id in names) if names is not None else None
    name = (names or {}).get(session_id) or _name_from_transcript(raw_text, session_id)
    out_of_work = age is not None and age >= FLOOR_MINUTES and _is_out_of_work(raw_text)
    observed_exit = bool(
        observed_exits and (session_id in observed_exits or (name and name in observed_exits))
    )
    verdict, reason = _verdict(age, in_registry, out_of_work, clock_reason, observed_exit)

    said = _assistant_text(raw_text)
    last_said = said[-1][:LAST_SAID_CHARS] if said else None
    named_move = next((line for line in reversed(said) if _NEXT_MOVE.search(line)), None)
    named_reason = any(_NAMED_REASON.search(line) for line in said[-3:])
    answered, within_cooldown = _crown_answer(crown_log, crown_session_id, session_id, now)
    if verdict == VERDICT_ESCALATE and not suppression_available:
        # Downgrade toward REPORTING. Nudging here would re-nudge whoever the
        # crown already answered, which is the failure the offer log prevents.
        verdict, reason = VERDICT_UNKNOWN, REASON_SUPPRESSION_UNAVAILABLE
    shape = _nudge_shape(verdict, bool(name), within_cooldown, named_move, named_reason)
    divergence, divergence_minutes = _divergence(age, mtime_age)
    exited = verdict == VERDICT_EXITED

    return {
        "session": session_id,
        "verdict": verdict,
        "reason": reason,
        "content-age": None if age is None else round(age, 1),
        "mtime-age": round(mtime_age, 1),
        "divergence": divergence,
        "divergence-minutes": divergence_minutes,
        # A corpse that re-escalates every tick reads as a fresh alarm and
        # trains its reader to skim past the real one. Dating the row fixes that.
        "exited-since": _last_record_iso(raw_text) if exited else None,
        "answered-by-crown": answered or "no",
        "nudge-shape": shape,
        "address": ("%s [%s]" % (name, session_id[:8])) if name else UNADDRESSABLE,
        # A dead session is never nudged, so it never carries nudge content.
        "last-said": None if exited else last_said,
        "named-next-move": (
            None if exited or not named_move else named_move[:LAST_SAID_CHARS]
        ),
        # The escalation most worth getting right is the one that comes back
        # unreachable: the verdict stands, the shape holds, and the crown --
        # who holds ListAgents -- is the one who reaches it.
        "report-to-crown": verdict in (VERDICT_ESCALATE, VERDICT_OUT_OF_WORK) and not name,
    }


def build_report(
    repo_root: str,
    crown_session_id: Optional[str] = None,
    caller_session_id: Optional[str] = None,
    peer: Optional[str] = None,
    now: Optional[float] = None,
    projects_dir: Optional[str] = None,
    names: Optional[dict] = None,
    registry_read: bool = True,
    observed_exits: Optional[frozenset] = None,
) -> dict:
    """The whole answer, as data. The human and `--json` arms both render this.

    `crown_session_id` is the Group EM's session and `caller_session_id` the
    process running the poll -- the same id only when the crown polls itself,
    the same two-id split `group_em.watch` carries. Both are excluded from the
    roster: reporting the crown to the crown is noise by construction, and the
    poller flagging itself is worse. It is the crown's offer log, not the
    caller's, that decides a peer has already been answered.

    `observed_exits` carries exit transitions the CALLER actually saw (session
    ids or names) -- the primary leg of the EXITED derivation. It is a parameter
    and not a lookup because no queryable exit event exists in the engine: the
    Monitor's `EXITED <name>` line lands in the crown's context, not in state.
    Nothing is persisted to simulate having seen one.

    `names` / `registry_read` are the injection seam for tests; production
    passes neither and the registry is read here.
    """
    now = time.time() if now is None else now
    directory = projects_dir or projects_dir_for(repo_root)
    if names is None and registry_read:
        names = registry_names()

    moved = crown_moved(repo_root, crown_session_id)
    observed_exits = frozenset(observed_exits or ())
    excluded = {sid.lower() for sid in (crown_session_id, caller_session_id) if sid}
    crown_log, suppression_available = _read_crown_log(repo_root, crown_session_id)
    rows = []
    # CROWN-MOVED short-circuits the roster entirely. The rows would describe a
    # fleet this watcher no longer has standing over, and a row that is present
    # is a row something acts on. Still exit 0: a void tick, stated, is a whole
    # report.
    for path in ([] if moved else sorted(glob.glob(os.path.join(directory, "*.jsonl")))):
        session_id = os.path.basename(path)[:-6]
        if any(session_id.lower().startswith(sid) for sid in excluded):
            continue
        if peer and not session_id.startswith(peer):
            continue
        row = _peer_row(path, session_id, now, names, crown_log, crown_session_id,
                        observed_exits, suppression_available)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda row: -(row["content-age"] if row["content-age"] is not None else 1e9))

    def count(verdict):
        return sum(1 for row in rows if row["verdict"] == verdict)

    return {
        "repo-root": repo_root,
        "projects-dir": directory,
        "crown-session-id": crown_session_id,
        "registry-available": names is not None,
        "floor-minutes": FLOOR_MINUTES,
        "threshold-minutes": THRESHOLD_MINUTES,
        # CROWN-MOVED is the REPORT'S state, not a peer's: it says this whole
        # tick is void because the standing the watcher watches on is gone. It
        # is a verdict in the consumer's closed table, carried here as the
        # top-level `verdict` so the watcher never has to look for it in rows.
        "verdict": VERDICT_CROWN_MOVED if moved else None,
        "crown-moved": moved,
        "peers": rows,
        "counts": {
            "peers": len(rows),
            "escalate": count(VERDICT_ESCALATE),
            "out-of-work": count(VERDICT_OUT_OF_WORK),
            "unknown": count(VERDICT_UNKNOWN),
            "exited": count(VERDICT_EXITED),
        },
    }


def summary_line(report: dict) -> str:
    """The last line, and the only one the watcher must read to know it has a
    whole report. The thresholds ride on it so a report pasted into the crown's
    context explains its own judgements without a second lookup.

    `exited=` is an additive extension to the consumer's spelled contract, for
    the verdict their doc predates. Nothing else on this line moves without a
    cross-repo memo.
    """
    counts = report["counts"]
    return (
        "peers=%d escalate=%d out-of-work=%d unknown=%d exited=%d "
        "floor=%.0fm threshold=%.0fm crown=%s%s" % (
            counts["peers"], counts["escalate"], counts["out-of-work"],
            counts["unknown"], counts["exited"],
            report["floor-minutes"], report["threshold-minutes"],
            report["crown-session-id"] or "unset",
            " CROWN-MOVED" if report["crown-moved"] else "",
        )
    )


def _render_row(row: dict) -> list:
    divergence = row["divergence"]
    if divergence != DIVERGENCE_NONE and row["divergence-minutes"] is not None:
        divergence = "%s(%.0fm)" % (divergence, row["divergence-minutes"])
    lines = ["  %-8s %-13s content=%s mtime=%s divergence=%s" % (
        row["session"][:8], row["verdict"],
        "n/a" if row["content-age"] is None else "%.1fm" % row["content-age"],
        "n/a" if row["mtime-age"] is None else "%.1fm" % row["mtime-age"],
        divergence,
    )]
    if row["reason"]:
        lines.append("      reason: %s" % row["reason"])
    if row["exited-since"]:
        lines.append("      EXITED since %s" % row["exited-since"])
    if row["verdict"] in (VERDICT_BETWEEN_TURNS, VERDICT_WATCH):
        return lines
    lines.append("      address: %s" % row["address"])
    lines.append("      answered-by-crown: %s" % row["answered-by-crown"])
    lines.append("      nudge-shape: %s" % row["nudge-shape"])
    if row["report-to-crown"]:
        lines.append("      report-to-crown: true")
    if row["verdict"] != VERDICT_EXITED:
        lines.append("      last-said: %s" % (row["last-said"] or "none"))
        lines.append("      named-next-move: %s" % (row["named-next-move"] or "none"))
    return lines


def render(report: dict, peer: Optional[str] = None) -> str:
    """The agent-facing rendering. Same facts as `--json`, no extra judgement.

    `between-turns` peers are counted but not printed per-peer: the contract
    says the watcher does nothing with them, and printing them is pure context
    cost.
    """
    lines = []
    if report["crown-moved"]:
        lines.append(
            "%s: %s no longer holds the crown for %s. This tick is void -- stop and "
            "tell the Group EM." % (VERDICT_CROWN_MOVED, report["crown-session-id"],
                                    report["repo-root"]))
    if peer and not report["peers"]:
        lines.append("no live transcript for %s" % peer)
    for row in report["peers"]:
        if row["verdict"] == VERDICT_BETWEEN_TURNS and not peer:
            continue
        lines.extend(_render_row(row))
    lines.append(summary_line(report))
    return "\n".join(lines)


def _cli(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m coordinator_core.group_em.idle_report",
        description="The fleet-watch oracle: every peer, its idle age, its liveness, its "
                    "verdict, and the nudge context for any peer that has stopped.",
    )
    parser.add_argument(
        "--repo-root", required=True,
        help="Repository root whose fleet to report. Taken as an argument, never derived "
             "from cwd -- this runs under a harness tool whose working directory is not ours.")
    parser.add_argument(
        "--crown-session-id", default=None,
        help="The Group EM's session id -- never the watcher's. Excluded from its own "
             "roster, and the owner of the offer log that decides a peer is already "
             "answered.")
    parser.add_argument(
        "--caller-session-id", default=None,
        help="Session running this poll, when a teammate holds the watch instead of the "
             "crown. Also excluded from the roster.")
    parser.add_argument(
        "--peer", default=None,
        help="One session id (any prefix), for re-running a peer after a nudge to see "
             "whether the state moved.")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the same facts as a machine-readable object, for the Monitor and tests.")
    args = parser.parse_args(argv)

    # Same refusal as `watch._cli`, for the same reason and the same shell: this
    # oracle is run by the same agent, with the same `--repo-root` spelling, and
    # a report over a root that does not exist reads as a quiet fleet.
    try:
        args.repo_root = repo_root_arg.resolve_repo_root_arg(args.repo_root)
    except repo_root_arg.RepoRootArgError as exc:
        import sys as _sys

        print(f"group-em-idle-report: {exc}", file=_sys.stderr)
        return 2

    # Nothing is printed until the whole report exists: a partial report on a
    # failed run is indistinguishable from a quiet fleet, which is the one
    # reading this instrument must never permit.
    try:
        report = build_report(
            args.repo_root,
            crown_session_id=args.crown_session_id,
            caller_session_id=args.caller_session_id,
            peer=args.peer,
        )
        rendered = json.dumps(report, indent=2, sort_keys=True) if args.json \
            else render(report, peer=args.peer)
    except Exception as exc:  # pragma: no cover - defensive, exercised by the exit contract
        import sys
        print("idle_report failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    print(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via _cli in tests
    raise SystemExit(_cli())
