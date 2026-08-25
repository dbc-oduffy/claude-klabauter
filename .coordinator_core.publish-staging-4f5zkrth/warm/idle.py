"""coordinator_core.warm.idle — idle-demotion timer: drain back to cold
after n minutes with no invocation, default 15.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C24

SUPERSEDES C17's de-fanged reclaim timer, which never wired an idle reaper
in (that was the retired resident's defect A -- eviction DEPENDING on an
idle timer). This module is the first-class replacement, kept disjoint
from C16's skew path on purpose: see "THE INVARIANT" below.

Trigger is LAST INVOCATION, never session count. Both of the PM's cases
(every session ended; every session sitting idle -- token-exhaustion wait,
operator elsewhere) present identically as "nothing has dispatched for n
minutes", so one clock covers both without depending on the liveness
subsystem.

THE INVARIANT THAT MUST SURVIVE REVIEW: this timer RECLAIMS, it never
ADJUDICATES. It must not gate, delay, or veto a skew eviction, and skew
eviction must not consult it -- `warm.skew.evict_on_skew` never imports
this module. The two paths share only C17's `warm.lifecycle.begin_shutdown`
shutdown sequence and nothing else; this module does not author a second
sequence.

IDLE CLOCK OWNERSHIP: `mark_invocation` is written by the dispatch seam on
EVERY invocation, including a skew-evicting one -- C16's skew check runs
per request, so a skew-evicting request resets the idle clock as an
ordinary side effect of being a request. This is harmless (the server is
exiting either way) and keeps "skew eviction consults no idle state"
literally true: C16 never READS anything from this module.

ZERO-SERVED DEADLINE (staff-eng finding 5): a server whose served-invocation
count is still zero after a short deadline (`ZERO_SERVED_DEADLINE_SECS`,
60-120s) demotes immediately rather than waiting out the full idle
deadline -- a predicate on an existing signal (C26's served-invocation
counter), not a new mechanism. `ServedCountFn` below is the seam C26 binds
to: a zero-arg callable returning the served-invocation count so far.

SUPERSEDED-GENERATION PREDICATE (`TokenStaleFn`): the two deadlines above
are both CLOCKS, and neither one reaches the population that motivated
this predicate. A publish rewrites the engine's build stamp, so the
generation token rotates, so `election.pipe_name` -- which embeds the
token -- names a DIFFERENT pipe. Every client thereafter dials that new
name, and the predecessor receives zero further traffic BY CONSTRUCTION.
`warm.skew.evict_on_skew` is traffic-driven, so the very token change that
should evict the predecessor is the one that routes traffic away from it:
the eviction path is unreachable for exactly the servers it exists to
retire. The zero-served deadline does not catch it either, because a
superseded generation has typically served plenty. So it waits out the
FULL idle deadline (15 minutes by default), and N publishes inside one
idle window leave N+1 engines resident, each holding a preloaded op
registry and a dispatch process pool. Observed live 2026-08-19: three
resident generations off the published mirror inside one hour.

`TokenStaleFn` is a LOCAL predicate -- "my generation token no longer
matches the current engine fingerprint" -- and needs no cross-process
handshake, no bind-time notification, and no prior-token registry. The
caller binds it (`warm.server._ServerContext._token_is_stale`); this
module never computes a token and never imports `warm.skew`, which is
what keeps THE INVARIANT below literally true in both directions.

RESTATED 2026-08-19 (plan 2026-08-19-an-engine-root-is-a-stamped-build §
C3): the unstamped/live-working-tree rotation described above was
recorded as *deliberately in scope* under the old rule, and gating the
binding on stamp presence was "considered and rejected -- it would leave
the live-tree population unfixed, and that population is the larger
one." That reasoning is CORRECT under the old rule and VOID under the
new one: once no ambient resolution path can return an unstamped tree as
the engine (this plan's C4, gated on C1), no server can be running from
an unstamped clone in the first place, so the population this predicate
was protecting stops existing. This paragraph does not change this
module's behaviour -- C3 is a pure collapse, and `TokenStaleFn`'s
fallback semantics are `compute_client_token`'s to change, not this
module's -- it corrects the reasoning so a future reader does not find
this docstring still defending a population the shipped rule no longer
has.

NOT FIXED BY SHORTENING THE IDLE DEADLINE, deliberately: that trades
stranding window against warm-hit rate for every HEALTHY server while
leaving the unreachable-eviction defect in place. It hides the symptom.

CONFIG SURFACE: `engine.warm.idle_minutes` extends the SAME
`engine.warm.*` registry namespace C23's `settings.py` introduced --
resolved through the same `machine_resolver.registry_get` read path, no
second config surface.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from coordinator_core.machine_resolver import registry_get
from coordinator_core.warm.lifecycle import begin_shutdown

__all__ = [
    "REGISTRY_KEY_IDLE_MINUTES",
    "DEFAULT_IDLE_MINUTES",
    "ZERO_SERVED_DEADLINE_SECS",
    "ServedCountFn",
    "TokenStaleFn",
    "mark_invocation",
    "seconds_idle",
    "reset_idle_clock_for_test",
    "resolve_idle_deadline_secs",
    "should_demote",
    "demote_if_idle",
]

REGISTRY_KEY_IDLE_MINUTES = "engine.warm.idle_minutes"
DEFAULT_IDLE_MINUTES = 15

# Staff-eng finding 5 names a 60-120s range; the midpoint is the default and
# is not registry-configurable -- it is a fixed short-circuit on an existing
# signal (served count), not a product-facing setting like the idle minutes
# above.
ZERO_SERVED_DEADLINE_SECS = 90.0

# The seam C26 (authored after this chunk) binds to: a zero-arg callable
# returning the server's served-invocation count so far. `should_demote`
# and `demote_if_idle` accept this rather than a raw int so the caller
# supplies a live read, never a snapshot taken before this call.
ServedCountFn = Callable[[], int]

# The seam the server binds its superseded-generation check to (module
# docstring): a zero-arg callable returning True iff this server's own
# generation token no longer matches the current engine fingerprint.
# A CALLABLE rather than a bool for the same reason `ServedCountFn` is --
# the predicate must read live state at check time, never a snapshot taken
# at server boot, since the whole point is to observe a change that
# happens mid-life.
TokenStaleFn = Callable[[], bool]

_clock_lock = threading.Lock()
_last_invocation_monotonic: Optional[float] = None


def mark_invocation(*, clock: Callable[[], float] = time.monotonic) -> None:
    """Record "an invocation happened now". Called by the dispatch seam on
    EVERY invocation -- see the module docstring's IDLE CLOCK OWNERSHIP note
    for why a skew-evicting request writing this too is harmless and
    deliberate, not a bug to suppress.
    """
    global _last_invocation_monotonic
    with _clock_lock:
        _last_invocation_monotonic = clock()


def seconds_idle(*, clock: Callable[[], float] = time.monotonic) -> float:
    """Seconds since the last `mark_invocation`, or since this module was
    imported (process start) if no invocation has ever been marked --
    treating "never invoked" as idle from process birth, which is what
    makes the zero-served deadline reachable in the first place.
    """
    with _clock_lock:
        last = _last_invocation_monotonic
        if last is None:
            last = _process_start_monotonic
    return clock() - last


def reset_idle_clock_for_test() -> None:
    """Test-only: clear the module-level idle clock so a fresh test can
    exercise `seconds_idle`/`should_demote` from a known state. Never
    called by production code.
    """
    global _last_invocation_monotonic
    with _clock_lock:
        _last_invocation_monotonic = None


_process_start_monotonic = time.monotonic()


def resolve_idle_deadline_secs() -> float:
    """Resolve the configured idle-demotion deadline in seconds, reading
    `engine.warm.idle_minutes` off the same machine-local registry
    `warm.settings` reads `engine.warm.enabled` from. An absent or
    unparseable value falls back to `DEFAULT_IDLE_MINUTES` -- this is a
    read path consumed on every idle check, not a validated config load.
    """
    raw = registry_get(REGISTRY_KEY_IDLE_MINUTES)
    if raw is None:
        return DEFAULT_IDLE_MINUTES * 60.0
    try:
        minutes = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_IDLE_MINUTES * 60.0
    if minutes <= 0:
        return DEFAULT_IDLE_MINUTES * 60.0
    return minutes * 60.0


def should_demote(
    *,
    served_count: ServedCountFn,
    token_stale: Optional[TokenStaleFn] = None,
    idle_deadline_secs: Optional[float] = None,
    zero_served_deadline_secs: float = ZERO_SERVED_DEADLINE_SECS,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True if the server should demote right now, on any of three arms:
    this server's generation has been superseded (`token_stale()`, module
    docstring), the full idle deadline has elapsed, or the zero-served
    deadline has elapsed while `served_count()` is still zero (staff-eng
    finding 5). Pure predicate -- no side effects, no shutdown call;
    `demote_if_idle` is the one that acts on this.

    THE SUPERSEDED ARM CARRIES NO DEADLINE, and that is not an oversight.
    The other two arms are clocks because "nobody is using this server" is
    a claim that only time can establish. "A newer engine has replaced
    this one" is established the instant it is true, and waiting adds
    nothing: no client can reach a superseded generation anyway -- its
    pipe name is one no client computes any more -- so a grace period
    buys back zero warm hits and costs one fully resident engine for its
    whole duration. Orderly drain is preserved not by delaying the
    decision but by `demote_if_idle` routing through C17's unchanged
    `begin_shutdown`, which closes the listener and waits out in-flight
    work exactly as every other exit path does.

    `token_stale` defaults to `None` (never stale) so every existing
    caller and test keeps its current two-arm behaviour unchanged; only a
    caller that can actually compute its own generation token binds it.

    THE FULL-DEADLINE ARM MUST STAY KEYED ON `seconds_idle()` (time since
    the LAST `mark_invocation`), never narrowed to "never served anything
    (`served_count() == 0`)" -- that narrower predicate looks like the
    obvious simplification and is wrong: the field orphan a stranded
    generation actually produces (`warm.skew`'s token rotation minting a
    new pipe name out from under a still-listening predecessor) typically
    SERVED requests before rotation stranded it, so its `served_count()`
    is nonzero. A refactor that drops the OR and demotes solely on
    `served_count() == 0` leaves that orphan to the full deadline while
    still passing a synthetic zero-invocation test
    (test_idle_demotion_survives_token_rotation_after_serving in
    `warm/tests/test_server_loop.py` is the regression pinned against
    exactly this).

    The superseded arm now retires that orphan promptly, but it does NOT
    make the full-deadline arm redundant for it: `token_stale` is
    caller-bound and optional, so any caller that does not bind one still
    depends on `seconds_idle()` as the only thing that ever crosses a
    deadline for a nonzero-served orphan. Both arms stay.
    """
    if token_stale is not None and token_stale():
        return True
    idle = seconds_idle(clock=clock)
    deadline = (
        resolve_idle_deadline_secs() if idle_deadline_secs is None else idle_deadline_secs
    )
    if served_count() == 0 and idle >= zero_served_deadline_secs:
        return True
    return idle >= deadline


def demote_if_idle(
    *,
    served_count: ServedCountFn,
    token_stale: Optional[TokenStaleFn] = None,
    close_listener: Callable[[], None],
    in_flight_count: Callable[[], int],
    ctx_shutdown: Callable[[], None],
    exit_fn: Callable[[int], None] = os._exit,
    idle_deadline_secs: Optional[float] = None,
    zero_served_deadline_secs: float = ZERO_SERVED_DEADLINE_SECS,
    drain_ceiling_secs: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """If `should_demote` says yes, drain and exit via C17's unchanged
    `begin_shutdown` sequence -- this module never reimplements shutdown,
    it only decides WHEN to call it. Returns whatever `begin_shutdown`
    returns (False if some other trigger already won the single-shot
    guard); returns False without calling `begin_shutdown` at all when the
    idle predicate says no.
    """
    if not should_demote(
        served_count=served_count,
        token_stale=token_stale,
        idle_deadline_secs=idle_deadline_secs,
        zero_served_deadline_secs=zero_served_deadline_secs,
        clock=clock,
    ):
        return False
    return begin_shutdown(
        close_listener=close_listener,
        in_flight_count=in_flight_count,
        ctx_shutdown=ctx_shutdown,
        exit_fn=exit_fn,
        drain_ceiling_secs=drain_ceiling_secs,
    )
