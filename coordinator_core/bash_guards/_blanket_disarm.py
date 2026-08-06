"""coordinator_core.bash_guards._blanket_disarm -- machine-scoped,
time-boxed disarm switch for the whole bash-guard suite, along a THREE-VALUE
scope axis (time / session / machine-total).

WHY THIS EXISTS. Every bash-guard deny message today ends with some variant
of "To bypass: export COORDINATOR_ALLOW_X=1". That instruction is
UNREACHABLE by anything running inside a session: the PreToolUse(Bash) hook
is spawned as a fresh subprocess per tool-call event and reads only that
subprocess's own `os.environ` (see
`coordinator_core/bash_guards/tests/test_override_unreachability_boundary.py`
for the mechanism, pinned as a security boundary, not merely documented). An
`export` from a prior Bash call sets a different, short-lived process; an
`export` INSIDE the candidate command is never executed at all -- it is just
characters this pipeline analyses, never shell-interprets. So every guard
advertises an escape hatch nobody inside the session can pull.

A FILE is the correct mechanism instead, for exactly the reason the
workflow model-guard's own error text already states: a marker file is
reachable from inside a live session (the operator can create one via the
`!`-prefixed prompt, which bypasses PreToolUse hooks entirely, per
`~/.claude` doctrine "The `!` prefix bypasses PreToolUse hooks"), whereas an
env var set from inside a Bash call is not.

MARKER LOCATION -- machine-scoped, not repo-scoped. Resolved via
`coordinator_core._settings_home.settings_home()` (COORDINATOR_SETTINGS_HOME
override, else CLAUDE_HOME, else the platform home + `.coordinator-claude-
settings` -- see that module's own docstring). Reusing this resolver rather
than hand-rolling a second `os.environ.get("COORDINATOR_SETTINGS_HOME") or
...` chain is deliberate: this repo has already been bitten twice by
sibling resolvers that skipped the Windows (`USERPROFILE`) rung by hand-
rolling their own chain (see that module's own "bare_home_or_chain" note).
The marker lives at `<settings-home>/.coordinator-bash-guards-disarmed`,
mirroring the precedent shape of `~/.claude/.coordinator-hooks-disabled`
(`Since:` / `Expires:` / `Reason:` key-value lines, one per line, human-
editable) -- but with a `Scope:` axis (see below) and, for two of its three
values, a MANDATORY, machine-parseable `Expires:` timestamp (the precedent
marker's own `Expires:` is prose-only and never actually enforced by any
reader).

THE SCOPE AXIS -- three explicit, enumerated values, never inferred from
which fields happen to be present (inferring intent from field presence is
exactly how a malformed marker becomes an accidental machine-wide
off-switch -- see FAIL-CLOSED CONDITIONS below):

  - `Scope: time` -- machine-wide, for a bounded period. Applies to EVERY
    caller on this machine, EM or dispatched subagent alike, for as long as
    the (mandatory, capped) `Since`..`Expires` window holds. This is the
    original, single-scope version of this module.
  - `Scope: session` -- applies to exactly ONE session, named by a
    mandatory `Session:` field holding that session's `session_id` (the
    same `session_id` every guard in this package already receives on its
    payload -- see e.g. `guard_head_tail_rewrite.check_head_tail_plumbing_
    rewrite(cmd, session_id)`). Still carries the mandatory, capped
    `Since`..`Expires` window. **No-inherit to dispatched subagents of that
    session** -- see NO-INHERIT below.
  - `Scope: machine-total` -- the default flips to OFF, standing, no
    expiry required. **EM-only audience, unconditionally** -- see
    AUDIENCE NARROWING below. This is the one scope that inverts the
    module's own fail-closed default into fail-OPEN, so it is the one
    scope narrowed to the smallest possible caller set to compensate.

An absent, unrecognised, or misspelled `Scope:` value disarms NOTHING --
there is no fallback/default scope. See FAIL-CLOSED CONDITIONS.

AUDIENCE NARROWING -- WHY `machine-total` IS EM-ONLY, NOT BLANKET.
Flipping a guard suite's default to OFF standing, with no expiry, is
reckless unless narrowed by WHO it applies to, not just WHERE/WHEN. This
package's own ratified threat model treats the guarded caller as an EAGER
AGENT, not an adversary (see e.g. `block_subagent_destructive_action.py`'s
own module docstring) -- and an EM has a human PM watching its output in
real time, while a dispatched subagent does not. So `machine-total` disarms
ONLY the EM's own tool calls; a dispatched subagent stays fully guarded
regardless of this marker's presence. The EM/subagent discriminator is NOT
reinvented here -- it reuses `resolve_effective_types` (re-exported by
`coordinator_core.bash_guards._helpers`, itself a thin re-export of
`coordinator_core.subagent_sandbox.engine.resolve_effective_types`), the
SAME resolver every other identity-gated guard in this package already
calls. A caller is treated as "the EM" iff ALL THREE resolved legs
(`agent_id`, `agent_type`, `subagent_type`) are empty -- i.e. the payload
carries no dispatch back-pointer and no top-level `agent_type` at all,
which is the shape a main-loop EM tool call has and a dispatched subagent's
never does (see `resolve_effective_types`'s own docstring: `agent_type` is
populated for unnamed/foreground dispatch, `subagent_type` for named/
teammate dispatch via the back-pointer file). This module does not author a
second identity resolver -- it is explicitly the wrong place for one.

NO-INHERIT (session scope) -- OPEN QUESTION, BUILT CONSERVATIVE. Whether a
session-scoped disarm should extend to that session's OWN dispatched
subagents (which may carry the same `session_id`) is an open question put
to the PM as of this module's authorship; it is NOT yet answered. This
module is built for the conservative answer: **no-inherit** -- a session-
scoped marker disarms only the named session's EM-context tool calls, never
any subagent it dispatches, even when the subagent's `session_id` happens
to match. This is controlled by exactly ONE named constant,
`_SESSION_SCOPE_INHERITS_TO_DISPATCHED_SUBAGENTS` (currently `False`) --
flipping the PM's eventual answer to "yes, inherit" is a one-line edit to
that constant, not a re-shape of the branch that reads it.

MARKER FORMAT -- plain `Key: value` lines, one per line (same shape as
`~/.claude/.coordinator-hooks-disabled`):

    Scope: time
    Since: 2026-07-30T14:00:00Z
    Expires: 2026-07-30T18:00:00Z
    Reason: <free text -- why the suite is disarmed, for a human reading it later>

or, for a session-scoped marker, additionally:

    Scope: session
    Session: <the target session_id>
    Since: 2026-07-30T14:00:00Z
    Expires: 2026-07-30T18:00:00Z
    Reason: ...

or, for the standing EM-only switch:

    Scope: machine-total
    Since: 2026-07-30T14:00:00Z
    Reason: ...

`Since` and `Expires` (where required) MUST be ISO-8601 UTC timestamps (`Z`
suffix, or an explicit UTC offset -- see `_parse_utc_timestamp` for the
accepted grammar). `Reason` is free text and is never inspected for
correctness (an empty or missing `Reason` does not by itself deny the
disarm); this mirrors the precedent marker's already-established "text
explaining WHY" as a courtesy to the next reader, not a machine-checked
field.

FAIL-CLOSED, WITHOUT EXCEPTION -- structurally, not just by convention.
Every scope branch below has exactly ONE `return DisarmResult(active=True,
...)` statement, reached only after every applicable check for THAT scope
has passed; every other path returns `active=False`. There is no
`try/except` whose `except` branch could grow an `else: return True`, and
no code path where an error, an ambiguity, or a partially-valid marker
results in disarm. The guards stay ON (or, for a subagent under a
`machine-total` marker, ALWAYS stay ON regardless of marker validity) when:

  - the marker is absent -- `Path.exists()` False;
  - the marker is present but unreadable (permission error, is-a-directory,
    any `OSError` on read) -- caught and treated as "no marker";
  - the marker is empty, or has no `Key: value` lines at all;
  - `Scope` is missing, blank, or not one of the three recognised literal
    values (`time` / `session` / `machine-total`) -- an unrecognised scope
    NEVER falls through to any permissive branch;
  - (scope `time`/`session`) `Since` or `Expires` is missing;
  - (scope `machine-total`) `Since` is missing (still required, for
    record-keeping, even though `Expires` is not);
  - any present `Since`/`Expires` is not a parseable ISO-8601 UTC
    timestamp;
  - any present `Expires` is earlier than or equal to the current UTC time
    (expired);
  - (scope `time`/`session` only) `Expires` is later than `Since` +
    `_MAX_DISARM_DURATION` (beyond the cap -- see EXPIRY IS CAPPED below;
    treated exactly as "expired", never clamped-and-allowed);
  - (scope `session`) `Session` is missing, or does not equal the calling
    payload's own `session_id`;
  - (scope `session`, no-inherit) the caller is a dispatched subagent, even
    when `Session` matches (see NO-INHERIT above);
  - (scope `machine-total`) the caller is a dispatched subagent, full stop
    -- this scope's fail-open behavior is unconditionally EM-only (see
    AUDIENCE NARROWING above); no marker content of any kind changes this.

EXPIRY IS MANDATORY for `time` and `session` scope, and CAPPED at
`_MAX_DISARM_DURATION = timedelta(hours=8)` -- roughly one working day.
Chosen because those two scopes exist for a bounded, supervised operation
(the PM sits down, disarms, does the thing, re-arms or lets it lapse) not
an ambient standing state; capping at "a work session" rather than, say, "a
week" means a marker some future PM forgets to delete self-expires well
before the next calendar day's session starts, so the blast radius of
"forgot to clean up" is at most the same working day, never a silently-
standing week-long hole. A marker whose `Since`..`Expires` span exceeds the
cap is treated as expired rather than silently truncated to the cap --
silently shortening a value the marker's author wrote would be a
surprising, hard-to-notice behavior; refusing it outright (fail closed) is
the same asymmetric posture every other guard in this package already uses.

`machine-total` scope is deliberately EXEMPT from both the mandatory-expiry
and the cap rule -- it is allowed to be standing (no `Expires` at all), and
this is exactly why it is the one scope narrowed to EM-only audience (see
AUDIENCE NARROWING). If a `machine-total` marker DOES carry an `Expires`,
it is still honoured (must not be in the past) but the 8-hour cap is not
applied to it -- a PM who chose to time-box even their standing switch gets
that choice respected literally, without this module inventing a second
cap value nobody asked for.

HOT-PATH CHEAP, PER-(SESSION, AUDIENCE) CACHE. This predicate is consulted
by every guard in the chain on every single Bash tool call -- including on
Windows, where a process spawn is expensive. The check itself is one
`Path.exists()` + a small text read, no subprocess, and cheap regardless;
the per-process cache below exists for the case where SEVERAL guards in one
dispatch chain each call `blanket_disarm_active()` while processing the
SAME PreToolUse event, which would otherwise re-stat and re-parse the
marker once per guard. The cache key is `(session_id, is_em)` rather than
no-key-at-all, because (unlike the earlier single-scope version of this
module) the verdict now genuinely depends on the calling context, not just
on marker content and wall-clock time. Caching per-process is still safe BY
CONSTRUCTION: the PreToolUse hook is spawned as a fresh subprocess for
every tool-call event (see "WHY THIS EXISTS" above, and
`test_override_unreachability_boundary.py`'s own mechanism note), and
`(session_id, is_em)` cannot change mid-event, so there is no "stale
across calls" failure mode to guard against -- the process itself never
outlives one event, and the context is fixed for that event's duration.

AGENT-CREATION MUST BE BLOCKABLE -- DESIGNED FOR, NOT WIRED, IN THIS TASK.
This marker is the same species of file as the two sentinels
`_sentinel_creation_guard.py` already protects
(`.coordinator-doctrine-edit-approved`,
`.coordinator-override-worktree-guard`): the PM creates it themselves (via
the `!`-prefixed prompt, which bypasses PreToolUse hooks entirely), and a
subagent must never be able to write it via Bash -- if a subagent could
`touch` or redirect into this marker, the entire guard suite becomes
self-disarmable by exactly the caller class it exists to constrain (and,
worse, a subagent forging a `Scope: machine-total` marker would defeat the
EM-only audience narrowing entirely by writing its own way around it).
Wiring a `SentinelCreationDetector(MARKER_BASENAME)` guard for this marker
is **explicitly deferred** to a follow-up task, because `_sentinel_
creation_guard.py` and `block_approval_sentinel_creation.py` are in a
concurrent row's edit scope in this dispatch wave. What THIS module exports
for that follow-up to consume without re-deriving it:

    MARKER_BASENAME  -- the exact basename to protect (mirrors the two
                        existing `_TARGET_BASENAME` constants' shape).
    marker_path()    -- resolves the FULL path via `settings_home()`, for a
                        guard that wants to log/report the concrete path
                        rather than only match a basename.

NEGATIVE-SPEC / FOLLOW-UP NOTE (do not "helpfully" wire this in the same
change that touches `_sentinel_creation_guard.py` or
`block_approval_sentinel_creation.py` -- both are mid-edit in a concurrent
row of this dispatch wave; landing a guard registration here would either
collide with or be silently overwritten by that row's own commit). The
follow-up is: instantiate `SentinelCreationDetector(MARKER_BASENAME)` the
same way `block_approval_sentinel_creation.py` instantiates its own
detector, wire a `check()` into `dispatch.py`'s guard chain, and give it a
`PRIORITY` ahead of `guard_offer_git_c` for the same short-circuit reason
`block_approval_sentinel_creation.py`'s own docstring documents under
"REGISTRATION ORDERING".

BAND-SCOPED SUPPRESSION -- WHY THIS EXISTS, AND WHY `confinement-deny` IS
NEVER SUPPRESSIBLE. `machine-total` scope's real motivating use case is not
"an operator silencing safety on a dev box" -- it is a platform-appropriate
DEFAULT for OSS coordinator consumers on macOS, where process spawns are
cheap and a large share of this suite's ADVISORY output is pure context
noise with no correctness benefit on that platform. That is legitimate
configuration, not a safety hole -- but it means "off" must never mean "ALL
guards off". So the marker also names WHICH band(s) of the guard chain it
suppresses, via a `Bands:` field, reusing `dispatch.py`'s own `GuardBand`
enum verbatim (`GuardBand.CONFINEMENT_DENY` / `.ADVISORY_REWRITE` /
`.PLATFORM_CONDITIONED_DENY`, string values `"confinement-deny"` /
`"advisory-rewrite"` / `"platform-conditioned-deny"`) rather than inventing
a second spelling of the same three names.

NEGATIVE SPEC -- `_SUPPRESSIBLE_BANDS` IS AN EXPLICIT ALLOWLIST, NOT A
DENYLIST OF ONE EXCLUDED BAND. The suppressible set is exactly
`{GuardBand.ADVISORY_REWRITE, GuardBand.PLATFORM_CONDITIONED_DENY}` --
spelled out as its own frozenset constant, not derived as "every band
except confinement-deny". This matters for what happens when a FOURTH band
is added to `dispatch.py` in the future: under an allowlist, that new band
is non-suppressible by default until someone deliberately adds it here;
under a denylist-of-one, it would silently inherit suppressibility the
moment it existed, with no one having decided that was safe. Do not
"simplify" this constant into `frozenset(b.value for b in GuardBand) -
{CONFINEMENT_DENY_VALUE}` -- that IS the denylist-of-one shape this
paragraph exists to rule out.

`GuardBand.CONFINEMENT_DENY` IS NEVER SUPPRESSIBLE, BY ANY SCOPE, INCLUDING
`machine-total` -- unconditionally, no marker content changes this. Its
guards (`--no-verify` bypass, destructive `rm`, `git clean`, worktree
creation, subagent-commit, the reviewer bash allowlist, and siblings) are
PLATFORM-INDEPENDENT CORRECTNESS PROTECTION: their cost is not "an
advisory nobody reads", it is "an agent forged a commit/deletion/worktree
its confinement was supposed to prevent" -- a defect that costs the same
whether the box is a fast Mac or a slow Windows VM, so there is no spawn-
cost tradeoff to weigh it against. A marker that NAMES `confinement-deny`
in its `Bands:` field is therefore treated as MALFORMED IN ITS ENTIRETY --
the whole marker is rejected (evaluates to inactive), not just the
`confinement-deny` token within it -- so a typo or a copy-paste that adds
`confinement-deny` alongside legitimately-suppressible bands cannot
"half-apply" and accidentally suppress the other named bands while merely
dropping the illegal one. See `_parse_suppressed_bands` for the mechanism.

`Bands:` FAILS CLOSED THE SAME WAY `Scope:` DOES. An absent, empty,
unrecognised, or PARTLY-unrecognised `Bands:` value suppresses NOTHING --
never "no bands named" silently reinterpreted as "all bands" or "the whole
suppressible set". Only an explicit, fully-recognised (and
`confinement-deny`-free) comma-separated list of band names suppresses
anything. A caller reading `DisarmResult.bands` therefore sees either
`None` (this marker's active-ness was never even reached, or its `Bands:`
field was itself malformed enough to reject the whole marker -- see
`confinement-deny` handling above) or a `frozenset` (possibly empty, in
every "absent/unrecognised" leg above) of the bands actually suppressed.
`disarm_covers_band()` is the forward-compatible query surface a future
per-guard band check should call rather than hand-deriving this logic a
second time.

MARKER FORMAT (with `Bands:` added):

    Scope: machine-total
    Since: 2026-07-30T14:00:00Z
    Bands: advisory-rewrite,platform-conditioned-deny
    Reason: macOS OSS default -- these advisories are noise on a fast box

M18 -- `active` MUST NOT LIE ABOUT AN INERT MARKER (2026-07-30, second
dispatch, same day). The FIRST version of this module's three
`_evaluate_*_scope` functions returned `active=True` whenever the
`Scope`/`Since`/`Expires`/audience checks all passed, REGARDLESS of
whether `Bands:` named anything suppressible. Probing the simplest marker
an operator would actually hand-write --

    Scope: machine-total
    Since: 2026-07-30T14:00:00Z
    (no Bands: line at all)

-- produced `active=True`, `detail="machine-total EM-only disarm active
(standing, no expiry)"`, and `disarm_covers_band(ANY_BAND) == False`. That
marker is well-formed and passes every check, and does NOTHING: an absent
`Bands:` field means "suppress nothing" (see "`Bands:` FAILS CLOSED THE
SAME WAY `Scope:` DOES" above -- that semantic is UNCHANGED by this fix),
so calling that state "active" is a false capability claim -- the exact
defect class ("This escape hatch doesn't do what it says") this whole
workstream exists to delete from guard messages, reappearing inside the
mechanism built to fix it.

The fix: each `_evaluate_*_scope` function now checks `bands` for
emptiness BEFORE returning its positive result, and returns `active=False`
with `_no_bands_named_detail(...)` (naming the missing `Bands:` line as
the remedy) when it is empty -- REGARDLESS of how cleanly every other
check passed. `blanket_disarm_active()`'s own docstring is narrowed to
document that `True` now requires a non-empty suppressed-bands set, and to
warn a future caller against reading its bare boolean as license to skip
any PARTICULAR guard (`disarm_covers_band(band, ...)` is the only correct
call for that). Nothing about `_parse_suppressed_bands`'s own semantics
changed -- an absent/blank/unrecognised `Bands:` field still means
"suppress nothing"; whether it SHOULD instead default to "suppress every
suppressible band" is a direction-class call for the PM (it widens default
blast radius) and is explicitly out of scope for this fix.

Spec backlink: this module was authored per an explicit PM-approved,
blanket-scope-over-per-guard dispatch brief (2026-07-30), widened mid-build
(same session) first into the three-value scope axis documented above in
"THE SCOPE AXIS", then into the band-scoped suppression axis documented in
"BAND-SCOPED SUPPRESSION". Companion precedent read before authoring:
`~/.claude/.coordinator-hooks-disabled` (the `Since:`/`Expires:`/`Reason:`
marker shape this format mirrors), `coordinator_core/bash_guards/_sentinel_
creation_guard.py` + `block_approval_sentinel_creation.py` (the
un-creatable-sentinel species this marker belongs to), and
`coordinator_core/bash_guards/dispatch.py`'s `GuardBand` enum (the
vocabulary `Bands:` reuses verbatim, per this session's row M1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Tuple, Union

from coordinator_core._settings_home import settings_home
from coordinator_core.bash_guards._helpers import resolve_effective_types
from coordinator_core.bash_guards.dispatch import GuardBand

#: The exact basename this module resolves and (eventually, per a follow-up
#: task -- see module docstring "AGENT-CREATION MUST BE BLOCKABLE") a
#: sentinel-creation guard will protect. Never relaxed to a substring/prefix
#: match, same posture as the two existing `_TARGET_BASENAME` constants this
#: mirrors.
MARKER_BASENAME = ".coordinator-bash-guards-disarmed"

#: The three, and only three, recognised `Scope:` values. See module
#: docstring "THE SCOPE AXIS".
SCOPE_TIME = "time"
SCOPE_SESSION = "session"
SCOPE_MACHINE_TOTAL = "machine-total"
_VALID_SCOPES = frozenset({SCOPE_TIME, SCOPE_SESSION, SCOPE_MACHINE_TOTAL})

#: Maximum honoured `Since`..`Expires` span for `time`/`session` scope. See
#: module docstring "EXPIRY IS MANDATORY ... AND CAPPED" for why 8 hours (a
#: working day) was chosen over a longer cap. Deliberately NOT applied to
#: `machine-total` scope, which is allowed to be standing.
_MAX_DISARM_DURATION = timedelta(hours=8)

#: OPEN QUESTION, answered conservatively -- see module docstring
#: "NO-INHERIT (session scope)". `False` = a session-scoped disarm covers
#: ONLY that session's own EM-context tool calls, never any subagent it
#: dispatches. Flip to `True` (a single-line change) if the PM's eventual
#: answer is "yes, session scope should inherit to dispatched subagents of
#: that session" -- do not restructure `_evaluate_session_scope` to do so.
_SESSION_SCOPE_INHERITS_TO_DISPATCHED_SUBAGENTS = False

#: The ONLY bands a `Bands:` field may name -- an explicit ALLOWLIST, not a
#: denylist of `GuardBand.CONFINEMENT_DENY`. See module docstring
#: "BAND-SCOPED SUPPRESSION" negative spec for why this must stay an
#: allowlist (a future fourth `GuardBand` member must default to
#: non-suppressible, not silently inherit suppressibility).
_SUPPRESSIBLE_BANDS = frozenset(
    {GuardBand.ADVISORY_REWRITE.value, GuardBand.PLATFORM_CONDITIONED_DENY.value}
)

#: Per-process cache, keyed by the calling context -- see module docstring
#: "HOT-PATH CHEAP, PER-(SESSION, AUDIENCE) CACHE". Safe for the lifetime of
#: this process only, which is exactly one PreToolUse(Bash) event by
#: construction.
_cache: Dict[Tuple[str, bool], "DisarmResult"] = {}


@dataclass(frozen=True)
class DisarmResult:
    """Outcome of a blanket-disarm evaluation, with enough detail for a
    later deny-message to quote truthfully (e.g. "disarm marker present but
    expired at <ts>") without every caller having to re-derive why.

    `active` is the only field `blanket_disarm_active()` callers strictly
    need; `detail`, `scope`, `bands`, and `expires_at` exist for a future
    guard message that wants to explain a non-disarmed state more
    specifically than a bare boolean allows.

    `bands` is `None` when this result never reached a band-aware
    evaluation (e.g. the marker was rejected before `Bands:` was even
    parsed), or a `frozenset` of the `GuardBand` string values this marker
    suppresses -- possibly empty, meaning "active for scope/audience
    purposes, but suppresses no band" (an absent/unrecognised `Bands:`
    field -- see module docstring "BAND-SCOPED SUPPRESSION").
    """

    active: bool
    detail: str
    scope: Optional[str] = None
    bands: Optional[FrozenSet[str]] = None
    expires_at: Optional[datetime] = None


_NOT_FOUND = DisarmResult(active=False, detail="no disarm marker present")


def marker_path() -> Path:
    """Resolve the full path to the blanket-disarm marker file, rooted at
    the machine-scoped settings home (see module docstring "MARKER
    LOCATION")."""
    return settings_home() / MARKER_BASENAME


def _parse_utc_timestamp(raw: str) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp (`Z` suffix or explicit UTC offset).
    Returns `None` (never raises) on anything unparseable -- the caller
    treats `None` as "malformed", one more fail-closed leg.

    A naive (offset-less) timestamp is also rejected (`None`): an expiry
    with no timezone is ambiguous about which UTC instant it means, and
    this predicate must never guess in the direction of "still valid".
    """
    raw = raw.strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_marker_fields(text: str) -> Dict[str, str]:
    """Parse `Key: value` lines into a dict, lower-casing keys. Lines that
    are not `Key: value` shaped (blank lines, free-text continuation, a
    stray line with no colon) are silently skipped -- this is a LENIENT
    PARSE only; leniency here never widens what counts as a valid disarm,
    because the required-field and timestamp checks downstream still apply
    to whatever this returns."""
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue
        fields[key] = value
    return fields


def _is_em_caller(payload: Optional[Dict[str, Any]], git_root: Optional[str]) -> bool:
    """True iff `payload` describes the main-loop EM, not a dispatched
    subagent -- reuses `resolve_effective_types`, the SAME resolver every
    other identity-gated guard in this package calls (see module docstring
    "AUDIENCE NARROWING": this module deliberately does not author a second
    resolver). A caller is "the EM" iff none of the three resolved legs
    (`agent_id`, `agent_type`, `subagent_type`) are populated -- the shape
    a main-loop tool call has and a dispatched subagent's never does.
    """
    if not payload:
        # No payload supplied at all -- callable directly by a test or a
        # non-Bash-hook caller; treat as "cannot prove EM", fail closed
        # toward "not EM" so machine-total/session-no-inherit narrowing
        # never accidentally widens for an unproven caller.
        return False
    agent_id, agent_type, subagent_type = resolve_effective_types(payload, git_root)
    return not agent_id and not agent_type and not subagent_type


def _validate_since_expires(
    fields: Dict[str, str], now: datetime, *, require_expires: bool, apply_cap: bool
) -> Tuple[Optional[DisarmResult], Optional[datetime], Optional[datetime]]:
    """Shared `Since`/`Expires` validation for the `time` and `session`
    branches (and the optional-`Expires` leg of `machine-total`). Returns
    `(early_return_result_or_None, since, expires)` -- when the first
    element is not `None`, the caller returns it immediately (a fail-closed
    leg fired); otherwise `since`/`expires` are valid, UTC-aware
    `datetime`s (with `expires` possibly `None` when `require_expires` is
    `False` and the field was simply absent)."""
    since_raw = fields.get("since")
    expires_raw = fields.get("expires")

    if not since_raw:
        return (
            DisarmResult(active=False, detail="disarm marker missing required Since field"),
            None,
            None,
        )
    since = _parse_utc_timestamp(since_raw)
    if since is None:
        return (
            DisarmResult(active=False, detail="disarm marker has an unparseable Since timestamp"),
            None,
            None,
        )

    if not expires_raw:
        if require_expires:
            return (
                DisarmResult(
                    active=False, detail="disarm marker missing required Expires field"
                ),
                since,
                None,
            )
        return None, since, None

    expires = _parse_utc_timestamp(expires_raw)
    if expires is None:
        return (
            DisarmResult(
                active=False, detail="disarm marker has an unparseable Expires timestamp"
            ),
            since,
            None,
        )

    if expires <= now:
        return (
            DisarmResult(
                active=False,
                detail=f"disarm marker expired at {expires.isoformat()}",
                expires_at=expires,
            ),
            since,
            expires,
        )

    if apply_cap and (expires - since) > _MAX_DISARM_DURATION:
        return (
            DisarmResult(
                active=False,
                detail=(
                    "disarm marker's Since..Expires span exceeds the "
                    f"{_MAX_DISARM_DURATION} cap -- treated as expired"
                ),
                expires_at=expires,
            ),
            since,
            expires,
        )

    return None, since, expires


def _parse_suppressed_bands(fields: Dict[str, str]) -> Tuple[FrozenSet[str], bool]:
    """Parse the `Bands:` field into `(suppressed_bands, confinement_deny_named)`.

    `confinement_deny_named=True` means the WHOLE marker must be rejected
    (see module docstring "BAND-SCOPED SUPPRESSION" -- a marker naming the
    non-suppressible `confinement-deny` band is malformed in its entirety,
    not merely missing that one token, so a typo cannot half-apply). When
    `confinement_deny_named` is `True`, the returned `suppressed_bands` is
    always empty and MUST be ignored by the caller in favor of rejecting
    the marker outright.

    Otherwise, `suppressed_bands` is:
      - empty, when `Bands:` is absent, blank, or names ONLY unrecognised
        tokens (or a MIX of recognised and unrecognised tokens -- "partly-
        unrecognised" suppresses nothing, same fail-closed discipline as
        `Scope:` -- see module docstring "`Bands:` FAILS CLOSED THE SAME
        WAY `Scope:` DOES");
      - the fully-recognised, `_SUPPRESSIBLE_BANDS`-only set of tokens
        named, when every named token is recognised.
    """
    raw = (fields.get("bands") or "").strip()
    if not raw:
        return frozenset(), False

    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if not tokens:
        return frozenset(), False

    if GuardBand.CONFINEMENT_DENY.value in tokens:
        return frozenset(), True

    if any(t not in _SUPPRESSIBLE_BANDS for t in tokens):
        # Partly-or-wholly unrecognised (confinement-deny already handled
        # above) -- suppress nothing, never "all bands" or "the whole
        # suppressible set".
        return frozenset(), False

    return frozenset(tokens), False


def _no_bands_named_detail(scope_desc: str, expires: Optional[datetime]) -> str:
    """Shared detail text for M18 -- a marker whose Scope/Since/Expires/
    audience checks ALL pass, but whose `Bands:` field names nothing
    suppressible (absent, blank, or entirely unrecognised tokens -- see
    `_parse_suppressed_bands`). Such a marker is well-formed and VALIDATES,
    but disarms nothing: `disarm_covers_band()` already returned `False`
    for every band against it before this fix, because `status.bands` was
    empty. The bug this closes is `active=True` on this exact result --
    calling that state "active" is a false capability claim (the specific
    class of guard-message defect this whole workstream exists to delete)
    reappearing inside the mechanism built to fix it. See module docstring
    "M18" for the fuller incident note; found by probing the simplest
    marker an operator would hand-write (`Scope: machine-total` / `Scope:
    time` with no `Bands:` line at all) and observing `active=True` with
    `disarm_covers_band(ANY_BAND)` still `False`.

    Deliberately shared across all three scope evaluators below rather than
    duplicated per-scope: the "well-formed but inert" state is the same
    fact regardless of which scope produced it, and a single wording here
    is one edit point instead of three that can drift.

    NEGATIVE SPEC -- this function does NOT change what an absent/blank/
    unrecognised `Bands:` field MEANS (still: suppress nothing -- see
    `_parse_suppressed_bands`'s own docstring). Whether that default should
    instead mean "suppress every suppressible band" is a direction-class
    call for the PM (it widens default blast radius) and is explicitly OUT
    OF SCOPE for this fix -- this function only makes the marker's own
    self-description stop lying about a state that already existed.
    """
    when = f" (valid until {expires.isoformat()})" if expires is not None else " (standing, no expiry)"
    return (
        f"{scope_desc} marker validates{when}, but its Bands field names "
        "nothing suppressible -- it is present and well-formed, NOT active. "
        "Add a Bands: line naming what it should suppress (e.g. 'Bands: "
        "advisory-rewrite' or 'Bands: advisory-rewrite,platform-"
        "conditioned-deny' -- confinement-deny can never be named). As "
        "written, this marker disarms nothing."
    )


def _evaluate_time_scope(
    fields: Dict[str, str], now: datetime, bands: FrozenSet[str]
) -> DisarmResult:
    """`Scope: time` -- machine-wide, bounded, unrestricted audience. See
    module docstring "THE SCOPE AXIS"."""
    early, _since, expires = _validate_since_expires(
        fields, now, require_expires=True, apply_cap=True
    )
    if early is not None:
        return DisarmResult(active=False, detail=early.detail, scope=SCOPE_TIME, expires_at=early.expires_at)
    if not bands:
        # M18 -- a validated marker naming no suppressible band is inert,
        # not active. See `_no_bands_named_detail`.
        return DisarmResult(
            active=False,
            detail=_no_bands_named_detail("machine-wide time-boxed", expires),
            scope=SCOPE_TIME,
            bands=bands,
            expires_at=expires,
        )
    return DisarmResult(
        active=True,
        detail=f"machine-wide time-boxed disarm valid until {expires.isoformat()}, suppressing {sorted(bands)}",
        scope=SCOPE_TIME,
        bands=bands,
        expires_at=expires,
    )


def _evaluate_session_scope(
    fields: Dict[str, str], now: datetime, session_id: str, is_em: bool, bands: FrozenSet[str]
) -> DisarmResult:
    """`Scope: session` -- one named session, no-inherit to its dispatched
    subagents (conservative default -- see module docstring "NO-INHERIT
    (session scope)")."""
    if not is_em and not _SESSION_SCOPE_INHERITS_TO_DISPATCHED_SUBAGENTS:
        return DisarmResult(
            active=False,
            detail="session-scoped disarm marker does not inherit to dispatched subagents",
            scope=SCOPE_SESSION,
        )

    target_session = fields.get("session")
    if not target_session:
        return DisarmResult(
            active=False,
            detail="session-scoped disarm marker missing required Session field",
            scope=SCOPE_SESSION,
        )
    if target_session != session_id:
        return DisarmResult(
            active=False,
            detail="session-scoped disarm marker names a different session",
            scope=SCOPE_SESSION,
        )

    early, _since, expires = _validate_since_expires(
        fields, now, require_expires=True, apply_cap=True
    )
    if early is not None:
        return DisarmResult(active=False, detail=early.detail, scope=SCOPE_SESSION, expires_at=early.expires_at)
    if not bands:
        # M18 -- see `_no_bands_named_detail`.
        return DisarmResult(
            active=False,
            detail=_no_bands_named_detail("session-scoped", expires),
            scope=SCOPE_SESSION,
            bands=bands,
            expires_at=expires,
        )
    return DisarmResult(
        active=True,
        detail=f"session-scoped disarm valid until {expires.isoformat()}, suppressing {sorted(bands)}",
        scope=SCOPE_SESSION,
        bands=bands,
        expires_at=expires,
    )


def _evaluate_machine_total_scope(
    fields: Dict[str, str], now: datetime, is_em: bool, bands: FrozenSet[str]
) -> DisarmResult:
    """`Scope: machine-total` -- standing, no-expiry-required, but
    UNCONDITIONALLY EM-only (see module docstring "AUDIENCE NARROWING").
    This is the one branch of this module that is fail-OPEN by design; the
    EM-only check below is what keeps that from being reckless."""
    if not is_em:
        return DisarmResult(
            active=False,
            detail="machine-total disarm applies to the EM only; caller is a dispatched subagent",
            scope=SCOPE_MACHINE_TOTAL,
        )

    early, _since, expires = _validate_since_expires(
        fields, now, require_expires=False, apply_cap=False
    )
    if early is not None:
        return DisarmResult(active=False, detail=early.detail, scope=SCOPE_MACHINE_TOTAL, expires_at=early.expires_at)

    if not bands:
        # M18 -- see `_no_bands_named_detail`. Checked BEFORE the
        # expires-vs-standing branch below: the "well-formed but inert"
        # fact is the same regardless of whether this marker also carries
        # an Expires, so one branch covers both instead of duplicating the
        # empty-bands check into each.
        return DisarmResult(
            active=False,
            detail=_no_bands_named_detail("machine-total EM-only", expires),
            scope=SCOPE_MACHINE_TOTAL,
            bands=bands,
            expires_at=expires,
        )

    if expires is not None:
        return DisarmResult(
            active=True,
            detail=f"machine-total EM-only disarm valid until {expires.isoformat()}, suppressing {sorted(bands)}",
            scope=SCOPE_MACHINE_TOTAL,
            bands=bands,
            expires_at=expires,
        )
    return DisarmResult(
        active=True,
        detail=f"machine-total EM-only disarm active (standing, no expiry), suppressing {sorted(bands)}",
        scope=SCOPE_MACHINE_TOTAL,
        bands=bands,
    )


def _evaluate(now: datetime, session_id: str, is_em: bool) -> DisarmResult:
    """Pure evaluation of the marker against `now` and the calling context
    -- factored out of `disarm_status()` so the cache wrapper and the pure
    logic are separate, and so tests can exercise arbitrary `now`/context
    values without patching the clock or forging a payload."""
    path = marker_path()

    try:
        if not path.exists():
            return _NOT_FOUND
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Unreadable (permission error, is-a-directory, race where the file
        # vanished between exists() and read_text(), ...) -- treated
        # identically to "absent". No exception ever propagates out of this
        # function; every OSError leg lands here, fail-closed.
        return DisarmResult(active=False, detail="disarm marker present but unreadable")

    if not text.strip():
        return DisarmResult(active=False, detail="disarm marker present but empty")

    fields = _parse_marker_fields(text)

    scope = (fields.get("scope") or "").lower()
    if scope not in _VALID_SCOPES:
        # Missing/blank/unrecognised Scope -- disarms NOTHING. Never a
        # fallback to any particular scope's behavior. See module docstring
        # "THE SCOPE AXIS" / "FAIL-CLOSED CONDITIONS".
        return DisarmResult(
            active=False,
            detail="disarm marker missing or has an unrecognised Scope field",
        )

    bands, confinement_deny_named = _parse_suppressed_bands(fields)
    if confinement_deny_named:
        # The whole marker is malformed -- see module docstring "BAND-SCOPED
        # SUPPRESSION": a typo/copy-paste naming the non-suppressible band
        # must not "half-apply" and suppress the other named bands anyway.
        return DisarmResult(
            active=False,
            detail=(
                "disarm marker names the non-suppressible confinement-deny "
                "band in its Bands field -- the whole marker is rejected"
            ),
            scope=scope,
        )

    if scope == SCOPE_TIME:
        return _evaluate_time_scope(fields, now, bands)
    if scope == SCOPE_SESSION:
        return _evaluate_session_scope(fields, now, session_id, is_em, bands)
    if scope == SCOPE_MACHINE_TOTAL:
        return _evaluate_machine_total_scope(fields, now, is_em, bands)

    # NEGATIVE SPEC -- unreachable today, and deliberately NOT written as a
    # bare `return _evaluate_machine_total_scope(...)` fallthrough on the
    # reasoning that machine-total is "the only remaining member of
    # _VALID_SCOPES". That reasoning is true right now and silently stops
    # being true the moment a fourth scope is added to _VALID_SCOPES: the new
    # member would inherit machine-total's behavior, which is the most
    # permissive one in this module (standing, no expiry). A new scope must
    # default to disarming NOTHING until someone writes its branch, matching
    # the same rule _SUPPRESSIBLE_BANDS enforces for a newly-added band.
    return DisarmResult(
        active=False,
        detail=(
            "disarm marker declares scope %r, which is registered as valid but "
            "has no evaluation branch -- disarms nothing until one is written" % scope
        ),
        scope=scope,
    )


def blanket_disarm_active(
    payload: Optional[Dict[str, Any]] = None, git_root: Optional[str] = None
) -> bool:
    """Return True iff the machine-scoped disarm marker is present,
    well-formed, within its validity window, applies (per its `Scope`) to
    the calling context described by `payload`, AND names at least one
    suppressible band in its `Bands:` field. See module docstring for the
    full fail-closed contract.

    NARROWED (M18, 2026-07-30) -- do NOT read a bare `True` here as licence
    to skip anything. Before this fix, this function returned `True` for a
    marker that validated its `Scope`/`Since`/`Expires`/audience checks but
    named NO suppressible band at all (the exact "well-formed but inert"
    marker an operator would hand-write by omitting `Bands:` entirely) --
    a false capability claim, discovered by probing the simplest marker
    shape an operator would actually write. `True` now REQUIRES a non-empty
    suppressed-bands set (see `_no_bands_named_detail` / the three
    `_evaluate_*_scope` functions below, each of which now returns
    `active=False` when `bands` is empty even though every other check
    passed).

    This function is still a BARE, BAND-BLIND boolean -- it answers "does
    this marker suppress ANYTHING", never "does it suppress the ONE band
    a specific guard belongs to". A caller deciding whether to skip a
    PARTICULAR guard must call `disarm_covers_band(band, payload,
    git_root)` (as `dispatch.py`'s own wiring does), never this function --
    `True` from `blanket_disarm_active()` says nothing about which band(s),
    and in particular never means `GuardBand.CONFINEMENT_DENY` is covered
    (a marker naming it is rejected in its entirety -- see
    `_parse_suppressed_bands`). This function exists for a caller that only
    needs "is there any live disarm at all" (e.g. a status/diagnostic
    surface), not for anything gating an individual guard's own behavior.

    `payload` is the same PreToolUse hook payload every guard in this
    package already receives (carries `session_id`, and the `agent_id`/
    `agent_type` legs `resolve_effective_types` reads to tell the EM apart
    from a dispatched subagent). `git_root`, if available, is forwarded to
    `resolve_effective_types` for its back-pointer `subagent_type` leg;
    omitting it still resolves the EM/subagent distinction correctly for
    the two top-level legs (`agent_id`, `agent_type`), which is what
    `_is_em_caller` actually depends on.

    Calling with no `payload` at all only ever matches `Scope: time` (the
    one scope with no audience restriction) -- `session`/`machine-total`
    both require proof of EM-ness, which an absent payload cannot supply
    (see `_is_em_caller`'s fail-closed-toward-"not EM" default).
    """
    return disarm_status(payload, git_root).active


def disarm_status(
    payload: Optional[Dict[str, Any]] = None, git_root: Optional[str] = None
) -> DisarmResult:
    """Same evaluation as `blanket_disarm_active()`, but returns the full
    `DisarmResult` (with `detail`/`scope`/`expires_at`) for a caller -- e.g.
    a future guard deny-message -- that wants to explain a non-disarmed
    state truthfully rather than as a bare boolean.
    """
    session_id = (payload or {}).get("session_id") or ""
    if not isinstance(session_id, str):
        session_id = ""
    is_em = _is_em_caller(payload, git_root)

    cache_key = (session_id, is_em)
    cached = _cache.get(cache_key)
    if cached is None:
        cached = _evaluate(datetime.now(timezone.utc), session_id, is_em)
        _cache[cache_key] = cached
    return cached


def disarm_covers_band(
    band: Union[GuardBand, str],
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> bool:
    """Forward-compatible query surface for the deferred `dispatch.py`
    wiring (see module docstring "AGENT-CREATION MUST BE BLOCKABLE" for why
    that wiring is not landed in this task): True iff the disarm marker is
    active for the calling context AND its `Bands:` field names `band` as
    suppressed.

    `band` may be a `GuardBand` member or its raw string value. Always
    False for `GuardBand.CONFINEMENT_DENY` in practice -- a marker naming
    it is rejected wholesale by `_parse_suppressed_bands` (see module
    docstring "BAND-SCOPED SUPPRESSION"), so `status.bands` can never
    contain it -- but this function does not special-case that; it falls
    out of the marker-parsing fail-closed behavior alone.

    A future per-guard band check should call THIS function rather than
    hand-deriving `disarm_status(...).bands` membership a second time.
    """
    status = disarm_status(payload, git_root)
    if not status.active or status.bands is None:
        return False
    band_value = band.value if isinstance(band, GuardBand) else str(band)
    return band_value in status.bands
