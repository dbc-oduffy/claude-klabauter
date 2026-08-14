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
from coordinator_core.session.scope import parse_touch_event

# `asyncio`, `yaml`, and `subagent_arrival_check._handler` are imported LAZILY
# (inside the functions that need them) rather than at module load — see F2:
# those imports cost ~44ms combined and were previously paid on every turn-end
# of every EM session, including the overwhelmingly common case where no
# sizing-object was written this session at all. `_arrival_check` starts as
# None and is populated on first use by `_get_arrival_check()`; kept as a
# module-level name (rather than a purely local import) so tests can still
# monkeypatch `m._arrival_check` directly.
# (Review: eng-director/the Director of Engineering F2.)
_arrival_check = None


def _get_arrival_check():
    """Return `subagent_arrival_check._handler`, importing it on first use.

    Module-level caching keeps the lazy import a one-time cost per process,
    while still allowing tests to monkeypatch `_arrival_check` directly —
    once it is non-None (real or a test double), this returns it as-is.
    """
    global _arrival_check
    if _arrival_check is None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        _arrival_check = _handler
    return _arrival_check

# ---------------------------------------------------------------------------
# Sizing-object match criteria — all four must hold (schema:
# coordinator/schemas/sizing-object.schema.json, DoE-claude repo).
# ---------------------------------------------------------------------------

# Membership rule, not a fixed count: routes whose room an EM can enter WITHOUT a
# PM utterance. `goal-setting` and `roadmap` are excluded because DoE-claude's
# `coordinator/skills/{goal-setting,roadmap-planning}/SKILL.md` frontmatter marks
# each `description: "PM-GATED. ..."` — see the module docstring's Negative-spec
# section for the full account, including why "PM-gated" is not the right blanket
# label for every excluded route (`shape` is PM-collaborative, not frontmatter-
# gated) and the caveat that this rule's truth for goal-setting/roadmap rests on
# another repo's frontmatter, unpinnable by any test here.
_ROUTABLE_ROUTES = frozenset({"plan", "spec-dispatch", "dispatch"})

_SIZING_PATH_RE = re.compile(r"^state/sizings/[^/]+\.ya?ml$")

# The appetite<->estimate divergence signal. `coordinator_core.sizing_assemble.
# route()` emits this detent and leaves `fork` null; `fork` is filled later, by
# the sizing skill, once the PM has picked. So THIS is the field to read for
# "is an appetite fork open" — never `fork`'s nullity, which is null on both
# sides of that question. Kept as a named constant so the coupling to
# `sizing_assemble.DETENT_ENUM` is greppable from either side.
_APPETITE_DIVERGENCE_DETENT = "appetite_exceeded"

# The post-size, M+ open-appetite-question halt. `coordinator_core.sizing_assemble.
# route()` emits this detent (appetite absent, resized t-shirt M/L/XL) and leaves
# `fork` null — the PM has not yet answered the open "shall we go with that or
# split/cut/what's up?" question. Same shape as `_APPETITE_DIVERGENCE_DETENT`
# above: read the DETENT, never `fork`'s nullity. Kept as a named constant so the
# coupling to `sizing_assemble.DETENT_ENUM` is greppable from either side.
_POST_SIZE_PROMPT_DETENT = "post_size_prompt_pending"

# route -> the coordinator:plan skill both plan-shaped routes resolve to.
_SKILL_BY_ROUTE = {
    "plan": "coordinator:plan",
    "spec-dispatch": "coordinator:plan",
}

# ---------------------------------------------------------------------------
# seam: plan->execute-plan — a SEPARATE evaluator from the sizing->room seam
# above (not a member of `_ROUTABLE_ROUTES`; see the boundary test
# `test_execute_plan_is_never_a_routable_route` and the negative-spec in the
# module docstring). Only ever consulted once `execution_authorized_by` is
# present on the candidate plan's frontmatter — see
# `_plan_execution_authorized_and_active`.
# ---------------------------------------------------------------------------

_PLAN_PATH_RE = re.compile(r"^docs/plans/[^/]+\.md$")

# Statuses this seam treats as "authorized and still in flight" — the ONLY
# statuses that can reach a fire. Anything else (an unrecognised value such as
# `shipped`, which appears on 9 real plans but is absent from
# plan.schema.json's status enum, every terminal status, or a missing/blank
# status) defaults to silence by simply not being a member of this set; no
# separate unknown-status branch is needed because membership-testing an
# allow-list already IS that branch.
_PLAN_ROUTABLE_STATUSES = frozenset({"approved", "executing"})

_EXECUTE_PLAN_SKILL = "coordinator:execute-plan"

# Route-referent base vocabulary for the plan->execute-plan seam's text half.
# The specific plan's own slug is appended per-candidate (see
# `_execute_plan_referent_regex`) so the co-occurrence check also recognises
# the plan being named by its filename stem, not only by skill/route nouns.
_EXECUTE_PLAN_REFERENT_BASE = r"coordinator:execute-plan|execute-plan|execute the plan"

_TAIL_WINDOW_BYTES = 512_000

_MAX_TRACK_MIN_ENV = "RUNTIME_TRIPWIRE_MAX_TRACK_MIN"
_DEFAULT_MAX_TRACK_MIN = 90

# Per-model runtime-threshold env vars — same names and same defaults as
# runtime-tripwire-em-check.py's `_runtime_threshold_minutes` (DoE-claude repo), so
# the two surfaces cannot silently drift apart on what "still within a plausible
# runtime" means for a given model.
_OPUS_MIN_ENV = "RUNTIME_TRIPWIRE_OPUS_MIN"
_SONNET_MIN_ENV = "RUNTIME_TRIPWIRE_SONNET_MIN"
_HAIKU_MIN_ENV = "RUNTIME_TRIPWIRE_HAIKU_MIN"
_DEFAULT_OPUS_MIN = 25
_DEFAULT_SONNET_MIN = 12
_DEFAULT_HAIKU_MIN = 10


def _runtime_threshold_minutes(model: str) -> int:
    """Per-model plausible-runtime threshold, in minutes. Unknown/empty -> Opus default.

    Byte-faithful port of runtime-tripwire-em-check.py's own `_runtime_threshold_minutes`
    (same env vars, same defaults, same 1M-context-variant and family-substring
    matching) — kept as a literal port, not a paraphrase, so the two surfaces answer
    "how long is this model plausibly still working" identically.
    """
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

# ---------------------------------------------------------------------------
# Text half — a single POSITIVE forward-intent gate, no suppressor. A completion
# report is already excluded on its own (it carries no forward-intent tell, so the
# gate simply never opens for it) — see module docstring's "Text half" section for
# why no suppressor is needed, and why that is a stronger discharge of the cited
# lesson than a correctly-ordered suppressor would be.
#
# The tell alone is NOT sufficient (F4): it must co-occur, in the same sentence
# (bounded to a ~120-char window either side, for a long unpunctuated sentence),
# with a ROUTE REFERENT — the resolved route's own skill name or route noun. A
# forward-intent phrase with no route referent nearby is route-agnostic prose
# ("Next I'll need your call on X") that a legitimate PM-question stop can
# contain just as easily as the incident this op targets.
# (Review: eng-director/the Director of Engineering F3 + F4 — probed live: 11/12 realistic incident
# phrasings missed the old regex, and 3/5 realistic legitimate-stop phrasings
# fired it; both are one precision story, fixed together.)
# ---------------------------------------------------------------------------

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

# Route referent — the resolved route's own skill name or route noun. Required
# to co-occur with a forward-intent tell before this op fires at all (F4).
_ROUTE_REFERENT_RE = re.compile(
    r"coordinator:plan|coordinator:sizing|\bplan\b|spec-dispatch|\bdispatch\b|\bsizing\b",
    re.IGNORECASE,
)

_SENTENCE_END_RE = re.compile(r"[.!?]")

_ROUTE_REFERENT_WINDOW_CHARS = 120


_EXECUTE_PLAN_REFERENT_WINDOW_CHARS = _ROUTE_REFERENT_WINDOW_CHARS


def _execute_plan_referent_regex(rel_path: str) -> re.Pattern:
    """Return the route-referent regex for one candidate plan.

    Base vocabulary (`coordinator:execute-plan`, `execute-plan`, `execute the
    plan`) plus the candidate's own filename slug (e.g.
    `2026-07-31-narrate-then-stop-discharge` for
    `docs/plans/2026-07-31-narrate-then-stop-discharge.md`), so a tell naming
    the plan by slug also co-occurrence-matches. Built per-candidate rather
    than as a module-level constant because the slug is candidate-specific.
    """
    slug = Path(rel_path).stem
    pattern = _EXECUTE_PLAN_REFERENT_BASE
    if slug:
        pattern = f"{pattern}|{re.escape(slug)}"
    return re.compile(pattern, re.IGNORECASE)


def _sentence_bounds(text: str, pos: int) -> tuple[int, int]:
    """Return the (start, end) offsets of the sentence in `text` containing `pos`.

    Sentence boundaries are `.`, `!`, `?`. Text with no such punctuation is one
    sentence spanning the whole string.
    """
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
    """Return (filesystem-safe session discriminator, whether a true session_id was present).

    Mirrors nudge_harness_directive_dispatch._session_key verbatim — same fallback
    shape, same F7 degradation signal.
    """
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid.strip())
        if safe:
            return safe, True
    return f"pid-{os.getpid()}", False


def _repo_root(payload: dict) -> str | None:
    """Return the repo root directory (the one holding a `.git` entry), or None.

    Mirrors nudge_harness_directive_dispatch._repo_root verbatim.
    """
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
    """Return the real git directory for a `.git` path, or None if unusable.

    Mirrors nudge_harness_directive_dispatch._resolve_git_dir verbatim (worktree/
    submodule `.git`-FILE gitdir: pointer resolution).
    """
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
    """Return the once-per-session sentinel path, or None when no repo root is resolvable.

    Mirrors nudge_harness_directive_dispatch._sentinel_path verbatim, own filename.
    """
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
    """Atomically claim the once-per-session fire slot; return True iff this call won it.

    Mirrors nudge_harness_directive_dispatch._claim_fire verbatim.
    """
    if not sentinel:
        return True
    try:
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        with open(sentinel, "x", encoding="utf-8") as fh:
            fh.write("1")
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def _tail_text(path: str, max_bytes: int = _TAIL_WINDOW_BYTES) -> str:
    """Return the last `max_bytes` of `path`, decoded, or "" on any I/O failure.

    Same bounded binary-seek technique as
    nudge_harness_directive_dispatch.last_assistant_text: seek to the tail window,
    discard the partial line the seek lands inside, decode with errors="replace".
    Deliberately bounded — never reads a multi-MB transcript whole.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # discard the partial line the seek landed inside
            raw = fh.read()
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _session_has_dispatched(session_id: str, repo_root: str) -> bool:
    """Return True iff this session has direct on-disk evidence of an Agent dispatch.

    Same evidence file as nudge_harness_directive_dispatch._session_has_dispatched:
    track_dispatched_agents.py's `dispatched-agents.txt`, keyed by the RAW session_id
    (that writer does not sanitize the directory name — see track_dispatched_agents.py
    and track_touched_files.py, both of which use `session_id` unmodified as the
    directory component). Resolved via git_common_dir so a worktree session's
    bookkeeping is read from the MAIN worktree's `.git`, matching the writer's own
    resolution.

    Fail-open by design: any ambiguity (unresolvable common dir, unreadable file)
    returns False — i.e. the `route: dispatch` nudge still fires. Suppressing a
    genuine nudge on an unreadable file is the worse failure.
    """
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

    Shared by BOTH seams (sizing->room and plan->execute-plan) so `touched.txt` is read
    once per turn-end rather than once per seam — `_session_touched_sizing_files` and
    `_session_touched_plan_files` are thin path-regex filters over this same read.
    """
    try:
        common_dir = git_common_dir(Path(repo_root))
    except Exception:
        return []
    touched_file = common_dir / "coordinator-sessions" / session_id / "touched.txt"
    try:
        with open(touched_file, "r", encoding="utf-8", errors="replace") as fh:
            return [parse_touch_event(ln.rstrip("\n").rstrip("\r"))[2] for ln in fh]
    except OSError:
        return []


def _session_touched_sizing_files(session_id: str, repo_root: str) -> list[str]:
    """Return the repo-relative state/sizings/*.yaml paths touched.txt records for this session."""
    return [ln for ln in _session_touched_lines(session_id, repo_root) if _SIZING_PATH_RE.match(ln)]


def _session_touched_plan_files(session_id: str, repo_root: str) -> list[str]:
    """Return the repo-relative docs/plans/*.md paths touched.txt records for this session.

    Same evidence mechanism as `_session_touched_sizing_files` — the plan->execute-plan
    seam's own near-free state-gate (checked before any plan frontmatter is even opened).
    """
    return [ln for ln in _session_touched_lines(session_id, repo_root) if _PLAN_PATH_RE.match(ln)]


def _load_sizing_object(repo_root: str, rel_path: str) -> dict | None:
    """Parse `<repo_root>/<rel_path>` as YAML and return it, or None on any failure.

    None covers: file absent/unreadable, malformed YAML, or a parsed value that is
    not a dict — every one of these means "cannot prove the exemptions don't apply",
    which this op treats as silence (see module docstring).

    `yaml` is imported lazily here rather than at module load — see F2, this is
    the near-free-guard-first reorder's other half.
    """
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
    """Parse the YAML frontmatter block of `<repo_root>/<rel_path>` and return it, or None.

    None covers: file absent/unreadable, no leading `---` frontmatter fence, an
    unterminated fence, malformed YAML, or a parsed value that is not a dict —
    every one of these reads as "cannot prove `execution_authorized_by` is
    present," which this seam treats as silence, matching
    `_load_sizing_object`'s own fail-toward-silence posture on its hard
    exemption reads (see module docstring).

    Unlike a pure-YAML sizing-object, a plan file is Markdown with a YAML
    frontmatter block delimited by a `---` line, the block itself, and a
    second `---` line — this extracts only that block before handing it to
    the SAME lazy `yaml.safe_load` the sizing seam already uses (F2's lazy-import
    posture is reused, not re-derived).
    """
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
    """Return True iff there is evidence `coordinator:execute-plan` was invoked this session.

    Same evidence and technique as the sizing->room seam's `_room_invoked`
    plan/spec-dispatch branch: a `Skill` tool_use naming the target skill in
    the transcript tail window, via the shared `_skill_invoked` helper — no
    second transcript-scanning function is authored for this seam.
    """
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    try:
        return _skill_invoked(transcript_path, frozenset({_EXECUTE_PLAN_SKILL}))
    except Exception:
        # Advisory op — a transcript-parse surprise must never raise. Treated as
        # "no evidence found" (fires), matching _room_invoked's own asymmetry.
        return False


def _skill_invoked(transcript_path: str, target_skills: frozenset[str]) -> bool:
    """Return True iff a `Skill` tool_use naming one of `target_skills` appears in the
    tail window of `transcript_path`.

    Scans every assistant entry in the tail window (not only the final one) for a
    `tool_use` content block named `Skill` whose `input.skill` matches. Any parse
    failure on a line is skipped, never raised.
    """
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
    """Parse `dispatched-agents.txt` into (agent_id, model, dispatched_at) rows.

    Same tab-separated shape and same tolerant-parse rules as
    runtime-tripwire-em-check.py's own read of this file: a missing/non-numeric
    `dispatched_at` (legacy record) or a zero timestamp is skipped, never raised on.
    Any I/O failure (absent file, unresolvable common dir) returns [].
    """
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
            continue  # aged past the hard staleness cap -- never tracked as in-flight

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
            continue  # confirmed done -- never in-flight, regardless of elapsed time
        if state == "running":
            return True  # confirmed live -- in-flight until the hard cap above

        # "unknown" (or an arrival-check failure, folded into the same tier): no
        # positive signal either way -- bound to the tighter per-model window.
        if elapsed_min < _runtime_threshold_minutes(model):
            return True
    return False


def _final_message_text(payload: dict) -> str:
    """Return the text of the turn's final assistant message, or "".

    Prefers the harness-supplied `last_assistant_message`; falls back to
    `nudge_harness_directive_dispatch.last_assistant_text`'s own bounded tail-read
    over `transcript_path` — imported and reused directly rather than re-derived,
    matching that sibling's own documented preference for this exact fallback shape.
    """
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
    """Return True iff there is evidence the route's own room was entered this session.

    `plan` / `spec-dispatch` -> a Skill tool_use naming coordinator:plan in the
    transcript tail window. `dispatch` -> dispatched-agents.txt evidence, the same
    signal nudge_harness_directive_dispatch._session_has_dispatched reads.
    """
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
        # Advisory op — a transcript-parse surprise must never raise. Treated as
        # "no evidence found" (fires), per the module docstring's stated asymmetry.
        return False


def _build_message(route: str, sizing_path: str) -> str:
    action = "invoke coordinator:plan" if route in _SKILL_BY_ROUTE else "dispatch it"
    return _NUDGE_MESSAGE_TEMPLATE.format(route=route, action=action, sizing_path=sizing_path)


def _build_plan_message(rel_path: str, status: object) -> str:
    return _EXECUTE_PLAN_NUDGE_MESSAGE_TEMPLATE.format(plan_path=rel_path, status=status)


def _find_sizing_candidate(session_id: str, repo_root: str) -> tuple[str, dict] | None:
    """Return the first (rel_path, sizing-object) this session touched that matches
    the sizing->room seam's criteria, or None. Near-free `touched.txt` gate first
    (F2) — no YAML is opened at all when nothing was touched under state/sizings/.
    """
    touched_sizing_paths = _session_touched_sizing_files(session_id, repo_root)
    if not touched_sizing_paths:
        return None  # criterion 1: express-lane / no sizing-object this session
    for rel_path in sorted(touched_sizing_paths):
        obj = _load_sizing_object(repo_root, rel_path)
        if obj is not None and _matches_criteria(obj):
            return rel_path, obj
    return None


def _find_plan_candidate(session_id: str, repo_root: str) -> tuple[str, dict] | None:
    """Return the first (rel_path, plan-frontmatter) this session touched that is
    execution-authorized and still active (seam: plan->execute-plan), or None.
    Near-free `touched.txt` gate first, mirroring `_find_sizing_candidate`.
    """
    touched_plan_paths = _session_touched_plan_files(session_id, repo_root)
    if not touched_plan_paths:
        return None
    for rel_path in sorted(touched_plan_paths):
        fm = _load_plan_frontmatter(repo_root, rel_path)
        if fm is not None and _plan_execution_authorized_and_active(fm):
            return rel_path, fm
    return None


def op(payload: dict) -> dict | None:
    """Stop advisory: nudge an EM that resolved a route (sizing->room, or an
    already execution-authorized plan->execute-plan) and stopped without
    entering it.

    Returns ``{"message": <str>}`` when either seam should fire, ``None``
    otherwise. Transport is owned by the caller (the DoE-resident stdin/stderr
    shim) — this op decides only *whether* to speak, matching
    nudge_harness_directive_dispatch's own transport-seam split.

    Both seams share the universal guards below (env switch, stop_hook_active,
    agent_id, the fire-once sentinel, EM-discriminator, in-flight-dispatch) and
    the ONE shared fire-once sentinel slot — see "Seam naming" in the module
    docstring for why a shared slot was chosen over one-per-seam. The
    sizing->room seam is checked first (the original, still the more common
    case); the plan->execute-plan seam is checked only if the sizing seam
    found no candidate at all, never as a fallback after a found-but-suppressed
    sizing candidate (each turn-end fires for at most one seam).

    Never raises on well-formed OR malformed input.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("stop_hook_active"):
        return None  # this Stop came from a hook block — re-firing would loop
    if payload.get("agent_id"):
        return None  # a dispatched subagent holds no dispatch authority

    if os.environ.get("COORDINATOR_UNROUTED_SIZING_NUDGE_OFF") == "1":
        return None

    sentinel = _sentinel_path(payload)
    if sentinel and os.path.exists(sentinel):
        return None  # fast-path skip; the real (atomic) gate is _claim_fire below

    repo_root = _repo_root(payload)
    if repo_root is None:
        return None  # cannot prove a sizing-object/plan was written this session

    raw_session_id = payload.get("session_id")
    if not isinstance(raw_session_id, str) or not raw_session_id.strip():
        return None  # no session_id -> no way to key touched.txt / dispatched-agents.txt

    # Near-free state reads FIRST (F2): a small file read plus a compiled regex
    # scan per seam, cheaper than the subagent/in-flight checks below, which each
    # pay for an event loop, a transcript tail-read, or a lazy asyncio/yaml
    # import. The overwhelmingly common case (neither seam has a candidate this
    # session) exits here without ever touching those heavier guards.
    sizing_candidate = _find_sizing_candidate(raw_session_id, repo_root)
    plan_candidate = None
    if sizing_candidate is None:
        plan_candidate = _find_plan_candidate(raw_session_id, repo_root)
    if sizing_candidate is None and plan_candidate is None:
        return None

    # Only now, with a genuinely matching candidate on disk (either seam), pay
    # for the subagent/in-flight checks (event loop + transcript reads) — see
    # F2. Shared across both seams: an EM-discriminator or in-flight-dispatch
    # answer does not depend on which seam found the candidate.
    if _is_subagent_session(raw_session_id, repo_root):
        return None  # house-authoritative EM-discriminator: this is a subagent's own Stop

    if _session_has_in_flight_dispatch(payload, raw_session_id, repo_root):
        return None  # legitimate wait: a dispatch is still running this session

    if sizing_candidate is not None:
        rel_path, obj = sizing_candidate
        route = obj["route"]

        if _room_invoked(payload, raw_session_id, repo_root, route):
            return None

        if not _text_trips_tell(_final_message_text(payload)):
            return None  # state alone is not enough -- no live forward-intent tell this turn

        if not _claim_fire(sentinel):
            return None  # lost the race to another concurrent Stop for this session

        message = _build_message(route, rel_path)
    else:
        rel_path, fm = plan_candidate

        if _execute_plan_invoked(payload):
            return None  # coordinator:execute-plan was already invoked this session

        referent_re = _execute_plan_referent_regex(rel_path)
        if not _text_trips_tell(_final_message_text(payload), referent_re):
            return None  # state alone is not enough -- no live forward-intent tell this turn

        if not _claim_fire(sentinel):
            return None  # lost the race to another concurrent Stop for this session

        message = _build_plan_message(rel_path, fm.get("status"))

    _, has_true_sid = _session_key(payload)
    if not has_true_sid:
        message += (
            "\n[nudge] (sentinel is invocation-scoped: no session_id was"
            " present, so this nudge may repeat.)\n"
        )
    # Routed through the shared envelope-builder chokepoint (coordinator_core.
    # _hook_envelope) so this op's message is captured by the C6 instrumentation
    # seam, same as every other prose-carrying hook op. op()'s own external
    # contract ({"message": <str>}, consumed by the DoE-resident stdin/stdout
    # shim per this function's docstring) is unchanged: the builder's
    # additionalContext is extracted back out immediately, so the returned
    # message text is byte-identical to before this routing — no envelope
    # shape is exposed past this point.
    envelope = context_only("Stop", message)
    return {"message": envelope["hookSpecificOutput"]["additionalContext"]}


if __name__ == "__main__":  # pragma: no cover - manual probe path
    result = op(json.load(sys.stdin))
    if result:
        print(result)
