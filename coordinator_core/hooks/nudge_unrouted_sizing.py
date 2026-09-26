"""
coordinator_core.hooks.nudge_unrouted_sizing — Stop-hook advisory op.

Purpose: Catch the EM computing a route through the sizing lobby (`coordinator:sizing`),
narrating "taking it into plan now" or the equivalent, and then ending the turn WITHOUT
actually invoking the resolved room. Live incident 2026-07-31, session 66339b3f: the
sizing-object was written with `status: sized`, `route: plan`, no judgment halt open
(`fork: null`, `xl_exit: null`) — a fully machine-resolved route — and the EM stopped
short of `coordinator:plan` anyway.

A second, structurally analogous seam (`seam: plan->execute-plan`) catches the same
narrate-then-stop shape one gate later: an EM narrating resuming/continuing an
already-authorized plan's execution and then stopping without invoking
`coordinator:execute-plan`. See "Seam naming" and "Negative-spec" below for the
plan->execute-plan seam's own state/text criteria and its hard exemption for the
pre-execute PM authorization gate.

This is a near-clone of `nudge_harness_directive_dispatch.py`; its structure (session
keying, sentinel resolution, atomic fire-once claim, the `stop_hook_active` / `agent_id`
/ env-off guards, the `has_true_sid` degradation notice) is mirrored verbatim rather than
re-derived — see that module's own docstring for the rationale each piece exists.

Detection differs from the sibling in kind, not just content: instead of pattern-matching
the EM's own prose, this op cross-references two independent on-disk facts written by
OTHER bookkeeping hooks this session — `track_touched_files.py`'s `touched.txt` (did the
EM write a sizing-object at all?) and the sizing-object's own YAML body (is it a genuinely
unblocked route?) — plus a bounded scan of the transcript for evidence the resolved room
was actually entered.

Three judgment halts are EXEMPT and this is the correctness core, not an edge case:
    - An OPEN APPETITE FORK is `appetite_exceeded` in `detents` with `fork` still
      null — the appetite/estimate diverged and the PM has not yet picked a
      resolution. That is a genuine open question, not a route the EM merely
      failed to act on.

      Read the DETENT, never `fork`'s nullity. `coordinator_core.sizing_assemble.
      route()` emits `fork: None` ALWAYS — it is the sizing skill's resolution
      slot, filled only once the PM has actually picked `cut_to_fit` vs
      `raise_appetite` — and that module's own negative-spec names this exact
      misread: "Setting `fork` here to either enum value pre-emptively would
      misread as 'engine already decided' to any caller checking `fork is not
      None` as its divergence test." An earlier version of this op WAS that
      caller: it required `fork is None` to fire, which is the shape every
      unresolved appetite fork has by construction, so it fired on every one of
      them — the precise failure mode the exemption exists to prevent. The
      divergence signal is `appetite_exceeded` in `detents`, full stop.

      `fork` non-null (the PM HAS picked) is also treated as silence here. That
      is deliberate and asymmetric: the halt is closed, so a fire would be
      defensible, but staying silent keeps this criteria function purely
      false-positive-removing and costs only a missed nudge — the cheap
      direction this module already accepts everywhere else.
    - An OPEN POST-SIZE PROMPT (`_post_size_prompt_open`, added alongside appetite
      leaving the front of sizing — see "Decision: the post-size prompt exemption
      is unbounded" below) is `post_size_prompt_pending` in `detents` with `fork`
      still null — the sizing lobby delivered a size at M or above with no
      appetite stated, asked the PM the open "shall we go with that, split it,
      cut it, what's up?" question, and the PM has not yet answered. Same shape
      as the appetite fork above, same reasoning, same asymmetric silence-on-
      resolved posture — a sibling predicate, not a fold-in, because the two
      halts are genuinely distinct questions that merely happen to share a
      resolution slot (`fork`).
    - `route: pm-decision` with `xl_exit: null` means the PM has not yet chosen among
      the four XL exits — `xl_exit: null` NEVER means accept. This case is already
      excluded by construction here: the route allow-list below is `{plan,
      spec-dispatch, dispatch}`, and `pm-decision` is not a member of it, so a
      `pm-decision` sizing-object never reaches the fork/xl_exit checks at all.
A hook that nudged the EM through any judgment halt would be worse than no hook —
it would train the EM to grind past the PM's genuine decision points.

Decision: the post-size prompt exemption is unbounded, not freshness-bounded.
    `_post_size_prompt_open` never expires on its own — there is no clock check
    against how long the prompt has been pending. This was a live, answered
    question (source plan's own "Ask 3"), not an oversight, for reasons specific
    to THIS hook's own substrate:
        - Session-scoping already bounds the hook's reach. This op's candidate
          set is `_find_sizing_candidate` -> `_session_touched_sizing_files`
          (via `touched.txt`) — it only ever considers sizing-objects the
          CURRENT session touched, never one forgotten across sessions. Combined
          with the fire-once-per-session sentinel (`_claim_fire`), the exemption
          discharges at session end: the object leaves the candidate set the
          moment the session that wrote it stops. The residual cost is real but
          bounded to one session's turn-ends — a session that writes an M+
          sizing, asks the open question, and wanders off gets no further nudge
          THAT session, and the next session touching the object re-evaluates
          fresh.
        - `_runtime_threshold_minutes` (above) is deliberately NOT reused as a
          freshness bound here, even though it looks like an available clock.
          It bounds DISPATCH runtime against a dispatch row's `dispatched_at`
          timestamp in `dispatched-agents.txt` — an answer to "is this specific
          subagent dispatch still plausibly running," nothing else. No
          sizing-object clock exists to bound against: `sizing-object.schema.json`
          carries no created/updated timestamp, and inventing a freshness bound
          would mean a new schema field, a file mtime (destroyed by clone), or a
          per-object `git log` shell-out on the session hot path — the last of
          which is break-class on Windows per this repo's own CLAUDE.md. There is
          no cheap clock to reuse, so unbounded is not laziness — it is what the
          substrate actually supports.
        - Reconciled against `state/memo-outbox/sent/
          sizing-lobby-advisory-discharges-on-being-seen-not-on-a-route.md`, which
          requires an advisory to discharge on a route being taken, not on being
          seen: at first read, an unbounded exemption that lifts at session end
          looks like it discharges on a clock rather than an action. It does not
          conflict once the object of the test is placed correctly. The exemption
          itself is not the advisory — it is a SUPPRESSION of the M+ prompt
          advisory while a genuine PM halt is open. The advisory it guards still
          discharges exactly on the route being taken: the EM routes the object,
          `status` flips to `routed`, and the object leaves `_matches_criteria`
          outright (the `status == "sized"` clause). Session-scoping bounds the
          HOOK'S REACH (which sizing-objects it ever considers this run), not the
          ADVISORY'S DISCHARGE CONDITION (what ends its nudging) — two different
          things, and the doctrine's test is about the latter.
        - Symmetric with `_appetite_fork_open`, which is already unbounded on
          this exact same substrate and has been since this hook shipped. A
          freshness bound on the post-size prompt alone would make two
          structurally identical halts (both detent-keyed, both `fork`-resolved,
          both session-scoped by the same candidate-set mechanism) behave
          differently for no stated reason. Unbounded here is consistency with
          established precedent in this same function, not a new concession.

Room-invocation evidence is route-shaped, not one-size-fits-all:
    - `route: plan` and `route: spec-dispatch` both resolve to `coordinator:plan`
      (`coordinator/skills/plan/SKILL.md` — `spec-dispatch` is the S-lane inside the
      same skill, not a separate one; verified against that file, which conforms both
      routes into Branch B/C of the one `coordinator:plan` invocation). Evidence is a
      `Skill` tool_use block in the transcript naming `coordinator:plan`.
    - `route: dispatch` names no skill at all — "the room" is simply an Agent dispatch
      happening directly. Evidence is the same on-disk signal
      `nudge_harness_directive_dispatch._session_has_dispatched` already uses:
      a non-empty `dispatched-agents.txt` for this session.

Transcript reads use the same bounded tail-window technique as
`nudge_harness_directive_dispatch.last_assistant_text` (binary seek, discard the partial
line the seek lands inside, decode with errors="replace") rather than reading a
potentially multi-MB transcript whole — deliberately BOUNDED, not a full-session scan.
Any I/O or parse failure, or a Skill invocation that happened to fall outside the tail
window, reads as "no evidence found" — which fires. This is an explicit judgment call:
a missed nudge costs one un-nudged turn; a suppressed nudge (from a false "yes, it was
invoked") costs the whole failure mode recurring silently. That asymmetry is why the
fallback direction is "fire", mirroring the sibling's own transcript-read posture — but
it applies ONLY to the room-invocation check. The two hard exemptions above are read
from the sizing-object's own on-disk YAML, never inferred from a transcript read, and a
YAML parse failure on the sizing-object itself is treated as "cannot prove the exemptions
don't apply" — i.e. silence, the opposite fallback — because misreading `fork` or
`xl_exit` as null when they are not is exactly the false-positive this op must never
produce.

Negative-spec, generalised: an open halt needs a positive, producer-emitted
signal, never an inference from a missing value. This is the general form of
the rule; `fork` (below) is the worked example it was originally filed
against, not the whole rule. A halt is "open" only when the producer
(`sizing_assemble.route()`) has affirmatively written a detent naming it —
`appetite_exceeded` or `post_size_prompt_pending` in `detents` — never by
noticing that some other field happens to be absent or null, because absence
is the shape EVERY sizing-object has by construction whether or not the halt
in question ever fired.
    - The worked example: `fork` is null on both sides of the appetite-fork
      question — before the halt exists at all, and while it is genuinely
      open — so `fork is None` cannot distinguish them. An earlier version of
      this op read `fork is None` as its divergence test and fired on every
      open fork, the precise failure this exemption exists to prevent (see
      above). The fix was to key on the DETENT, not `fork`'s nullity.
    - The near-miss this generalisation is filed FROM, not merely alongside:
      DoE's first draft of the post-size-prompt exemption proposed detecting
      the new halt from the ABSENCE of `appetite` in the sizing-object — since
      appetite absence is exactly the shape the post-size prompt flow produces.
      That is the fork misread one field over: appetite absence is the shape
      EVERY M+ object has once appetite leaves the front of sizing, whether or
      not the open-question flow ever ran, exactly as `fork is None` was the
      shape every appetite fork had whether open or resolved. Two engineers,
      hours apart, reached for a nullity/absence read despite both having read
      the fork-specific version of this rule — which is the evidence the rule
      was filed too narrowly the first time, and why it is restated here in its
      general form rather than left as a second one-off case note.

Negative-spec:
    - Never fires on a subagent's Stop (agent_id present) or when stop_hook_active is set,
      matching the sibling's loop-guard posture verbatim.
    - Never fires when no sizing-object was written THIS session (the express-lane ask) —
      criterion 1 is a hard gate, checked before any YAML is even opened.
    - Never fires on `status: routed` (already handed off) or `status: draft` / `superseded`.
    - Not a general "did the EM finish the sizing lobby" auditor — it matches only the
      exact shape of the observed incident.
    - The **sizing->room** seam's routable set (`_ROUTABLE_ROUTES`) is not a fixed
      three-item list to be extended as the lobby grows rooms — it names the RULE
      "routes whose room an EM can enter without a PM utterance," and only routes
      meeting that rule belong in it. `execute-plan` is not a sizing-object route at
      all and cannot reach that seam's code by construction; `"execute-plan" not in
      _ROUTABLE_ROUTES` is pinned by its own test so this stays true even as the
      module grows. `goal-setting` and `roadmap` are excluded by the same rule for a
      different reason: both are PM-gated rooms, not EM-enterable ones — per DoE-claude
      `coordinator/skills/goal-setting/SKILL.md` and `coordinator/skills/roadmap-planning/
      SKILL.md`, each carrying a frontmatter `description: "PM-GATED. ..."` — so
      falsifiable by grepping those two files, not by memory. `shape` is the one
      exception worth naming precisely: it is PM-COLLABORATIVE, not frontmatter-gated
      the same way, so "PM-gated" as a blanket label overstates its own posture even
      though it is likewise excluded here. DoE separately accepted the
      `goal_setting_pm_gated` detent (chunk body, this plan), which means this
      exclusion is no longer the ONLY line holding the PM-gate boundary for
      `goal-setting` — it is now a second, independent guard, though it was designed
      as the sole line of defense had DoE declined that detent. Caveat: the RULE's
      truth for `goal-setting` and `roadmap` rests on frontmatter living in ANOTHER
      REPO (DoE-claude), so no test in this repo can pin it — if DoE ever un-gates
      either skill, this exclusion becomes silently wrong until someone notices.
    - The **plan->execute-plan** seam (second seam, live as of this module's second seam
      addition — see "Seam naming" below) NEVER fires on the pre-execute PM authorization
      gate: a plan with NO `execution_authorized_by` in its frontmatter is never a nudge
      target, REGARDLESS of `status` — including `status: approved` or `status: executing`,
      should those ever appear without the stamp. An EM that finished review and stopped to
      ask the PM to authorize execution is behaving CORRECTLY; nudging it would convert a
      working PM gate into pressure to skip it, the same error class as nudging either of
      the two judgment-halt exemptions above or the sizing-lobby's own `pm-decision` route.
      This seam's business starts only AFTER `execution_authorized_by` is present: a plan
      already execution-authorized and started (`status: executing`, the strongest single
      positive signal, or `status: approved`), then narrated-and-abandoned mid-chunk, has no
      gate left standing between the EM and finishing it. Any status this seam does not
      explicitly recognise as authorized-and-active (`status: shipped` — used on 9 real
      plans but absent from `plan.schema.json`'s enum — every terminal status, or anything
      else) defaults to silence; see `_plan_execution_authorized_and_active`.

Seam naming: two seams live in this module. `seam: sizing->room` (the original) detects a
resolved-but-unentered sizing route. `seam: plan->execute-plan` (added second) detects an
execution-authorized plan narrated-and-abandoned before `coordinator:execute-plan` was
invoked. The universal guards (env switch, `stop_hook_active`, `agent_id`, the shared
fire-once sentinel, EM-discriminator, in-flight-dispatch, the sentence-bounded
forward-intent/route-referent text analysis) are seam-agnostic by construction and are
shared unchanged by both seams — each seam supplies only its own state-read (candidate
lookup + criteria function) and its own room-invocation-evidence function; `_text_trips_tell`
takes the referent pattern as a parameter for exactly this reason, so a second seam reuses
the one sentence-bounded co-occurrence helper rather than growing a second copy. The
fire-once sentinel is a SINGLE SHARED slot across both seams (one nudge per session,
whichever seam earns it first), not one slot per seam — the PM's stated intent is that the
EM gets the point after a single nudge per session, and a second independent slot would let
both seams fire in the same turn-end, doubling the interruption the "at most once" design is
meant to bound. No registry or plugin framework exists for this and none should be added for
two seams — that abstraction earns its cost at a third, not before.

Guard ordering is a correctness constraint, not incidental style: this fires on
EVERY turn-end of every EM session in the fleet, so guards run cheapest-first with
hard short-circuits — env switch -> `stop_hook_active` -> `agent_id` -> sentinel-exists
-> repo_root/session_id -> state read (touched.txt + sizing YAML) -> EM-discriminator
(em-session-id.txt lookup) -> in-flight-dispatch check -> text (room-invocation
evidence + the forward-intent tell). The EM-discriminator and in-flight-dispatch
checks are demoted to run only once a matching sizing-object has actually been
found — they are the two guards that pay for an event loop, a transcript tail-read,
or a YAML parse, so they run AFTER the near-free `touched.txt` gate, not before it.
No filesystem scan, YAML parse, or transcript read happens before the cheap guards
have passed, and no event loop or transcript read happens at all unless a
genuinely matching sizing-object was found this session, so the overwhelmingly
common case ("no sizing-object this session") stays near-free.
(Review: eng-director/the Director of Engineering F2 — the prior ordering ran the subagent and
in-flight checks, plus their asyncio/yaml/subagent_arrival_check imports, on
every turn-end regardless of whether a sizing-object existed; those imports are
now lazy, paid only on a qualifying turn.)

EM-discriminator (house-authoritative, not `agent_id` alone): `agent_id` is a cheap,
useful first check but is NOT the house-authoritative subagent test — a dispatched
subagent's Stop payload does not always carry `agent_id`, and `CLAUDE_CODE_SESSION_ID`
is explicitly documented as unreliable for this purpose (it inherits the dispatching
EM's own id inside a subagent process). The house-authoritative test, per
`coordinator/hooks/scripts/runtime-tripwire-em-check.py` (DoE-claude repo; docstring
~lines 26-45, implementation ~1585-1600): a firing `session_id` found under
`<git-common-dir>/coordinator-sessions/.agents/<session_id>/em-session-id.txt` is a
SUBAGENT session, full stop — never fire. `_is_subagent_session` below is that same
lookup, resolved via `git_common_dir` (worktree-safe) rather than a raw `.git` join,
matching this module's own established convention for every other per-session
bookkeeping read (see `_session_has_dispatched` / `_session_touched_sizing_files`
above). Ambiguity (git_common_dir failing, an unreadable directory) degrades to "not
a confirmed subagent" — i.e. processing continues — matching this file's existing
fail-toward-availability posture on every other non-hard-exemption ambiguity; the
cheap `agent_id` check already catches the overwhelming majority of subagent Stops,
so this is a second, narrower net, not the only one.

Legitimate-wait suppression (in-flight dispatch): a turn-end with a dispatch still
running is not a stalled EM, it is a correct wait, and must not fire.
`dispatched-agents.txt` is APPEND-ONLY and carries no completion record of its own —
confirmed against a real session's tracker file, which still listed rows for agents
that had already returned and one that had been explicitly stopped. A suppressor
keyed on that file's mere non-emptiness would go permanently silent after an EM's
first dispatch of the session (essentially every session) — worse than the noise it
would prevent, and silently so, since a hook that never fires looks identical to one
with nothing to report. This op does not do that: the liveness read is per-row, from
that row's OWN subagent transcript, via the EXACT same running-vs-arrived
determination `runtime-tripwire-em-check.py` already computes for its own overrun
suppression — `coordinator_core.hooks.subagent_arrival_check`'s `_handler` (state
`"arrived"` / `"running"` / `"unknown"`, called directly in-process rather than over
the DoE-side IPC round-trip that script needs) — rather than authoring a second,
divergent "is a dispatch still running" answer; two surfaces disagreeing about that
question is itself a defect class this deliberately avoids. `dispatched-agents.txt`
rows are read exactly as that script reads them (tab-separated `agent_id, model,
subagent_type, dispatched_at`).

Three-tier resolution per row (see `_session_has_in_flight_dispatch`'s own docstring
for the full account): `"arrived"` never counts as in-flight regardless of elapsed
time (a confirmed positive completion signal beats any timer); `"running"` counts as
in-flight until the row ages past the shared `RUNTIME_TRIPWIRE_MAX_TRACK_MIN` hard
cap (default 90 min, same env var as the sibling script) — "running" is
`subagent_arrival_check._classify`'s DEFAULT bucket for any well-formed
non-arrival record, not an affirmative liveness proof, so a killed-mid-tool
agent holds this state for the full 90 minutes too (F7; safe direction,
over-suppression not over-firing); `"unknown"` (no resolvable fact at all —
a real, named gap in `subagent_arrival_check` itself for dispatch shapes whose
transcript never lands at the expected per-agent path, not invented here) counts as
in-flight only within its OWN per-model `RUNTIME_TRIPWIRE_OPUS_MIN` /
`_SONNET_MIN` / `_HAIKU_MIN` window (defaults 25/12/10 min, same env vars and
defaults as that script's `_runtime_threshold_minutes`, ported byte-faithfully as
this module's own `_runtime_threshold_minutes`) — a tighter bound than the 90-minute
cap, because "unknown" is not evidence of anything happening, only elapsed time is.

Honest limitation, named rather than papered over: this is a heuristic standing in
for a liveness fact the tracker does not record. An "unknown" row over-suppresses
for up to its per-model window even if that agent finished seconds after dispatch —
the safe direction. A "running" row that genuinely runs past `MAX_TRACK_MIN` stops
suppressing and could be nudged mid-wait — the unsafe direction, bounded by this
op's own fire-once sentinel. The correct long-term fix is a completion record in the
dispatch tracker itself; revisit this heuristic if one lands — building that record
is out of scope for this op.

This whole in-flight determination is a SEPARATE concern from
`_session_has_dispatched`'s use of the same file as route-invocation evidence for
`route: dispatch` above — one asks "was anything ever dispatched", the other asks
"is anything dispatched still running right now"; they are not the same question and
must not be conflated into one check.

Text half (state AND text, not state alone): the state-only version of this op fired
on ANY sizing-object left unentered, including a turn that finished something else
entirely and correctly reported that completion. The fire condition is now state AND
text: the final assistant message must ALSO carry a POSITIVE forward-intent tell —
narrating the resolved next move ("taking it into plan now", "next I'll...", "I'll
invoke...", "proceeding to...", "now entering...", "let me take this into...") —
before this op fires at all. `_text_trips_tell` has no suppressor of any kind: it is
a single positive-match gate, and that absence is the design, not an omission. A
completion report ("fixed the parser, committed, pushed, tests green") is already a
correct non-fire under this gate on its own — it carries no forward-intent tell, so
the positive requirement is never satisfied, with nothing else needed to exclude it.
The overlap case ("Fixed the parser and committed. Next I'll take this into
`coordinator:plan`." must still fire) requires no precedence rule to get right either
— there is only one check, and that message trips it.

This is a STRONGER discharge of
`state/lessons/2026-07-28-a-detector-s-suppressor-must-not-key-on-c91c411f46ed.yaml`
(DoE-claude repo, status `open`, born from this hook's structural sibling
`nudge_harness_directive_dispatch.py`'s own F2 finding) than a correctly-ordered
suppressor would be: that lesson's failure mode is a suppressor that can veto a
genuine tell, and a design with no suppressor at all has no vetoing mechanism to get
wrong in the first place — structurally immune rather than merely correctly ordered.
An earlier draft of this module carried a `_COMPLETION_SHAPE_RE` suppressor checked
AFTER the forward-intent match; it was inert by construction (a positive-match gate
already excludes every completion-only report, so the suppressor branch could never
be reached with anything left for it to change) and was removed rather than kept as
dead code behind an explanatory comment. Message text is sourced via
`_final_message_text`, which prefers the harness-supplied `last_assistant_message`
and falls back to `nudge_harness_directive_dispatch.last_assistant_text`'s own
bounded tail-read — imported and reused directly rather than re-derived, per that
sibling's own documented preference for this exact fallback shape.

Spec backlink: two-repo change, DoE-side transport shim companion in
`coordinator/hooks/scripts/` (DoE-claude repo). Live incident 2026-07-31, session 66339b3f.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from coordinator_core._hook_envelope import context_only
from coordinator_core.hooks.nudge_harness_directive_dispatch import (
    last_assistant_text as _last_assistant_text,
)
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.session.scope import (
    _TOUCH_RECORD_FILENAME,
    _read_touch_record_as_legacy_lines,
    parse_touch_event,
)

_arrival_check = None


def _get_arrival_check():
    global _arrival_check
    if _arrival_check is None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        _arrival_check = _handler
    return _arrival_check


# each `description: "PM-GATED. ..."` — see the module docstring's Negative-spec
_ROUTABLE_ROUTES = frozenset({"plan", "spec-dispatch", "dispatch"})

_SIZING_PATH_RE = re.compile(r"^state/sizings/[^/]+\.ya?ml$")

# `sizing_assemble.DETENT_ENUM` is greppable from either side.
_APPETITE_DIVERGENCE_DETENT = "appetite_exceeded"

# split/cut/what's up?" question. Same shape as `_APPETITE_DIVERGENCE_DETENT`
# coupling to `sizing_assemble.DETENT_ENUM` is greppable from either side.
_POST_SIZE_PROMPT_DETENT = "post_size_prompt_pending"

_SKILL_BY_ROUTE = {
    "plan": "coordinator:plan",
    "spec-dispatch": "coordinator:plan",
}

# seam: plan->execute-plan — a SEPARATE evaluator from the sizing->room seam
# above (not a member of `_ROUTABLE_ROUTES`; see the boundary test

_PLAN_PATH_RE = re.compile(r"^docs/plans/[^/]+\.md$")

_PLAN_ROUTABLE_STATUSES = frozenset({"approved", "executing"})

_EXECUTE_PLAN_SKILL = "coordinator:execute-plan"

_EXECUTE_PLAN_REFERENT_BASE = r"coordinator:execute-plan|execute-plan|execute the plan"

_TAIL_WINDOW_BYTES = 512_000

_MAX_TRACK_MIN_ENV = "RUNTIME_TRIPWIRE_MAX_TRACK_MIN"
_DEFAULT_MAX_TRACK_MIN = 90

_OPUS_MIN_ENV = "RUNTIME_TRIPWIRE_OPUS_MIN"
_SONNET_MIN_ENV = "RUNTIME_TRIPWIRE_SONNET_MIN"
_HAIKU_MIN_ENV = "RUNTIME_TRIPWIRE_HAIKU_MIN"
_DEFAULT_OPUS_MIN = 25
_DEFAULT_SONNET_MIN = 12
_DEFAULT_HAIKU_MIN = 10


def _runtime_threshold_minutes(model: str) -> int:
    model = model or ""
    opus_default = int(os.environ.get(_OPUS_MIN_ENV, str(_DEFAULT_OPUS_MIN)) or _DEFAULT_OPUS_MIN)
    sonnet_default = int(
        os.environ.get(_SONNET_MIN_ENV, str(_DEFAULT_SONNET_MIN)) or _DEFAULT_SONNET_MIN
    )
    haiku_default = int(
        os.environ.get(_HAIKU_MIN_ENV, str(_DEFAULT_HAIKU_MIN)) or _DEFAULT_HAIKU_MIN
    )

    if "[1m]" in model or "-1m" in model:
        return opus_default
    if "opus" in model:
        return opus_default
    if "sonnet" in model:
        return sonnet_default
    if "haiku" in model:
        return haiku_default
    return opus_default

# Text half — a single POSITIVE forward-intent gate, no suppressor. A completion
# with a ROUTE REFERENT — the resolved route's own skill name or route noun. A

_CURLY_APOSTROPHE_RE = re.compile("[’ʼ]")

_FORWARD_INTENT_RE = re.compile(
    r"\btaking (?:it|this) into\b"
    r"|\bnext,?\s+i'll\b"
    r"|\bi'll (?:invoke|dispatch|take (?:it|this) into|enter)\b"
    r"|\bproceeding to\b"
    r"|\bnow entering\b"
    r"|\blet me take this into\b"
    r"|\binvok(?:e|ing)\b"
    r"|\bkick(?:ing)? off\b"
    r"|\bmov(?:e|ing) into\b"
    r"|\bhead(?:ing)? into\b"
    r"|\brout(?:e|ing) to\b"
    r"|\bgoing to (?:take|invoke)\b"
    r"|\bnext step\b",
    re.IGNORECASE,
)

_ROUTE_REFERENT_RE = re.compile(
    r"coordinator:plan|coordinator:sizing|\bplan\b|spec-dispatch|\bdispatch\b|\bsizing\b",
    re.IGNORECASE,
)

_SENTENCE_END_RE = re.compile(r"[.!?]")

_ROUTE_REFERENT_WINDOW_CHARS = 120


_EXECUTE_PLAN_REFERENT_WINDOW_CHARS = _ROUTE_REFERENT_WINDOW_CHARS


def _execute_plan_referent_regex(rel_path: str) -> re.Pattern:
    slug = Path(rel_path).stem
    pattern = _EXECUTE_PLAN_REFERENT_BASE
    if slug:
        pattern = f"{pattern}|{re.escape(slug)}"
    return re.compile(pattern, re.IGNORECASE)


def _sentence_bounds(text: str, pos: int) -> tuple[int, int]:
    start = 0
    for boundary in _SENTENCE_END_RE.finditer(text, 0, pos):
        start = boundary.end()
    end_match = _SENTENCE_END_RE.search(text, pos)
    end = end_match.start() if end_match else len(text)
    return start, end

_NUDGE_MESSAGE_TEMPLATE = """\
[nudge] route: {route} is resolved and unblocked — {action} now.

[nudge] {sizing_path} recorded status: sized, route: {route}, with no judgment
[nudge] halt open — no appetite_exceeded detent awaiting your PM's cut-vs-raise call, and
[nudge] no open XL exit — so the sizing lobby already did the work of
[nudge] picking the room. Narrating the route and ending the turn without invoking it buys
[nudge] nothing: the round trip back to your operator costs a turn and the route will not
[nudge] change.

[nudge] What still gates, unchanged: ask-before-external-action, the pre-execute
[nudge] authorization gate, and any PM-gated skill. Satisfying this nudge never lowers one
[nudge] of your own gates.

[nudge] If you held back because of a genuine PM-altitude question, say so and end the
[nudge] turn again; this fires at most once per session and will not repeat.
"""

_EXECUTE_PLAN_NUDGE_MESSAGE_TEMPLATE = """\
[nudge] {plan_path} (status: {status}) is already PM-authorized but unentered \
this turn — the gate is satisfied, so stopping just costs a turn. Use instead:
    `coordinator:execute-plan`
"""


def _session_key(payload: dict) -> tuple[str, bool]:
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid.strip())
        if safe:
            return safe, True
    return f"pid-{os.getpid()}", False


def _repo_root(payload: dict) -> str | None:
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        return None
    probe = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent


def _resolve_git_dir(dot_git: str) -> str | None:
    if os.path.isdir(dot_git):
        return dot_git
    try:
        with open(dot_git, "r", encoding="utf-8", errors="replace") as fh:
            pointer = fh.read().strip()
    except OSError:
        return None
    if not pointer.startswith("gitdir:"):
        return None
    target = pointer[len("gitdir:"):].strip()
    if not target:
        return None
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(dot_git), target)
    return os.path.normpath(target)


def _sentinel_path(payload: dict) -> str | None:
    probe = _repo_root(payload)
    if probe is None:
        return None

    git_dir = _resolve_git_dir(os.path.join(probe, ".git"))
    if not git_dir:
        return None
    session_key, _has_true_sid = _session_key(payload)
    return os.path.join(
        git_dir, "coordinator-sessions", session_key,
        "unrouted-sizing-nudge.fired",
    )


def _claim_fire(sentinel: str | None) -> bool:
    if not sentinel:
        return True
    try:
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        with open(sentinel, "x", encoding="utf-8", newline="\n") as fh:
            fh.write("1")
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def _tail_text(path: str, max_bytes: int = _TAIL_WINDOW_BYTES) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            raw = fh.read()
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _session_has_dispatched(session_id: str, repo_root: str) -> bool:
    try:
        common_dir = git_common_dir(Path(repo_root))
    except Exception:
        return False
    dispatched = common_dir / "coordinator-sessions" / session_id / "dispatched-agents.txt"
    try:
        return dispatched.is_file() and dispatched.stat().st_size > 0
    except OSError:
        return False


def _session_touched_lines(session_id: str, repo_root: str) -> list[str]:
    """Return the bare repo-relative path recorded by each `touched.txt` line
    for this session, unfiltered.

    Reads track_touched_files.py's own write target — `touched.txt`, keyed by the RAW
    session_id (unsanitized, matching that writer's directory-naming contract) under
    git_common_dir. Any read failure (absent file, absent repo, unreadable) returns [].

    Parsed via `parse_touch_event` (coordinator_core.session.scope), NOT a bare
    `.strip()` of the raw line: `touched.txt` lines carry an event-log shape,
    `'<verb> <ISO-8601 timestamp> <path>'` (verb in {T, R}), once a writer
    starts emitting them (P2, sequenced after this chunk) — as well as every
    pre-existing bare `<path>`-only legacy line, which `parse_touch_event`
    reports as `('T', None, <path>)`. This reader is PLAIN-MEMBERSHIP ("did
    this session ever touch X"): it needs only the bare path half of the
    triple (`[2]`), never `verb`/timestamp claim-vs-release policy — so it
    calls `parse_touch_event` directly rather than a scope projection
    (`project_self_scope` / `project_peer_claims`), which answer a different,
    policy-bearing question this reader does not ask.

    Prior to this fix, `_session_touched_sizing_files` /
    `_session_touched_plan_files` regex-matched their `^state/sizings/...` /
    `^docs/plans/...` patterns — anchored at line start — against the WHOLE
    raw line. That is a hard break, not the "accepted degradation for a
    session that commits early" the plan's original § Known effects table
    recorded: an anchored regex against a verb-prefixed line
    (`'T 2026-08-03T12:00:00.000000+00:00 docs/plans/foo.md'`) never matches
    at position 0 regardless of when the touch happened, so this reader goes
    silent for EVERY touch once any line in the file carries a verb prefix,
    not merely for an early-committing session. Reading the bare path back
    out via `parse_touch_event(line)[2]` restores what the anchored regexes
    expect to see, and is a no-op on today's all-bare-legacy-line corpus
    (no writer emits event-shaped lines yet — this chunk lands before P2).

    Shared by BOTH seams (sizing->room and plan->execute-plan) so the touch record is
    read once per turn-end rather than once per seam — `_session_touched_sizing_files`
    and `_session_touched_plan_files` are thin path-regex filters over this same read.

    C2 — reads through `session.scope._read_touch_record_as_legacy_lines`, the C0
    seam, rather than parsing `touched.txt` directly: the seam unions the
    `touch-record.jsonl` family with a sibling `touched.txt` (legacy first, so a
    later jsonl event still wins the last-verb-wins fold below), returning
    old-dialect `'<verb> <ts> <path>'` lines this reader already knows how to parse
    via `parse_touch_event`. A degraded read (an unreadable/malformed family member)
    is treated the same as the prior bare OSError catch — silence ([]) — matching
    this reader's own long-standing fail-toward-availability posture: a missed
    nudge costs one un-nudged turn, never a false fire.
    """
    try:
        common_dir = git_common_dir(Path(repo_root))
    except Exception:
        return []
    sink_path = common_dir / "coordinator-sessions" / session_id / _TOUCH_RECORD_FILENAME
    try:
        lines, degraded = _read_touch_record_as_legacy_lines(sink_path)
    except Exception:
        return []
    if degraded:
        return []
    return [parse_touch_event(ln)[2] for ln in lines]


def _session_touched_sizing_files(session_id: str, repo_root: str) -> list[str]:
    return [ln for ln in _session_touched_lines(session_id, repo_root) if _SIZING_PATH_RE.match(ln)]


def _session_touched_plan_files(session_id: str, repo_root: str) -> list[str]:
    return [ln for ln in _session_touched_lines(session_id, repo_root) if _PLAN_PATH_RE.match(ln)]


def _load_sizing_object(repo_root: str, rel_path: str) -> dict | None:
    import yaml

    try:
        with open(os.path.join(repo_root, rel_path), "r", encoding="utf-8") as fh:
            obj = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    return obj if isinstance(obj, dict) else None


def _appetite_fork_open(obj: dict) -> bool:
    """Return True iff `obj` carries an UNRESOLVED appetite<->estimate fork.

    Open means: the lobby recorded the divergence (`appetite_exceeded` in
    `detents`) and the PM has not yet filled the resolution slot (`fork` still
    null). Reading the detent — not `fork`'s nullity — is the whole point; see
    the module docstring's judgment-halt section for why the inverse test fired
    on every appetite fork by construction.

    A malformed/absent `detents` (not a list) reads as "cannot prove no fork is
    open" -> True -> silence, matching `_load_sizing_object`'s own
    fail-toward-silence posture on every hard-exemption read.
    """
    detents = obj.get("detents")
    if not isinstance(detents, list):
        return True
    return _APPETITE_DIVERGENCE_DETENT in detents and obj.get("fork") is None


def _post_size_prompt_open(obj: dict) -> bool:
    """Return True iff `obj` carries an UNRESOLVED post-size, M+ appetite question.

    Open means: the lobby recorded the pending prompt (`post_size_prompt_pending`
    in `detents`) and the PM has not yet filled the resolution slot (`fork` still
    null). A sibling halt to `_appetite_fork_open`, kept as a SEPARATE predicate
    rather than folding into it — two distinct halts, two distinct names, per
    this module's own convention of one named predicate per halt. Reading the
    detent — never `fork`'s nullity — is the whole point; see the module
    docstring's "Negative-spec, generalised" section for the general rule this
    instantiates, and `_appetite_fork_open`'s own docstring for the original
    incident this shape is modeled on.

    A malformed/absent `detents` (not a list) reads as "cannot prove no prompt
    is open" -> True -> silence, byte-matching `_appetite_fork_open`'s own
    fail-toward-silence handling.
    """
    detents = obj.get("detents")
    if not isinstance(detents, list):
        return True
    return _POST_SIZE_PROMPT_DETENT in detents and obj.get("fork") is None


def _matches_criteria(obj: dict) -> bool:
    """Return True iff `obj` is a genuinely unblocked, unrouted sizing-object.

    All of: status sized, route in the routable set, no open appetite fork
    (`_appetite_fork_open`), no open post-size prompt (`_post_size_prompt_open`),
    no already-picked fork, and `xl_exit` still null. `route: pm-decision` is
    excluded purely by not being in `_ROUTABLE_ROUTES` — see module docstring.

    The `xl_exit is None` clause is UNREACHABLE-BY-CONSTRUCTION belt, not the
    live guard, and is deliberately NOT rewritten in `_appetite_fork_open`'s
    detent-keyed shape: `sizing_assemble.route()` appends `pm_decision_pending`
    only when the resolved route is `pm-decision`, which the route allow-list
    above already excludes, so no routable sizing-object can carry an open XL
    halt. It is retained against a hand-edited record, where silence is the
    safe direction anyway.
    """
    return (
        obj.get("status") == "sized"
        and obj.get("route") in _ROUTABLE_ROUTES
        and not _appetite_fork_open(obj)
        and not _post_size_prompt_open(obj)
        and obj.get("fork") is None
        and obj.get("xl_exit") is None
    )


def _load_plan_frontmatter(repo_root: str, rel_path: str) -> dict | None:
    import yaml

    try:
        with open(
            os.path.join(repo_root, rel_path), "r", encoding="utf-8", errors="replace"
        ) as fh:
            text = fh.read()
    except OSError:
        return None

    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return None

    try:
        obj = yaml.safe_load("\n".join(lines[1:end_idx]))
    except yaml.YAMLError:
        return None
    return obj if isinstance(obj, dict) else None


def _plan_execution_authorized_and_active(fm: dict) -> bool:
    """Return True iff `fm` is a plan frontmatter dict that is BOTH execution-authorized
    AND still active — the plan->execute-plan seam's own criteria function.

    THE CRITICAL BOUNDARY (see module docstring's Negative-spec): a plan with no
    `execution_authorized_by` NEVER fires here, regardless of `status` — that
    is the pre-execute PM authorization gate doing its job, checked FIRST and
    unconditionally. Only once that stamp is present does `status` matter at
    all, and only `status` in `_PLAN_ROUTABLE_STATUSES` (`approved`,
    `executing`) counts as active. Any other status — an unrecognised value
    such as `shipped` (used on 9 real plans, absent from
    plan.schema.json's enum), every terminal status (`landed`, `implemented`,
    `deferred`, `abandoned`, `superseded`), a missing status, or anything else
    — is silence by construction: membership-testing an allow-list already IS
    the "default unknown to silence" branch, with no separate unknown-status
    check needed.
    """
    auth = fm.get("execution_authorized_by")
    if not (isinstance(auth, str) and auth.strip()):
        return False
    return fm.get("status") in _PLAN_ROUTABLE_STATUSES


def _execute_plan_invoked(payload: dict) -> bool:
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    try:
        return _skill_invoked(transcript_path, frozenset({_EXECUTE_PLAN_SKILL}))
    except Exception:
        return False


def _skill_invoked(transcript_path: str, target_skills: frozenset[str]) -> bool:
    text = _tail_text(transcript_path)
    if not text:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use" or block.get("name") != "Skill":
                continue
            block_input = block.get("input")
            if not isinstance(block_input, dict):
                continue
            skill_val = block_input.get("skill")
            if isinstance(skill_val, str) and skill_val.strip() in target_skills:
                return True
    return False


def _is_subagent_session(session_id: str, repo_root: str) -> bool:
    """Return True iff `session_id` is a CONFIRMED subagent session, house-authoritative.

    Per runtime-tripwire-em-check.py (DoE-claude repo, docstring ~lines 26-45,
    implementation ~1585-1600): a firing session_id found under
    `<git-common-dir>/coordinator-sessions/.agents/<session_id>/em-session-id.txt`
    is a dispatched subagent's own session, not the EM's. This is a SECOND, narrower
    net alongside the cheap `agent_id` check in `op()` — not a replacement for it —
    covering the gap `agent_id` can miss (and never trusting
    `CLAUDE_CODE_SESSION_ID`, which inherits the dispatching EM's own id inside a
    subagent process, per that same module's docstring).

    Resolved via `git_common_dir` (worktree-safe), matching this module's own
    convention for every other per-session bookkeeping read, NOT a raw `.git` join.

    Any resolution failure (git_common_dir raising, an unreadable directory) returns
    False — "not a confirmed subagent" — so processing continues. This is a
    deliberate fail-toward-availability choice, not an oversight: this check is a
    second net behind the cheap `agent_id` guard, and misreading ambiguity as "is a
    subagent" here would silence the whole op on a filesystem hiccup.
    """
    try:
        common_dir = git_common_dir(Path(repo_root))
    except Exception:
        return False
    marker = common_dir / "coordinator-sessions" / ".agents" / session_id / "em-session-id.txt"
    try:
        return marker.is_file()
    except OSError:
        return False


def _dispatch_rows(session_id: str, repo_root: str) -> list[tuple[str, str, int]]:
    try:
        common_dir = git_common_dir(Path(repo_root))
    except Exception:
        return []
    dispatch_file = common_dir / "coordinator-sessions" / session_id / "dispatched-agents.txt"
    try:
        with open(dispatch_file, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    rows: list[tuple[str, str, int]] = []
    for raw_line in lines:
        line = raw_line.rstrip("\n").rstrip("\r")
        parts = line.split("\t")
        agent_id_row = parts[0] if len(parts) > 0 else ""
        model = parts[1] if len(parts) > 1 else ""
        dispatched_at_str = parts[3] if len(parts) > 3 else ""
        if not agent_id_row or not dispatched_at_str.isdigit():
            continue
        dispatched_at = int(dispatched_at_str)
        if dispatched_at == 0:
            continue
        rows.append((agent_id_row, model, dispatched_at))
    return rows


def _session_has_in_flight_dispatch(payload: dict, session_id: str, repo_root: str) -> bool:
    """Return True iff this session has at least one dispatch plausibly still running.

    Legitimate-wait suppression: a turn-end with a live dispatch is a correct wait,
    not the failure this op targets. `dispatched-agents.txt` is APPEND-ONLY and
    carries no completion record of its own — a row's mere presence proves only
    "was ever dispatched," never "is still running" (confirmed live against a real
    session's tracker file: it retained rows for agents already returned, and one
    that had been explicitly stopped). A suppressor keyed on that file's non-emptiness
    would go permanently silent after an EM's first dispatch of the session — worse
    than the noise it would prevent, and silently so. This function never does that:
    the actual liveness read is per-row, from that row's OWN subagent transcript, via
    `subagent_arrival_check` — the exact op runtime-tripwire-em-check.py's own
    SUBAGENT-ARRIVAL-CHECK section already uses to suppress its overrun nudge on a
    confirmed "arrived", called here directly in-process (same package, no IPC
    round-trip) rather than authoring a second, divergent "is this still running"
    answer.

    Three-tier resolution per row, cheapest/hardest fact first:
        - `state == "arrived"` (a confirmed, positive completion signal read straight
          from that agent's own transcript) -> never counts as in-flight, regardless
          of how recently it was dispatched. This is what makes an agent that
          finished in 30 seconds stop suppressing immediately, rather than only after
          a fixed window elapses.
        - `state == "running"` (the not-yet-arrived shape — `_classify`'s DEFAULT
          bucket for any well-formed non-arrival record, NOT an affirmative
          liveness proof — see F7: an agent that died mid-tool also reads
          "running" and holds this state for the full window below, since nothing
          re-checks it) -> counts as in-flight until the row ages past
          `RUNTIME_TRIPWIRE_MAX_TRACK_MIN` (default 90 min) — a hard cap shared
          with the sibling script, not a per-model window; the direction is safe
          (over-suppression, not over-firing) even though the fact "running"
          encodes is weaker than the wording used to imply.
          (Review: eng-director/the Director of Engineering F7.)
        - `state == "unknown"` (no resolvable fact at all — e.g. a dispatch shape
          whose transcript never lands under the expected per-agent path, or was
          cleaned up after return; a real, named gap in `subagent_arrival_check`
          itself, not invented here) -> counts as in-flight only while the row is
          within its OWN per-model runtime threshold (`_runtime_threshold_minutes`;
          `RUNTIME_TRIPWIRE_OPUS_MIN`/`SONNET_MIN`/`HAIKU_MIN`, defaults 25/12/10 min)
          — a tighter bound than the 90-minute cap, because "unknown" is not
          evidence of anything, only elapsed time is.

    Honest limitation, stated rather than papered over: this is a heuristic standing
    in for a liveness fact the tracker does not record. Two consequences, both
    accepted: (a) an "unknown" row over-suppresses for up to its per-model window
    even if that agent actually finished seconds after dispatch — safe direction;
    (b) a "running" row that genuinely runs past `MAX_TRACK_MIN` stops suppressing
    and could be nudged mid-wait — unsafe direction, but bounded by this op's own
    fire-once sentinel. The correct long-term fix is a completion record in the
    dispatch tracker itself; revisit this heuristic if one lands. Do not build that
    record here — out of scope for this op.

    Never raises: an arrival-check failure for one row degrades that row to
    "unknown" (the elapsed-time-bounded tier above) rather than aborting the scan.

    `asyncio` is imported lazily here — see F2. This function (and the module's
    other lazy import in `_load_sizing_object`) is now reached only once a
    matching sizing-object has already been found, per the reordered guards in
    `op()`.
    """
    import asyncio

    try:
        max_track_minutes = int(
            os.environ.get(_MAX_TRACK_MIN_ENV, str(_DEFAULT_MAX_TRACK_MIN))
            or _DEFAULT_MAX_TRACK_MIN
        )
    except ValueError:
        max_track_minutes = _DEFAULT_MAX_TRACK_MIN

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str):
        transcript_path = ""

    now = int(time.time())
    for agent_id_row, model, dispatched_at in _dispatch_rows(session_id, repo_root):
        elapsed_min = (now - dispatched_at) // 60
        if elapsed_min >= max_track_minutes:
            continue

        try:
            result = asyncio.run(
                _get_arrival_check()(
                    {"transcript_path": transcript_path, "agent_id": agent_id_row}
                )
            )
        except Exception:
            result = None
        state = result.get("state") if isinstance(result, dict) else None

        if state == "arrived":
            continue
        if state == "running":
            return True

        if elapsed_min < _runtime_threshold_minutes(model):
            return True
    return False


def _final_message_text(payload: dict) -> str:
    supplied = payload.get("last_assistant_message")
    if isinstance(supplied, str) and supplied.strip():
        return supplied
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return ""
    try:
        return _last_assistant_text(transcript_path)
    except Exception:
        return ""


def _text_trips_tell(text: str, referent_re: "re.Pattern[str]" = _ROUTE_REFERENT_RE) -> bool:
    """Return True iff `text` carries a live forward-intent tell co-occurring
    with a route referent.

    `referent_re` defaults to the sizing->room seam's own `_ROUTE_REFERENT_RE`
    but is a parameter (not hardcoded) precisely so the plan->execute-plan
    seam can reuse this same sentence-bounded co-occurrence helper with its
    own referent vocabulary (`_execute_plan_referent_regex`) rather than a
    second, near-duplicate co-occurrence function.

    A single positive-match gate, deliberately with no suppressor of any kind: a
    completion report ("fixed the parser, committed, pushed, tests green") is
    already a correct non-fire because it carries no forward-intent tell, so the
    gate never opens for it — nothing else is needed to exclude it. A message
    carrying BOTH a completion report AND a genuine forward-intent tell still fires,
    with no precedence rule required, because there is only one check and that
    message trips it. See the module docstring's "Text half" section for why this
    no-suppressor design is a STRONGER discharge of the cited lesson
    (`state/lessons/2026-07-28-a-detector-s-suppressor-must-not-key-on-c91c411f46ed.yaml`,
    DoE-claude repo) than a correctly-ordered suppressor would be.

    The tell alone is not enough (F4): a route referent (the resolved route's
    own skill name or route noun) must appear in the same sentence as the tell
    (bounded to a ~120-char window either side, for a long unpunctuated
    sentence) — this is what excludes route-agnostic forward-intent prose in a
    legitimate PM-question stop ("Next I'll need your call on whether shipped
    belongs in the enum.") while still firing on the true incident shape
    ("Taking it into coordinator:plan now."). Curly apostrophes (U+2019,
    U+02BC — this harness's own rendering commonly emits them) are normalized
    to ASCII before matching (F3), since the raw pattern only matched an ASCII
    `'`.
    """
    if not text:
        return False
    normalized = _CURLY_APOSTROPHE_RE.sub("'", text)
    tell_match = _FORWARD_INTENT_RE.search(normalized)
    if not tell_match:
        return False
    sent_start, sent_end = _sentence_bounds(normalized, tell_match.start())
    win_start = max(sent_start, tell_match.start() - _ROUTE_REFERENT_WINDOW_CHARS)
    win_end = min(sent_end, tell_match.end() + _ROUTE_REFERENT_WINDOW_CHARS)
    window = normalized[win_start:win_end]
    return bool(_ROUTE_REFERENT_RE.search(window))


def _room_invoked(payload: dict, session_id: str, repo_root: str, route: str) -> bool:
    if route == "dispatch":
        return _session_has_dispatched(session_id, repo_root)

    skill_name = _SKILL_BY_ROUTE.get(route)
    if skill_name is None:
        return False
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    try:
        return _skill_invoked(transcript_path, frozenset({skill_name}))
    except Exception:
        return False


def _build_message(route: str, sizing_path: str) -> str:
    action = "invoke coordinator:plan" if route in _SKILL_BY_ROUTE else "dispatch it"
    return _NUDGE_MESSAGE_TEMPLATE.format(route=route, action=action, sizing_path=sizing_path)


def _build_plan_message(rel_path: str, status: object) -> str:
    return _EXECUTE_PLAN_NUDGE_MESSAGE_TEMPLATE.format(plan_path=rel_path, status=status)


def _find_sizing_candidate(session_id: str, repo_root: str) -> tuple[str, dict] | None:
    touched_sizing_paths = _session_touched_sizing_files(session_id, repo_root)
    if not touched_sizing_paths:
        return None
    for rel_path in sorted(touched_sizing_paths):
        obj = _load_sizing_object(repo_root, rel_path)
        if obj is not None and _matches_criteria(obj):
            return rel_path, obj
    return None


def _find_plan_candidate(session_id: str, repo_root: str) -> tuple[str, dict] | None:
    touched_plan_paths = _session_touched_plan_files(session_id, repo_root)
    if not touched_plan_paths:
        return None
    for rel_path in sorted(touched_plan_paths):
        fm = _load_plan_frontmatter(repo_root, rel_path)
        if fm is not None and _plan_execution_authorized_and_active(fm):
            return rel_path, fm
    return None


def op(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("stop_hook_active"):
        return None
    if payload.get("agent_id"):
        return None

    if os.environ.get("COORDINATOR_UNROUTED_SIZING_NUDGE_OFF") == "1":
        return None

    sentinel = _sentinel_path(payload)
    if sentinel and os.path.exists(sentinel):
        return None

    repo_root = _repo_root(payload)
    if repo_root is None:
        return None

    raw_session_id = payload.get("session_id")
    if not isinstance(raw_session_id, str) or not raw_session_id.strip():
        return None

    sizing_candidate = _find_sizing_candidate(raw_session_id, repo_root)
    plan_candidate = None
    if sizing_candidate is None:
        plan_candidate = _find_plan_candidate(raw_session_id, repo_root)
    if sizing_candidate is None and plan_candidate is None:
        return None

    if _is_subagent_session(raw_session_id, repo_root):
        return None

    if _session_has_in_flight_dispatch(payload, raw_session_id, repo_root):
        return None

    if sizing_candidate is not None:
        rel_path, obj = sizing_candidate
        route = obj["route"]

        if _room_invoked(payload, raw_session_id, repo_root, route):
            return None

        if not _text_trips_tell(_final_message_text(payload)):
            return None

        if not _claim_fire(sentinel):
            return None

        message = _build_message(route, rel_path)
    else:
        rel_path, fm = plan_candidate

        if _execute_plan_invoked(payload):
            return None

        referent_re = _execute_plan_referent_regex(rel_path)
        if not _text_trips_tell(_final_message_text(payload), referent_re):
            return None

        if not _claim_fire(sentinel):
            return None

        message = _build_plan_message(rel_path, fm.get("status"))

    _, has_true_sid = _session_key(payload)
    if not has_true_sid:
        message += (
            "\n[nudge] (sentinel is invocation-scoped: no session_id was"
            " present, so this nudge may repeat.)\n"
        )
    envelope = context_only("Stop", message)
    return {"message": envelope["hookSpecificOutput"]["additionalContext"]}


if __name__ == "__main__":  # pragma: no cover - manual probe path
    result = op(json.load(sys.stdin))
    if result:
        print(result)
