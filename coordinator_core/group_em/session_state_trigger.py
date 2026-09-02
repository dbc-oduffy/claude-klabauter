"""coordinator_core.group_em.session_state_trigger — the harness's own session
status, used as a TRIGGER and never as a verdict.

Spec backlink: `state/sizings/2026-09-01-the-harness-already-knows-which-sessions.yaml`
(`dlv-the-harness-already-knows-which-sessions-e74c3f`). Full measured surface
inventory, with costs and citations: `docs/reference/harness-session-state-surface.md`.

PURPOSE. The harness knows which sessions are idle because it IS the process --
`idle` is a value it authors, not a fact it infers. Every signal we can reach
from outside is a proxy. This module reads the one authored value, cheaply
(~5ms in-process for the whole box, no subprocess), and uses it for exactly one
job: deciding which peers are WORTH LOOKING AT this tick. It decides nothing
else, and the negative-spec below is the whole point of the module.

    NEGATIVE SPEC -- four rules, each one a defect if broken.

    1. A TRIGGER MAY ONLY EVER WIDEN. `status` may cause a peer to be
       examined. It may NEVER cause one to be skipped, excluded, marked
       live, marked dead, or nudged. The moment this module removes a peer
       from consideration it is a verdict wearing a trigger's name. Note
       what `unclassified()` is for: peers this module has nothing to say
       about are RETURNED, not dropped, so a caller iterating our output
       still sees the whole fleet.

    2. PRESENCE IS NEVER LIVENESS. There is no self-unlink on session exit.
       Cleanup is a LATER session's startup sweep, and that sweep declines
       ENTIRELY under WSL, Docker, bubblewrap, `IS_SANDBOX` and Windows
       container users -- logging that dead records are left in place,
       neither counted nor deleted. So `absence -> plausibly exited` is
       sound and `presence -> alive` is not. `pid` + `procStart` remains the
       only liveness discriminator, and this module computes no liveness at
       all: `session.liveness` owns that question.

    3. `statusUpdatedAt` IS NOT READ HERE, AT ANY CALL SITE, EVER. Nothing
       re-stamps a registry record, so its age is time-since-last-transition
       and the arithmetic is genuinely tempting: a settle delay falls
       straight out of it with no timer and no state. That convenience is
       exactly how a future reader narrows the 2026-08-14 status ban --
       "it is only a trigger, and the arithmetic is right there." The settle
       in this module is keyed on OUR OWN observation clock instead
       (`SettleLedger`), which costs a state file and is worth it.

    4. THE VERDICT IS NOT OURS. Whether a peer is genuinely parked stays
       with the derived predicate (`read_pass.classify_peer`, plus
       `session.receiver_state`'s tail-type consult). This module adds no
       second classifier. It says "look here first", never "this one is
       stopped".

WHY `busy` IS NOT THE OPPOSITE OF WHAT YOU THINK. `busy` is forced by ANY live
delegated task -- `local_agent`, `remote_agent`, `in_process_teammate`,
`local_workflow` -- with no turn in flight. That is why registry `busy` has been
measured disagreeing with a peer's own receiver-state saying the turn ended: not
staleness, a narrower meaning than "the model is generating". A `busy` peer is
therefore NOT excluded here (rule 1); it is simply not pulled forward.

`waiting` IS THE MOST VALUABLE VALUE ON THE SURFACE, AND YOU WILL MEASURE ZERO
OF IT HERE. It means a human is blocking -- a question, a permission prompt, an
elicitation -- and it is the one state that identifies a peer nobody else can
unblock. On a fleet where every peer runs in bypass permission mode, no dialog
is ever opened, so `waiting` is structurally UNREACHABLE: measured 0 of 40
records on this box, across 16 transitions and 10 distinct sessions. That is a
property of the fleet's configuration, NOT evidence the harness stopped
publishing it -- confirmed live by recording this box's own record at 4Hz across
an `AskUserQuestion`, which produced `busy -> waiting/"input needed" -> busy`
with a 145ms write latency.

    DO NOT DELETE THE `waiting` PATH BECAUSE A CENSUS RETURNED ZERO.

    An absent signal does not name its own cause. A trigger keyed on
    `waiting` is correct and will observe nothing on a bypass fleet; the
    next reader who measures zero hits and removes this code will be
    deleting a working capability on the strength of a configuration.

COST. `snapshot()` is an in-process read of `sessions/*.json` -- no subprocess,
measured 4.6ms for 25 records. `oracle_disagreements()` is the opposite: it
spawns `claude agents --json` (measured 1004-1800ms wall, 344ms CPU) and exists
ONLY so a crown can ask a live fleet whether this instrument is currently lying.
Diagnostic and test path only, never a poll path -- its cost is both why it is
trustworthy and why it cannot be routine.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.session import harness_registry

# The harness's own idle notifier folds `shell` into idle
# (`status === "idle" || status === "shell"`), so nothing downstream should
# treat it as a third state. We match that fold deliberately.
_IDLE_STATES = frozenset({"idle", "shell"})
_WAITING_STATE = "waiting"

# `waitingFor` reasons a human -- and only a human -- can clear.
WAIT_REASONS_HUMAN = frozenset(
    {"input needed", "permission prompt", "dialog open", "goal proposal"}
)
# Reasons that resolve without a human once the request completes.
WAIT_REASONS_MACHINE = frozenset({"sandbox request", "worker request"})

BLOCKED_ON_HUMAN = "blocked-on-human"
PARKED_CANDIDATE = "parked-candidate"
WORKING = "working"
UNCLASSIFIED = "unclassified"

DEFAULT_SETTLE_SECONDS = 120.0


@dataclass(frozen=True)
class TriggerSignal:
    """One peer's trigger classification. Carries no verdict -- see rule 4.

    `trigger` is advice about attention order, not a state of the world.
    `question_shaped` is True only for a peer blocked on a QUESTION
    specifically (`waitingFor == "input needed"`), which is the one wait a
    caller can act on by routing the question elsewhere rather than by
    fetching a human.
    """

    session_id: str
    name: str | None
    cwd: str | None
    trigger: str
    status: str | None
    waiting_for: str | None

    @property
    def question_shaped(self) -> bool:
        return self.waiting_for == "input needed"

    @property
    def needs_a_human(self) -> bool:
        return self.trigger == BLOCKED_ON_HUMAN


@dataclass
class TriggerScan:
    """One tick's classification of the whole visible fleet.

    `registry_readable` distinguishes the two cases a caller must never
    collapse: `True` with an empty `signals` means the registry read fine and
    no peer is present; `False` means we could not read it at all and know
    NOTHING. That is the null-vs-zero distinction, and reporting a confident
    empty fleet from an unreadable registry is the failure it guards.
    """

    signals: list[TriggerSignal] = field(default_factory=list)
    registry_readable: bool = True

    def blocked_on_human(self) -> list[TriggerSignal]:
        return [s for s in self.signals if s.trigger == BLOCKED_ON_HUMAN]

    def questions(self) -> list[TriggerSignal]:
        """Peers blocked on a QUESTION -- the ones whose block is routable.

        A question is the one human-wait a caller has an alternative for: it
        can be put to an adversarial reviewer instead of to the PM. A
        permission prompt cannot -- only the human holds that decision.
        """
        return [s for s in self.signals if s.question_shaped]

    def parked_candidates(self) -> list[TriggerSignal]:
        return [s for s in self.signals if s.trigger == PARKED_CANDIDATE]

    def unclassified(self) -> list[TriggerSignal]:
        """Peers this module has nothing to say about -- returned, not dropped.

        Rule 1 in the module docstring: a trigger widens. A caller iterating
        `signals` still sees every peer the registry knows about, so nothing
        this module fails to recognise falls out of the fleet.
        """
        return [s for s in self.signals if s.trigger == UNCLASSIFIED]


def classify(session_id: str, record) -> TriggerSignal:
    """Classify one registry record. Pure; no I/O, no clock, no verdict."""
    status = record.status
    waiting_for = getattr(record, "waiting_for", None)

    if status == _WAITING_STATE:
        trigger = (
            BLOCKED_ON_HUMAN
            if waiting_for is None or waiting_for not in WAIT_REASONS_MACHINE
            else WORKING
        )
    elif status in _IDLE_STATES:
        trigger = PARKED_CANDIDATE
    elif status == "busy":
        trigger = WORKING
    else:
        trigger = UNCLASSIFIED

    return TriggerSignal(
        session_id=session_id,
        name=record.name,
        cwd=record.cwd,
        trigger=trigger,
        status=status,
        waiting_for=waiting_for,
    )


def scan(cwd_filter: str | None = None) -> TriggerScan:
    """Classify every session the registry knows about.

    `cwd_filter`, when given, keeps only peers whose `cwd` matches it -- a
    per-working-tree view. It filters the REPORT, never the fleet: a caller
    wanting the whole box passes nothing.

    Never raises. `harness_registry.snapshot()` already degrades to `{}` at its
    public boundary, which is indistinguishable from an empty fleet, so this
    function additionally probes `registry_dir()` to tell the two apart and
    reports it on `registry_readable`.
    """
    readable = True
    try:
        directory = harness_registry.registry_dir()
        if directory is None or not Path(directory).is_dir():
            readable = False
    except Exception:
        readable = False

    try:
        snap = harness_registry.snapshot()
    except Exception:
        return TriggerScan(signals=[], registry_readable=False)

    signals = []
    for session_id, record in snap.items():
        if cwd_filter is not None and record.cwd != cwd_filter:
            continue
        signals.append(classify(session_id, record))

    signals.sort(key=lambda s: (s.trigger, s.name or "", s.session_id))
    return TriggerScan(signals=signals, registry_readable=readable)


class SettleLedger:
    """Our own observation clock for the settle -- deliberately NOT `statusUpdatedAt`.

    Rule 3 in the module docstring. The harness stamps every transition and
    never re-stamps it, so `now - statusUpdatedAt` would give a settle delay
    for free, with no state file and no timer. We refuse that on purpose: it
    is the exact convenience a later reader would cite to narrow the standing
    status ban. This ledger costs a small JSON file and owes nothing to a
    banned field.

    The settle gates EMITTING, never LOOKING. Every peer is classified and
    returned by `scan()` regardless; this only decides whether a signal has
    been stable long enough to act on. That distinction is what keeps the
    settle out of rule 1's territory -- it narrows what gets SENT, which is
    what every gate in the send pass already does, not what gets EXAMINED.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._first_seen: dict[str, dict] = {}

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                seen = raw.get("first_seen")
                self._first_seen = seen if isinstance(seen, dict) else {}
        except Exception:
            self._first_seen = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"schema": "settle-ledger/1", "first_seen": self._first_seen}
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def observe(self, signals: list[TriggerSignal], now: float | None = None) -> None:
        """Record when each (peer, trigger, reason) was FIRST seen in this state.

        A peer whose trigger changes restarts its clock -- a session that went
        busy and came back is a new stop, not a continuation of the old one.
        Peers absent this tick are forgotten, so a returning peer is never
        credited with time it spent away.
        """
        now = time.time() if now is None else now
        fresh: dict[str, dict] = {}
        for sig in signals:
            key = sig.session_id
            prior = self._first_seen.get(key)
            if (
                isinstance(prior, dict)
                and prior.get("trigger") == sig.trigger
                and prior.get("waiting_for") == sig.waiting_for
            ):
                fresh[key] = prior
            else:
                fresh[key] = {
                    "trigger": sig.trigger,
                    "waiting_for": sig.waiting_for,
                    "since": now,
                }
        self._first_seen = fresh

    def held_for(self, signal: TriggerSignal, now: float | None = None) -> float | None:
        """Seconds this peer has held its current trigger, or None if unknown.

        `None` means "first sighting" -- not zero. A caller must not treat an
        unknown hold as a fresh one: on the first tick after a restart every
        peer is unknown, and rendering that as "just arrived" would suppress
        the whole fleet for one settle window.
        """
        now = time.time() if now is None else now
        entry = self._first_seen.get(signal.session_id)
        if not isinstance(entry, dict) or "since" not in entry:
            return None
        try:
            return max(0.0, now - float(entry["since"]))
        except (TypeError, ValueError):
            return None

    def settled(
        self,
        signal: TriggerSignal,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        now: float | None = None,
    ) -> bool:
        """Has this signal been stable long enough to act on?

        Unknown holds return False -- we withhold rather than fire on a peer
        we have only just started watching. That is the safe direction: a
        missed emission costs one settle window, a premature one nudges a peer
        that had not actually stopped.
        """
        held = self.held_for(signal, now=now)
        return held is not None and held >= settle_seconds


def oracle_disagreements(timeout: float = 30.0) -> dict:
    """Ask the documented CLI whether this parser is currently lying.

    THIS SPAWNS A SUBPROCESS AND COSTS 1004-1800ms WALL / 344ms CPU. It is a
    diagnostic and test path ONLY -- never a poll path, never on a tick. Its
    expense is the whole reason it is worth having: `claude agents --json` is
    a genuinely SECOND implementation over the same underlying state, not the
    same read wearing a different hat, so a disagreement between it and
    `snapshot()` is real evidence that one of the two has drifted.

    Measured agreeing record-for-record (22 `busy` both ways) on 2026-09-01
    against bundle 2.1.257. The CLI collapses `shell` into `idle`, exactly as
    the harness's own notifier does, so this comparison folds it too.

    Returns a report dict rather than raising; `available: False` means the
    check could not run, which is NOT the same as agreement.
    """

    def _fold(value):
        return "idle" if value in _IDLE_STATES else value

    try:
        proc = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        rows = json.loads(proc.stdout)
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__, "disagreements": []}

    if not isinstance(rows, list):
        return {"available": False, "reason": "unexpected-shape", "disagreements": []}

    cli: dict[str, object] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_session_id = row.get("sessionId")
        if isinstance(row_session_id, str) and row_session_id:
            cli[row_session_id] = row.get("status")
    try:
        ours = harness_registry.snapshot()
    except Exception:
        return {"available": False, "reason": "snapshot-failed", "disagreements": []}

    disagreements = []
    for session_id, cli_status in cli.items():
        record = ours.get(session_id)
        if record is None:
            disagreements.append(
                {"session_id": session_id, "cli": cli_status, "ours": None,
                 "kind": "absent-from-registry"}
            )
        elif _fold(record.status) != _fold(cli_status):
            disagreements.append(
                {"session_id": session_id, "cli": cli_status, "ours": record.status,
                 "kind": "status-mismatch"}
            )
    for session_id in ours:
        if session_id not in cli:
            disagreements.append(
                {"session_id": session_id, "cli": None,
                 "ours": ours[session_id].status, "kind": "absent-from-cli"}
            )

    return {
        "available": True,
        "cli_rows": len(cli),
        "registry_rows": len(ours),
        "disagreements": disagreements,
    }
