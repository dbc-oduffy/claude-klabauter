"""
coordinator_core.session.messaging_gate — did THIS session ask the harness to
open its cross-session messaging inbox, and did the inbox open?

Purpose: `coordinator_core.session.reachability.messaging_available()` answers
a BOX-level deliverability question — "does any live record carry a
`messaging_socket_path`". It returns `False` both when nothing on the box ever
asked for the gate and when something asked and the gate stayed shut, and those
two readings are byte-identical to a reader. That collapse has a measured cost:
`coordinator/bin/claude-doe.py` has defaulted `CLAUDE_CODE_HARBOR_KITE` on for
every session it launches since `c3db5d8b1` (2026-08-15T00:30Z), 45 sessions
started after it and 0 bound an inbox, and three repos independently measured
the resulting `messaging_available: false` (claude-klabauter 44/44 2026-08-14,
Example-cockpit-repo 49/49 and claude-klabauter 50/50 2026-08-15) and all three read it as
"the remote GrowthBook flag is still off". A peer EM planned two blocked plan
chunks around a human relay on that reading. Same shape as
`state/lessons/2026-08-15-a-hook-silent-on-the-happy-path-is-invis-a2013001e71f.yaml`:
a mechanism that sets a switch and never checks whether the switch opened emits
no signal on either path.

Spec backlink:
`state/bug-backlog/2026-08-15-the-messaging-gate-default-ships-and-no-session-binds.yaml`
(§ proposed_action, "close the visibility hole as well as the defect");
`cross-repo/inbox/2026-08-15-example-cockpit-repo-em-peer-messaging-gate-still-off-and-an-odd-failed-bind.md`
§ "EM Response" ¶ 3.

Four states, from ONE read of the calling process's environment:

    "not-requested"     — `CLAUDE_CODE_HARBOR_KITE` is absent. Nothing in this
                          session's launch chain asked for the gate; the
                          harness's own gate predicate never short-circuited
                          and fell through to the platform/GrowthBook checks.
    "declined"          — the variable is present and EMPTY. The one value that
                          defeats both `os.environ.setdefault(...)` in the
                          launcher and the harness predicate (see below): a
                          deliberate opt-out, honoured, not a defect.
    "requested-unbound" — the variable is present and non-empty, and this
                          session has no bound inbox. WE ASKED AND IT DID NOT
                          OPEN. The actionable state, and the one that had no
                          voice before this module.
    "open"              — this session's inbox is bound.

Truthiness mirrors the HARNESS predicate, not Python's. The gate short-circuits
on JS string truthiness (`if (q.CLAUDE_CODE_HARBOR_KITE) return !0;` — Claude
Code 2.1.233, verified on-disk; grep the literal `tengu_harbor_kite`, never a
function name, the identifier went `ig` -> `dg` across one patch release), and
every non-empty JS string is truthy. So `CLAUDE_CODE_HARBOR_KITE=0` REQUESTS the
gate — it does not decline it — and this module classifies it as
`"requested-unbound"`/`"open"` accordingly. Reading `"0"` as a decline here
would report an opt-out the harness never performed.

Why the environment is an honest oracle for "we asked":

    - The variable reaches the session's own process and is not stripped.
      Measured 2026-08-15 against the shipped bundle
      (`~/.local/share/claude/versions/2.1.233`): `CLAUDE_CODE_HARBOR_KITE`
      occurs exactly ONCE in 320MB of bundle, in the gate predicate itself, and
      the bundle carries no `delete <env>.CLAUDE_CODE_*` of any shape. Contrast
      `CLAUDE_CODE_MESSAGING_SOCKET`, which the launch bundle DOES `unset` — the
      two variables are not interchangeable as evidence and this module reads
      the un-stripped one for the request signal.
    - An op/hook process is spawned fresh by the session and inherits its
      environment, so `os.environ` here IS the session's environment. The same
      inheritance already carries `reachability._socket_env_self_match`'s
      `CLAUDE_CODE_MESSAGING_SOCKET` self-signal; this module adds no new
      assumption, it reuses a proven one.

Negative-spec:
    - SELF-SCOPED ONLY. Every value describes the CALLING session and nothing
      else. A peer's registry record carries no environment, so no peer's
      request state is knowable from here and none is ever reported. Callers
      must name the field for its subject (`caller_messaging_gate`), never
      attach it to a resolved target.
    - Does NOT change, wrap, or re-answer `reachability.messaging_available()`
      or `reachability.resolve_address()`. `messaging_available()` stays the
      box-level `messaging_socket_path` predicate its own negative-spec pins
      (it must NOT start keying on `peerProtocol` or on anything here), and
      this module never imports it.
    - Manufactures no address, and holds no address-shaped value. A `"open"`
      state is not an address and is never a substitute for one — the
      resolver's Anti-scope ("a confident wrong address is worse than no
      address") is untouched because nothing here produces an address at all.
    - Reads the environment ONLY. No registry scan, no `harness_registry.
      snapshot()`, no debug-log parse, no file, no cache, no cross-call state.
      The harness debug log records the bind verbatim but is written only under
      `--debug` (12 files for 52 sessions on this box, none from the day this
      module was written), so it is an opportunistic artifact, not an oracle.
    - `inbox_bound` is NOT a per-peer or box-wide reachability claim. It says
      only that THIS session's own inbox is bound. A session can be `"open"`
      while a given peer still has no address (that is
      `NotReachableReason.PEER_INBOX_ABSENT`), and the two must not be read as
      one fact.
    - Never reports the state of a LATE bind it cannot see. A GrowthBook
      refresh can bind mid-session; a process spawned after that refresh
      inherits the updated environment and reads `"open"`, one spawned before
      it does not exist to be asked. This module reports the instant it ran and
      claims no window either side of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

GATE_ENV_VAR = "CLAUDE_CODE_HARBOR_KITE"
SOCKET_ENV_VAR = "CLAUDE_CODE_MESSAGING_SOCKET"


class GateState:
    """The four states, named. See the module docstring for what each means
    and why `"0"` is a REQUEST rather than a decline."""

    NOT_REQUESTED = "not-requested"
    DECLINED = "declined"
    REQUESTED_UNBOUND = "requested-unbound"
    OPEN = "open"


_NOTES = {
    GateState.NOT_REQUESTED: (
        "Cross-session messaging was not requested for this session. "
        "Route peer traffic through the peer_notice.send channel."
    ),
    GateState.DECLINED: (
        "Cross-session messaging was declined for this session. "
        "Route peer traffic through the peer_notice.send channel."
    ),
    GateState.REQUESTED_UNBOUND: (
        "Cross-session messaging was requested at launch and no inbox bound. "
        "Route peer traffic through the peer_notice.send channel."
    ),
    GateState.OPEN: "This session's cross-session messaging inbox is bound.",
}


@dataclass(frozen=True)
class MessagingGate:
    """This session's own messaging-gate state — request and outcome, separated.

    `requested` is the harness's own predicate re-evaluated on the calling
    process's environment: `True` for a non-empty `CLAUDE_CODE_HARBOR_KITE`,
    including the value `"0"` (JS string truthiness — see the module
    docstring). `inbox_bound` is `True` when `CLAUDE_CODE_MESSAGING_SOCKET` is
    set, which the harness sets at exactly one place, a successful
    `startCrossSessionInbox()` bind.

    `state` is the pair collapsed into the reader's switch; `note` is a
    display-only sentence in the agent-facing register (`docs/wiki/
    guard-messaging.md` § Register — one fact, one terse alternative), never
    parsed. Consumers branch on `state`, never on `note`'s text.

    Negative-spec: every field is about the CALLING session. Nothing here is
    evidence about a peer, about the box as a whole, or about the remote
    `agents_cross_session_inbox` GrowthBook flag — a `"requested-unbound"`
    reading in particular is evidence AGAINST the remote flag being the whole
    story, since the local short-circuit runs ahead of the GrowthBook read.
    """

    state: str
    requested: bool
    inbox_bound: bool
    note: str


def classify(environ: Optional[Mapping[str, str]] = None) -> MessagingGate:
    """Classify the calling session's messaging gate into one of
    `GateState`'s four.

    `environ` defaults to `os.environ` (the calling process's own environment,
    inherited from the session that spawned it). It is a parameter so tests can
    exercise every state without mutating process-wide state — `pyproject.toml`
    carries an `allow_environ_leak` marker precisely because such mutation is
    otherwise a suite-level defect, and this function needs no test to claim it.

    Negative-spec: never raises. A caller on an advisory read path (the
    resolver's own degrade discipline) gets a `MessagingGate` on every input,
    including an empty mapping — the absent case IS a state (`"not-requested"`),
    not a failure.
    """
    env = os.environ if environ is None else environ

    raw_gate = env.get(GATE_ENV_VAR)
    requested = bool(raw_gate)
    inbox_bound = bool(env.get(SOCKET_ENV_VAR))

    if inbox_bound:
        state = GateState.OPEN
    elif requested:
        state = GateState.REQUESTED_UNBOUND
    elif raw_gate is None:
        state = GateState.NOT_REQUESTED
    else:
        state = GateState.DECLINED

    return MessagingGate(
        state=state,
        requested=requested,
        inbox_bound=inbox_bound,
        note=_NOTES[state],
    )


def to_dict(gate: MessagingGate) -> dict:
    """Serialize a `MessagingGate` for a JSON-RPC/CLI payload.

    Owned here rather than duplicated in each consumer: `coordinator_core.ops.
    session_resolve_address` and `coordinator/bin/session-reachability-cli.py`
    carry deliberately lock-stepped serializers of the same resolver result
    (that CLI's own header comment pins the lock-step), and a second hand-rolled
    dict in either is where the two drift.
    """
    return {
        "state": gate.state,
        "requested": gate.requested,
        "inbox_bound": gate.inbox_bound,
        "note": gate.note,
    }
