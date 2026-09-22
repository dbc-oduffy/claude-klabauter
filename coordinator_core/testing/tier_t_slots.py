"""
coordinator_core.testing.tier_t_slots — machine-wide K-slot semaphore for
scoped (Tier-T) test invocations.

Purpose: bound how many file/node-id-scoped test runs execute AT ONCE across
every repo and every concurrent Claude session on the box. This is the
RESOURCE control for Tier T, and it is the counterpart the DR-088 ladder was
missing: `check_test_suite_invocation` gives Tier T an AUTHORITY bound (leg 0
narrows a dispatched caller to file-and-node-id precision) and, until this
module, no resource bound of any kind at any N.

THE INCIDENT, stated precisely, because the obvious reading of it is wrong.
On 2026-09-20 a `/bug-blitz` run over an 876-record backlog took this box to a
15-minute load average of 17.83 and had to be recovered. The reading that
"executors ran the test suite concurrently" is what the suite mutex already
prevents, and it is not what happened: the mutex worked, killing the one
suite-shaped run at its 2700s ceiling with 33 processes still live. The damage
came from the TRIAGE leg, which had not yet reached its executors, issuing
hundreds of scoped runs — every one of them a run the guard is designed to
permit, and correctly so (DoE ruling R9, 2026-07-28: a node id stays permitted
for a subagent regardless of its touched set, which is what keeps
pre-existing-failure verification legal).

So the gap was never an authority gap, and MUST NOT be closed by narrowing the
Tier-T carve-out: that carve-out belongs to another plane's ruling and protects
the one workflow it was added for. A single scoped run is genuinely cheap —
measured here at 0.18s and ~72MB for a collection — which is exactly why
nothing flagged it. The cost that killed the box was the PRODUCT, and no
artifact in the chain could see the product: the ceremony's correctness bar
("`already-fixed` needs the failing case run against HEAD") is right, the
guard's carve-out is right, and composed over 876 records they multiply into an
unbounded spawn count neither one can observe.

CORROBORATION, from a peer session the same afternoon and unprompted: on
finding 49 reds in its own fast tier, ``example-market-data-repo-90`` reported that
its first instinct was to immediately re-run each one scoped for a verdict --
49 concurrent Tier-T runs -- and that it suppressed the instinct only because
it had just been told about this incident. Its own words: "that instinct is the
failure mode, it feels like diligence, and nothing currently stops it." That is
the case for bounding this mechanically rather than by discipline. The peer was
being careful and still nearly did it.

Design: K independent lock dirs, ``<settings-home>/tier-t-slots/slot-<i>.lock``,
each one a single instance of the atomic-mkdir primitive in ``suite_mutex``,
taken via that module's ``path=`` parameter. The liveness rules, TTL split,
metadata grace window and stale-reclaim logic are therefore SHARED rather than
forked — all of it was bought with incidents (see that module's
``LIVE_HOLDER_TTL_SECS`` comment for one that cost a reclaimed live suite) and
a second hand-rolled copy would drift from it silently.

Acquisition walks slots in index order and takes the first that is free, so a
partially-loaded box concentrates in the low slots and ``free_slots()`` reads
as a simple count. Order is not fairness: under contention, which waiter wins
which slot is unspecified, exactly as in ``suite_mutex``.

K comes from ``coordinator_core.install.derive_worker_cap.derive_cap`` — an
existing implementation of CLAUDE.md's two-term ``min(physical_cores/2,
usable_RAM_GB*1024/150MB)`` formula, which is also what bounds xdist workers.

Which of the repo's TWO implementations of that formula is load-bearing here:
``diagnostics.contained_run.derive_worker_cap`` computes the same number and
was the first choice, but ``coordinator_core/diagnostics`` is NOT in the
published engine's restricted tree, and the published mirror is where this
guard actually runs. Importing it there would raise, hit the fallback below and
silently pin K at 1 -- serializing every dispatched scoped run on the box while
the semaphore looked healthy. The publish import-closure gate caught it before
it shipped. ``install`` ships, so ``derive_cap`` is the one that can be
depended on from here. Verify a module ships before importing it into anything
on the guard path. That reuse is deliberate and load-bearing: a whole scoped
pytest process and an xdist worker are the same unit of cost on this box, so
they must not be bounded by two numbers that can drift apart. K is resolved
FRESH on every call, never cached at import: the RAM term reads *available*
memory, so K legitimately falls as the box fills, which is the adaptive
behaviour a fixed constant cannot have. On the machine this was written on
(15 logical cores, 25.8GB) K resolves to 7; on the 24-core workstation it
resolves higher. Never replace this with a literal.

TTL: a scoped run is short by construction. `SLOT_STALE_TTL_SECS` is therefore
far tighter than the suite mutex's ceiling — a scoped run still holding a slot
after this long is wedged, not slow, and holding K slots hostage to wedged runs
is the failure mode this module would otherwise introduce. It is the one policy
number this module sets for itself rather than inheriting.

Negative spec — what this is NOT:
    - NOT an authorization check. It answers "may another scoped run start
      *now*", never "is this caller entitled to run tests at all". Tier/grant
      enforcement stays in ``bash_guards``.
    - NOT a replacement for the suite mutex, and NOT to be collapsed into it.
      A suite run and a scoped run are different tiers with different bounds:
      one at a time machine-wide vs. K at a time. A single mechanism serving
      both would either serialize scoped runs (breaking the R9 workflow) or
      admit K suites (reopening DR-088 layer 6).
    - Does NOT raise from ``free_slots()`` or ``acquire_slot()`` — a resource
      control whose read path can explode would take down the guard that
      consults it, and that guard must be able to deny (or allow) cleanly.
    - Does NOT bound TOTAL test spawns, only CONCURRENT ones. A ceremony that
      issues 876 scoped runs serially is not this module's problem to solve;
      that is a spawn-count budget and belongs to the amplification gate.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.testing import suite_mutex

_LOG = logging.getLogger(__name__)

_SLOTS_DIRNAME = "tier-t-slots"

#: Ceiling for a slot whose holder cannot be shown alive, and — unlike the
#: suite mutex, which splits its TTL because an honest suite legitimately runs
#: for hours — for a live holder too. A scoped run that has held a slot for
#: fifteen minutes is not a slow scoped run; the tier is defined by naming
#: individual files and node ids. Reclaiming at one flat ceiling keeps a wedged
#: runner from consuming a slot for the rest of the session, which matters far
#: more here than at the suite mutex: there a bad reclaim lets a second suite
#: start, while here the whole point is that K runs are already concurrent and
#: safe.
SLOT_STALE_TTL_SECS: float = 15 * 60.0

_OWNER_PREFIX = "tier-t"


def slot_cap() -> int:
    """Return K — how many scoped runs may execute at once on this box.

    Resolved fresh on every call (never cached): the RAM term reads *available*
    memory, so K falls as the box fills and recovers as it drains. Falls back
    to a hard floor of 1 if the derivation raises — a box that cannot be
    measured admits one scoped run at a time, which is slow but never harmful.
    """
    try:
        from coordinator_core.install.derive_worker_cap import derive_cap

        return max(1, int(derive_cap()))
    except Exception as exc:  # pragma: no cover — defensive, see docstring
        _LOG.warning("tier_t_slots: worker-cap derivation failed, falling back to 1: %s", exc)
        return 1


def slots_dir() -> Path:
    """Return the directory holding the per-slot lock dirs.

    Resolved fresh on every call so a test or caller that repoints
    ``COORDINATOR_SETTINGS_HOME`` is honoured, matching ``suite_mutex.lock_path``.
    """
    return settings_home() / _SLOTS_DIRNAME


def slot_path(index: int) -> Path:
    """Return the lock-dir path for slot ``index``."""
    return slots_dir() / f"slot-{index}.lock"


def _holder_of(path: Path) -> Optional[dict]:
    """Return the slot's holder, applying this module's tighter TTL.

    Delegates to ``suite_mutex.holder`` for the atomic-mkdir, metadata and
    liveness semantics, then applies ``SLOT_STALE_TTL_SECS`` on top — the one
    policy this module does not inherit. Never raises.
    """
    try:
        meta = suite_mutex.holder(path=path)
    except Exception as exc:
        _LOG.warning("tier_t_slots: holder read failed for %s: %s", path, exc)
        return None
    if meta is None:
        return None

    age = suite_mutex._age_secs(path, meta)
    if age is not None and age > SLOT_STALE_TTL_SECS:
        suite_mutex._reclaim(path, f"tier-t slot age {age:.0f}s exceeds {SLOT_STALE_TTL_SECS:.0f}s", meta)
        return None
    return meta


def occupancy() -> Tuple[int, int, List[dict]]:
    """Return ``(taken, cap, holders)`` for the current instant. Never raises.

    ``holders`` carries one metadata dict per occupied slot, in slot order, so
    a caller denying on a full semaphore can name who is holding it rather than
    reporting a bare count.
    """
    cap = slot_cap()
    holders: List[dict] = []
    try:
        for i in range(cap):
            meta = _holder_of(slot_path(i))
            if meta is not None:
                holders.append(dict(meta, slot=i))
    except Exception as exc:
        _LOG.warning("tier_t_slots: occupancy() swallowed an unexpected error: %s", exc)
    return len(holders), cap, holders


def free_slots() -> int:
    """Return how many scoped runs could start right now. Never raises."""
    taken, cap, _ = occupancy()
    return max(0, cap - taken)


def owner_id() -> str:
    """Owner id for the calling process, reusing the mutex's session-id resolution."""
    return suite_mutex.mutex_owner(_OWNER_PREFIX)


class Slot(NamedTuple):
    """One held slot: its index, and the unique lease token that owns it.

    ``token`` — not ``owner`` — is what is written into the lock dir, and it is
    what ``release_slot`` must be handed back. See ``acquire_slot`` for why the
    two are deliberately different strings.
    """

    index: int
    token: str
    owner: str


def acquire_slot(owner: str, cmd: str, timeout: float = 0.0, *,
                 pid: Optional[int] = None) -> Optional[Slot]:
    """Take one slot. Returns a ``Slot``, or None if all K are occupied.

    ``timeout=0`` (the default) is non-blocking: one sweep over the slots, then
    None. ``timeout>0`` re-sweeps with backoff until the deadline. Failure is
    always a None return, never an exception.

    ``pid`` records who is judged for liveness and follows ``suite_mutex.acquire``'s
    contract exactly: it defaults to this process, and a short-lived acquirer
    that spawns the runner elsewhere MUST pass the runner's PID or its slot is
    reclaimed the moment it exits.

    WHY A LEASE TOKEN RATHER THAN ``owner``, and why this is not ceremony:
    ``suite_mutex.acquire`` treats a lock already held by the SAME owner string
    as a no-op success, returning True without taking anything. That shallow
    re-entrancy is right for a mutex — one session runs one suite — and is
    exactly wrong here, where one session may legitimately hold several slots
    at once. Passing the caller's ``owner`` straight through made the second
    acquisition return the FIRST slot's index while occupying one slot, so K
    concurrent runs from one session consumed one slot and the semaphore
    silently stopped counting. It was caught by a test asserting two
    acquisitions differ; it would not have been caught by any test of the
    bound itself, because the bound still reads K. Each acquisition therefore
    mints ``<owner>#<uuid4>``, which no other acquisition can collide with, and
    the human-readable ``owner`` survives as the token's prefix so a deny can
    still name the session holding a slot.
    """
    import time

    deadline = time.monotonic() + max(0.0, timeout)
    interval = 0.1
    while True:
        cap = slot_cap()
        for i in range(cap):
            path = slot_path(i)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            # Probe for staleness first so a wedged predecessor's slot is
            # reclaimed and then won here, rather than skipped for its TTL.
            if _holder_of(path) is not None:
                continue
            token = f"{owner}#{uuid.uuid4().hex}"
            try:
                if suite_mutex.acquire(token, cmd, 0.0, pid=pid, path=path):
                    return Slot(index=i, token=token, owner=owner)
            except Exception as exc:
                _LOG.warning("tier_t_slots: acquire attempt failed on slot %d: %s", i, exc)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(interval, remaining))
        interval = min(interval * 1.5, 5.0)


def release_slot(slot: Slot) -> None:
    """Release the slot this lease holds. Logged no-op if it no longer owns it."""
    try:
        suite_mutex.release(slot.token, path=slot_path(slot.index))
    except Exception as exc:
        _LOG.warning("tier_t_slots: release of slot %d failed: %s", slot.index, exc)


@contextmanager
def held_slot(owner: str, cmd: str, timeout: float = 0.0, *,
              pid: Optional[int] = None) -> Iterator[Optional[Slot]]:
    """Hold a slot for the block; yields the ``Slot`` or None if none was free.

    Releases on normal exit and on exception alike, and only when this call
    actually acquired — a None yield releases nothing, so a failed acquire can
    never free someone else's slot.
    """
    slot = acquire_slot(owner, cmd, timeout, pid=pid)
    try:
        yield slot
    finally:
        if slot is not None:
            release_slot(slot)
