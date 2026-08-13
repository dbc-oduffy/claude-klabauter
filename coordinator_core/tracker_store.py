"""
coordinator_core.tracker_store — sovereign-tracker per-machine event store.

Purpose: a git-durable, append-only event log for the sovereign action-item
tracker, sharded one file per machine so that concurrent writers on
different hosts never contend for the same bytes and never git-conflict at
EOF. Each repo that wants a sovereign tracker instantiates this module
against its own ``repo_root`` / ``state/sovereign-tracker/`` — this module
is a library, not a fleet-wide service, and holds no op registration.

Spec backlink: pln-sat-01-sovereign-tracker-subst-a66742
§ Pinned module interface, § The multi-machine correction (DEC-3/DEC-5).

Ordering contract: ``read_events`` FILTERS to ``applied_at``-populated
events (cockpit-ratified §4.3 — an event that has not cleared reconcile
does not participate in projection at all) and sorts the remainder on the
three-key tuple ``(applied_at, observed_at, id)``. ``machine``, ``sequence``,
and ``logical_clock`` are deliberately absent from the sort key: ``id`` is
Cockpit's globally-unique primary key, so nothing below it in the tuple is
reachable, and ``logical_clock`` is written for a future cockpit merge
policy but is not authoritative here (DEC-10).

Also present as of sat-01b: the observed-set-fold marker mechanism
(``fold_observed_set``, ``resolve_observed_set``,
``resolve_observed_set_for_event``, ``OBSERVED_SET_UNKNOWN``) — a
machine periodically appends a self-describing ``observed_set_fold``
marker to its own shard recording, content-bound and re-derivable on
every read, which peer bytes it had observed at fold time. See each
function's own docstring for the full contract; spec backlink
docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md § Design.

Negative-spec:
  - Do NOT make ``logical_clock`` authoritative or invent a merge policy —
    that is cockpit's contract to deliver (DEC-10). The field is written
    and never read by this module.
  - Do NOT add a global (cross-shard) read to ``append_event`` — within-shard
    monotonicity from the tail is the whole obligation; a true cross-shard
    clock requires observing peer events at write time, which is exactly
    the deferred merge policy this module must not design.
  - Do NOT grow ``read_events`` into a query surface (DEC-12) — no filters,
    projections, pagination, or cross-repo reach. It is the reference
    implementation of one repo's ordering contract, not an API.
  - Do NOT resolve ``repo_root`` against this repo's own tree, and do NOT
    add a fleet-wide aggregating read (DEC-11) — every entrypoint takes
    ``repo_root`` and ``EVENTS_DIR_RELPATH`` is a relpath on purpose.
  - Do NOT treat ``sequence`` as a global key. It is unique only within
    ``(machine, sequence)`` — two shards legitimately share a sequence
    value after an offline merge, and that is expected, not an error.
  - Do NOT collapse ``OBSERVED_SET_UNKNOWN`` into ``{}`` or a trusted
    maximum anywhere in this module (fold, resolve, or any future
    caller). That collapse is the exact over-claim cockpit's property
    (c) exists to prevent — see docs/plans/2026-07-28-sat-01b-observed-
    set-fold-actuator.md § Anti-scope.
  - Do NOT open a second ``locked_rmw`` around the ``append_event`` call
    inside ``fold_observed_set``. ``append_event`` already acquires
    ``locked_rmw`` internally on its own shard; a second acquisition on
    the same target in the same process deadlocks until ``LockTimeout``
    (``locked_write.py:38-41``).
  - Do NOT implement ``append_events`` by looping ``append_event``.
    ``append_events`` acquires ``locked_rmw`` exactly once for the whole
    batch; nothing inside it may call ``append_event``. Looping
    ``append_event`` for a multi-event cascade gives N lock acquisitions
    and an observable partial cascade, which is the bug this primitive
    exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from coordinator_core.locked_write import locked_rmw

# NOTE: `coordinator_core.ops.emit._slug.machine_slug` is NOT imported at
# module scope. `coordinator_core.ops.emit` is a submodule of
# `coordinator_core.ops`, so a top-level import here requires initializing
# `coordinator_core/ops/__init__.py` first, which eager-imports every op
# module -- including `ops/tracker/fold_observed_set.py`, which imports THIS
# module (`EVENTS_DIR_RELPATH` et al.) at its own module scope. A top-level
# import here therefore closes a real import cycle (tracker_store ->
# coordinator_core.ops -> ops.tracker.fold_observed_set -> tracker_store),
# confirmed by `python3 -c "import coordinator_core.tracker_store"` emitting
# three "FAILED to import" warnings for ops.session.boot_sweep,
# ops.session.sweep_consumed_handoffs, and ops.tracker.fold_observed_set, each
# an ImportError: "cannot import name 'EVENTS_DIR_RELPATH' from partially
# initialized module ... (most likely due to a circular import)". See
# `coordinator_core/reconcile/commit_reality.py` for the same idiom against
# the same package.
#
# `machine_slug` below is a thin module-level wrapper, not a re-export --
# this keeps `tracker_store.machine_slug` a stable, monkeypatchable public
# name (the test suite patches it via `monkeypatch.setattr(ts, "machine_slug",
# ...)`) while deferring the real import to call time, after all modules have
# finished loading, which sidesteps the cycle.


def machine_slug() -> str:
    """Lazy-imported delegate to ``coordinator_core.ops.emit._slug.machine_slug``.

    See the module-level NOTE above this function for why the import is
    deferred to call time rather than hoisted to module scope.
    """
    from coordinator_core.ops.emit._slug import machine_slug as _machine_slug

    return _machine_slug()


EVENTS_DIR_RELPATH = "state/sovereign-tracker"
EVENTS_SHARD_GLOB = "events.*.jsonl"


class _UnknownObservedSet:
    """Sentinel for "the bytes that justified this claim are not the bytes
    present" — the third value of ``resolve_observed_set``'s and
    ``resolve_observed_set_for_event``'s three-valued return.

    Distinguished from a genuinely-empty ``{}`` observed set (a machine that
    really did observe nothing) and from a concrete non-empty dict (a
    validated claim). ``__bool__`` returns ``False`` deliberately — this
    sentinel and ``{}`` are both falsy — so callers must never test truthiness
    to distinguish the two; assert identity/type instead (see AC6's test,
    which proves the API still distinguishes them despite the shared
    falsiness).

    Negative-spec: a future editor must NOT collapse this sentinel into
    ``{}`` or into a trusted maximum anywhere in this module. That collapse
    is the exact over-claim cockpit's property (c) exists to prevent — see
    docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md § Anti-scope.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "OBSERVED_SET_UNKNOWN"

    def __bool__(self) -> bool:
        return False


OBSERVED_SET_UNKNOWN = _UnknownObservedSet()
"""The module-level singleton instance of ``_UnknownObservedSet`` — the
canonical ``unknown`` return value for ``resolve_observed_set`` and
``resolve_observed_set_for_event``. Compare with ``is``, not ``==``."""


class TrackerStoreError(Exception):
    """Raised for a malformed or contract-violating tracker-store operation.

    Covers: a missing ``id``/``observed_at`` on append, an ``id`` that
    already appears in this machine's own shard, a malformed tail line read
    for the sequence/logical-clock bump, or an ``id`` that is visibly not
    globally-unique-guaranteeing (DR-241 bound (i)).
    """


class TrackerStoreDuplicateIdError(TrackerStoreError):
    """Raised specifically when an appended ``id`` already appears in this
    machine's own shard.

    A structural subtype of ``TrackerStoreError`` (every existing
    ``except TrackerStoreError`` still catches this) that exists so a
    caller needing to distinguish THIS specific condition — e.g.
    ``fold_observed_set``'s concurrent-fold race catch — can match on
    exception type instead of parsing ``append_event``'s human-readable
    message text.
    """


def _now_ms() -> int:
    """Return the current wall-clock time in integer milliseconds.

    Module-level seam so tests can monkeypatch the wall-clock read to
    exercise a same-millisecond counter bump or a backwards clock step
    deterministically, without sleeping (pinned at dispatch time for
    AC1h — see the plan's § EM decision pinned at dispatch time).
    """
    return int(time.time() * 1000)


def shard_path(repo_root: Path, *, machine: str | None = None) -> Path:
    """Absolute path to ONE machine's shard.

    Defaults to this machine's shard (``machine_slug()``). Callers pass
    *machine* only to read a peer's shard — never to write one; see
    ``append_event``, which has no *machine* parameter by design.
    """
    slug = machine if machine is not None else machine_slug()
    return repo_root / EVENTS_DIR_RELPATH / f"events.{slug}.jsonl"


def _split_lines(text: str) -> list[str]:
    """Split shard text into non-empty lines, tolerating any line ending."""
    return [line for line in text.splitlines() if line.strip()]


def _id_not_bare_digit_string(event_id: object) -> bool:
    """Return False only for the one shape DR-241 bound (i) can see is wrong.

    sat-01 owns no entity schema (sat-02's job) but DR-241 bound (i)
    requires the id to be a content-derived, globally-unique-guaranteeing
    value. This module cannot validate the full scheme — a ``True`` result
    here means only "not visibly wrong", NOT "verified globally unique".
    It refuses the one shape it can see is wrong on its face: a bare digit
    string, which is exactly what a shard-local ``sequence`` value looks
    like if it were mistakenly reused as the event id. Full-scheme
    enforcement (a UUID or a ``<machine>-<seq>``-qualified id) is owed at
    sat-02/sat-06, not here.
    """
    if not isinstance(event_id, str) or not event_id.strip():
        return False
    return not event_id.strip().isdigit()


def _next_clock(prior_wall_ms: int, prior_counter: int, *, now_ms: int) -> tuple[int, int]:
    """Bump ``(wall_ms, counter)`` off the given prior pair.

    The shared rule behind ``append_event``'s single-event bump and
    ``append_events``' per-event chaining: ``wall_ms`` never regresses
    below the prior pair's ``wall_ms``, and ``counter`` resets to 0 unless
    this event lands in the same millisecond as the prior pair, in which
    case it increments. ``append_event`` calls this once, chaining off the
    shard tail; ``append_events`` calls this once per batch event,
    chaining event *k* off event *k-1*'s OWN just-assigned pair (only the
    first batch event chains off the shard tail) — see ``append_events``'
    docstring for why that distinction matters.
    """
    wall_ms = max(now_ms, prior_wall_ms)
    counter = (prior_counter + 1) if wall_ms == prior_wall_ms else 0
    return wall_ms, counter


def _validate_event_fields(event: dict) -> None:
    """Pre-lock field validation shared by ``append_event`` and
    ``append_events``: ``id`` present, ``observed_at`` present, and ``id``
    not visibly a bare digit string (DR-241 bound (i))."""
    if "id" not in event or not event["id"]:
        raise TrackerStoreError("event is missing required field: id")
    if "observed_at" not in event or not event["observed_at"]:
        raise TrackerStoreError("event is missing required field: observed_at")
    if not _id_not_bare_digit_string(event["id"]):
        raise TrackerStoreError(
            f"event id {event['id']!r} is a bare digit string, which is "
            "visibly shard-local rather than content-derived (DR-241 "
            "bound (i)) — passing this check does not verify full "
            "global uniqueness"
        )


def append_event(event: dict, *, repo_root: Path) -> dict:
    """Append *event* to THIS MACHINE'S shard under an exclusive same-host lock.

    Wraps ``locked_rmw`` over this machine's shard only, treating the
    read-assign-append cycle (duplicate check, sequence bump, logical-clock
    bump, serialize, write) as one atomic operation under a single lock
    acquisition — never a split counter-then-append.
    """
    _validate_event_fields(event)

    target = shard_path(repo_root)
    assigned: dict = {}

    def _mutate(old_text: str) -> str:
        lines = _split_lines(old_text)

        # Own-shard duplicate detection: one extra pass over data already
        # read into memory for the sequence bump. A line that fails to
        # parse is skipped here (not raised) — only the TAIL line's
        # malformedness is fatal to append_event; an earlier malformed
        # line surfaces at read_events instead. The loop always visits
        # lines[-1] last, so tail_parsed/tail_parse_error below reflect
        # that line's parse result without a second json.loads call.
        tail_parsed: object = None
        tail_parse_error: json.JSONDecodeError | None = None
        for line in lines:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError as exc:
                tail_parsed = None
                tail_parse_error = exc
                continue
            tail_parsed = existing
            tail_parse_error = None
            if isinstance(existing, dict) and existing.get("id") == event["id"]:
                raise TrackerStoreDuplicateIdError(
                    f"event id {event['id']!r} already appears in this shard"
                )

        if lines:
            if tail_parse_error is not None:
                raise TrackerStoreError(
                    f"malformed tail line in shard {target}: {tail_parse_error}"
                ) from tail_parse_error
            tail = tail_parsed
            if not isinstance(tail, dict):
                raise TrackerStoreError(
                    f"malformed tail line in shard {target}: not a JSON object"
                )
            tail_sequence = tail.get("sequence", 0)
            if not isinstance(tail_sequence, int):
                tail_sequence = 0
            tail_clock = tail.get("logical_clock") or {}
            tail_wall_ms = tail_clock.get("wall_ms", 0) if isinstance(tail_clock, dict) else 0
            tail_counter = tail_clock.get("counter", 0) if isinstance(tail_clock, dict) else 0
        else:
            tail_sequence = 0
            tail_wall_ms = 0
            tail_counter = 0

        wall_ms, counter = _next_clock(tail_wall_ms, tail_counter, now_ms=_now_ms())

        new_event = dict(event)
        new_event["machine"] = machine_slug()
        new_event["sequence"] = tail_sequence + 1
        new_event["logical_clock"] = {"wall_ms": wall_ms, "counter": counter}
        assigned["event"] = new_event

        new_line = json.dumps(new_event, sort_keys=True) + "\n"
        if old_text and not old_text.endswith("\n"):
            return old_text + "\n" + new_line
        return old_text + new_line

    locked_rmw(target, _mutate, repo_root=repo_root, missing_ok=True)
    return assigned["event"]


def append_events(events: list[dict], *, repo_root: Path) -> list[dict]:
    """Append *events* to THIS MACHINE'S shard under ONE exclusive same-host
    lock — the F2 transactional primitive. NOT composable from
    ``append_event``: looping it would take N lock acquisitions and expose
    an observable partial cascade (see the module-docstring negative-spec
    bullet).

    Two textually separate passes:

    1. Pre-lock (pure functions of the batch, before ``locked_rmw`` is
       acquired): every event is field-validated exactly as
       ``append_event`` validates a single event, and intra-batch
       duplicate ids are rejected. An invalid batch writes nothing and
       never touches the lock.
    2. In-lock (inside the single ``_mutate``, because it depends on the
       shard tail): own-shard collision detection — every EXISTING shard
       line's id is checked against ``seen_batch_ids``, the full,
       pre-lock-computed set of every id in the batch (en masse, not an
       incrementally-grown in-lock accept-set), and a hit raises
       ``TrackerStoreDuplicateIdError`` and writes nothing (AC3).
       Intra-batch duplicates are never seen here — pass 1 already
       rejected them before the lock was touched. ``sequence`` is
       assigned ``tail+1, tail+2, … tail+N`` in input order within the
       one ``_mutate`` return.

    The clock rule: event *k* chains its ``(wall_ms, counter)`` off event
    *k-1*'s OWN just-assigned pair, NOT off the original shard tail for
    every event — chaining every event off the original tail would give
    all N events an identical pair, a silent collision invisible to a
    happy-path test. Only the FIRST batch event chains off the shard
    tail, exactly as ``append_event`` does today. Both functions share
    the bump rule via ``_next_clock``.

    Any exception raised inside ``_mutate`` — including
    ``locked_write.MutateAbort`` — propagates out of ``append_events``
    unchanged; ``locked_rmw`` honours ``MutateAbort`` as a clean no-write
    abort (no partial write, no shard mutation). ``append_events`` does
    not swallow or re-wrap either case.

    Atomicity comes free from ``locked_rmw``'s existing ``os.replace``
    crash-safety — the obligation here is to do all N assignments in ONE
    ``_mutate`` return, never to call ``locked_rmw`` twice (see the
    module-docstring negative-spec bullet).

    Empty *events* is a no-op returning ``[]`` — the lock is never
    acquired.
    """
    if not events:
        return []

    # Pass 1 — pre-lock, pure functions of the batch.
    for event in events:
        _validate_event_fields(event)

    seen_batch_ids: set = set()
    for event in events:
        event_id = event["id"]
        if event_id in seen_batch_ids:
            raise TrackerStoreDuplicateIdError(
                f"event id {event_id!r} appears more than once in this batch"
            )
        seen_batch_ids.add(event_id)

    target = shard_path(repo_root)
    assigned: dict = {}

    def _mutate(old_text: str) -> str:
        lines = _split_lines(old_text)

        # Own-shard duplicate detection: one extra pass over data already
        # read into memory for the sequence bump, exactly as
        # append_event's own pass — see that function's comment. The loop
        # always visits lines[-1] last, so tail_parsed/tail_parse_error
        # below reflect that line's parse result without a second
        # json.loads call.
        tail_parsed: object = None
        tail_parse_error: json.JSONDecodeError | None = None
        for line in lines:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError as exc:
                tail_parsed = None
                tail_parse_error = exc
                continue
            tail_parsed = existing
            tail_parse_error = None
            if isinstance(existing, dict) and existing.get("id") in seen_batch_ids:
                raise TrackerStoreDuplicateIdError(
                    f"event id {existing.get('id')!r} already appears in this shard"
                )

        if lines:
            if tail_parse_error is not None:
                raise TrackerStoreError(
                    f"malformed tail line in shard {target}: {tail_parse_error}"
                ) from tail_parse_error
            tail = tail_parsed
            if not isinstance(tail, dict):
                raise TrackerStoreError(
                    f"malformed tail line in shard {target}: not a JSON object"
                )
            tail_sequence = tail.get("sequence", 0)
            if not isinstance(tail_sequence, int):
                tail_sequence = 0
            tail_clock = tail.get("logical_clock") or {}
            tail_wall_ms = tail_clock.get("wall_ms", 0) if isinstance(tail_clock, dict) else 0
            tail_counter = tail_clock.get("counter", 0) if isinstance(tail_clock, dict) else 0
        else:
            tail_sequence = 0
            tail_wall_ms = 0
            tail_counter = 0

        machine = machine_slug()
        wall_ms, counter = tail_wall_ms, tail_counter
        new_lines: list[str] = []
        assigned_events: list[dict] = []
        for offset, event in enumerate(events, start=1):
            wall_ms, counter = _next_clock(wall_ms, counter, now_ms=_now_ms())
            new_event = dict(event)
            new_event["machine"] = machine
            new_event["sequence"] = tail_sequence + offset
            new_event["logical_clock"] = {"wall_ms": wall_ms, "counter": counter}
            assigned_events.append(new_event)
            new_lines.append(json.dumps(new_event, sort_keys=True) + "\n")

        assigned["events"] = assigned_events

        appended = "".join(new_lines)
        if old_text and not old_text.endswith("\n"):
            return old_text + "\n" + appended
        return old_text + appended

    locked_rmw(target, _mutate, repo_root=repo_root, missing_ok=True)
    return assigned["events"]


def read_events(*, repo_root: Path) -> list[dict]:
    """Read every shard, filter to ``applied_at``-populated events, sort.

    Sorted on the three-key tuple ``(applied_at, observed_at, id)`` only —
    ``machine``, ``sequence``, and ``logical_clock`` are absent from the
    sort key and must stay absent. Takes no lock: appends are atomic via
    ``os.replace``, so a reader sees a whole file or the prior whole file,
    never a torn one.

    Load-bearing for ``fold_observed_set``'s markers: the ``applied_at is
    None`` exclusion below is what keeps an ``observed_set_fold`` marker
    (always written with ``"applied_at": None`` — see that function) out
    of the sort. Removing or narrowing this exclusion without also
    updating the sort key would let a marker reach ``events.sort`` and
    compare ``None`` against a string in the ``(applied_at, observed_at,
    id)`` tuple, raising ``TypeError``.
    """
    shard_dir = repo_root / EVENTS_DIR_RELPATH
    events: list[dict] = []
    if shard_dir.is_dir():
        for shard_file in sorted(shard_dir.glob(EVENTS_SHARD_GLOB)):
            text = shard_file.read_text(encoding="utf-8")
            for line in _split_lines(text):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TrackerStoreError(
                        f"malformed line in shard {shard_file}: {exc}"
                    ) from exc
                if not isinstance(record, dict):
                    raise TrackerStoreError(
                        f"malformed line in shard {shard_file}: not a JSON object"
                    )
                # Load-bearing for fold_observed_set's markers, which are
                # always written with "applied_at": None — see that
                # function's marker construction. If this exclusion is
                # ever removed/narrowed, a marker would reach the sort
                # below and compare None against a string, raising
                # TypeError.
                if record.get("applied_at") is not None:
                    events.append(record)

    events.sort(key=lambda e: (e.get("applied_at"), e.get("observed_at"), e.get("id")))
    return events


def max_sequence(*, repo_root: Path, machine: str | None = None) -> int:
    """Highest sequence in one machine's shard (this machine's by default).

    Returns 0 for an absent or empty shard. Parses only the last
    non-empty line of the shard — sequence is monotonic per-shard by
    construction, so the tail always carries the max.
    """
    path = shard_path(repo_root, machine=machine)
    if not path.exists():
        return 0

    lines = _split_lines(path.read_text(encoding="utf-8"))
    if not lines:
        return 0

    try:
        tail = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise TrackerStoreError(
            f"malformed tail line in shard {path}: {exc}"
        ) from exc
    if not isinstance(tail, dict):
        raise TrackerStoreError(
            f"malformed tail line in shard {path}: not a JSON object"
        )

    sequence = tail.get("sequence", 0)
    return sequence if isinstance(sequence, int) else 0


def _prefix_digest(event_ids: list[str]) -> str:
    """Pinned canonical digest over one peer shard's event-id prefix.

    ``sha256(json.dumps(event_ids, separators=(",", ":")))[:16]`` — a plain
    JSON list. ``sort_keys`` is inapplicable here (it only reorders dict
    keys during serialization; ``event_ids`` is a flat ``list[str]`` with
    no nested dicts, so ``sort_keys`` would be a no-op either way). What
    actually matters is that list order is preserved by construction —
    this is a *prefix* digest, and reordering the ids would hide a
    mid-prefix substitution. C2's ``resolve_observed_set`` MUST recompute this exact
    same serialization over the same shard-order id list to validate a
    marker's claim — see docs/plans/2026-07-28-sat-01b-observed-set-fold-
    actuator.md § Design, "The marker event — content-bound, not
    position-bound".
    """
    return hashlib.sha256(
        json.dumps(event_ids, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def fold_observed_set(*, repo_root: Path) -> dict | None:
    """Read every PEER shard and append ONE ``observed_set_fold`` marker to
    this machine's own shard, recording what this machine has observed.

    Returns the assigned marker event dict on a successful append, or
    ``None`` if there is no store at *repo_root* (opt-in by existence — this
    function never mints ``EVENTS_DIR_RELPATH``) or if the fold is a no-op
    because this exact ``observed_set`` was already recorded by a prior
    marker on this machine's own shard (idempotent on its own terms, see
    below).

    The vector covers PEER shards only — never the calling machine's own
    shard (that position is recorded by where the marker itself lands, and
    including it would make every fold's ``observed_set`` differ from the
    last, defeating idempotent re-fold). A peer shard with zero events is
    excluded from the vector entirely, so a genuinely-empty ``observed_set``
    stays distinguishable from "no peers observed anything" only by that
    exclusion rule, not by a zero-valued entry.

    Bootstrap (the fresh-clone case): a machine that has never folded before
    has no marker on its own shard, so its position has never been recorded.
    This function needs no special-casing for that case — an ordinary first
    call already IS the bootstrap: it appends a marker, and that marker's
    placement in the own shard becomes the recorded position, rather than
    having it inferred from a naive empty-vector default (cockpit's named
    hazard: an empty vector would read the machine's first events as
    concurrent with all prior history of every peer — "a wrong answer, not a
    tuning problem"). Before that first fold, any query for this machine's
    position correctly answers ``OBSERVED_SET_UNKNOWN``
    (``resolve_observed_set_for_event`` finds no marker at all) — never an
    empty set and never a trusted maximum. If the bootstrap fold finds zero
    peer shards with any events, the resulting ``observed_set`` is a
    legitimate empty ``{}`` (AC6) — a positive assertion this machine made
    about itself at fold time — which stays distinct from the absence of a
    marker altogether.

    Each vector component is ``{"sequence": <peer's max_sequence>,
    "prefix_digest": <_prefix_digest over that peer's ids 1..N in shard-file
    order>}`` — content-bound, not position-bound, per DR-241 bound (i) and
    the plan's critical revision (a bare ``{machine: sequence}`` vector
    cannot distinguish "the bytes are still here" from "different bytes now
    occupy those positions" after a rebase-and-re-append).

    Each peer shard is read EXACTLY ONCE (a single ``read_text()`` call).
    Both ``sequence`` and ``prefix_digest`` are derived from that one read
    — ``sequence`` from the last record encountered with a valid int
    ``sequence`` field, ``prefix_digest`` from the ``event_ids`` built in
    the same pass. This function must NOT call ``max_sequence`` (which
    does its own independent ``read_text()`` of the same shard) as a
    second, separate read: a peer append landing between two reads of the
    same shard would compute ``sequence`` and ``prefix_digest`` from two
    different byte-states, minting a component that can never resolve
    concrete again. A record is included in the vector only if its own
    ``sequence`` field is a valid int (the same predicate
    ``resolve_observed_set`` applies) and only if its ``id`` is present
    and truthy — a record failing the latter is a shard-corruption
    contract violation, not a silently-admitted ``None``, and raises
    ``TrackerStoreError``.

    Idempotency is computed and enforced HERE, not delegated to
    ``append_event``'s duplicate-id rejection: the marker id is derived from
    the *content* of ``observed_set`` (see below), so an unchanged
    ``observed_set`` always re-derives the same id. This function checks its
    own shard for that id BEFORE appending and returns ``None`` without
    calling ``append_event`` if found. A second, narrower check treats a
    ``TrackerStoreDuplicateIdError`` raised by the append itself (the
    concurrent-fold race window between this function's own check and the
    append) as the same benign no-op — that specific exception type only;
    any other ``TrackerStoreError`` (malformed tail, missing field)
    propagates.

    Negative-spec — a future editor must NOT:
      - Open a second ``locked_rmw`` around the ``append_event`` call below.
        ``append_event`` already acquires ``locked_rmw`` internally; a second
        acquisition on the same target in the same process deadlocks until
        ``LockTimeout`` (``locked_write.py:38-41``). Peer shards are read
        lock-free here on purpose — appends are atomic via ``os.replace``,
        so a reader sees a whole file or the prior whole file, never a torn
        one.
      - Rely on ``append_event``'s duplicate-id rejection AS the idempotency
        mechanism. It raises ``TrackerStoreError`` rather than no-op'ing
        (``append_event`` at the own-shard duplicate-detection pass above);
        treating that raise as "idempotency" would make this function raise
        on every quiet re-fold once the vector stops moving, rather than
        silently doing nothing.

    Spec backlink: pln-sat-01b-observed-set-fold-actu-8b3f7a
    § Design, § Tasks C1.
    """
    shard_dir = repo_root / EVENTS_DIR_RELPATH
    if not shard_dir.is_dir():
        return None

    own_slug = machine_slug()
    prefix = "events."
    suffix = ".jsonl"

    observed_set: dict[str, dict[str, object]] = {}
    for shard_file in sorted(shard_dir.glob(EVENTS_SHARD_GLOB)):
        name = shard_file.name
        slug = name[len(prefix):-len(suffix)]
        if slug == own_slug:
            continue

        # Exactly one read_text() per peer shard: sequence and
        # prefix_digest below are BOTH derived from this single read, not
        # from a second, independent call to max_sequence() — see the
        # docstring's TOCTOU note above.
        text = shard_file.read_text(encoding="utf-8")
        event_ids: list[str] = []
        current_sequence = 0
        for line in _split_lines(text):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TrackerStoreError(
                    f"malformed line in shard {shard_file}: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise TrackerStoreError(
                    f"malformed line in shard {shard_file}: not a JSON object"
                )
            sequence = record.get("sequence")
            if not isinstance(sequence, int):
                continue
            record_id = record.get("id")
            if not record_id:
                raise TrackerStoreError(
                    f"malformed record in shard {shard_file}: missing or "
                    "empty 'id'"
                )
            event_ids.append(record_id)
            current_sequence = sequence

        if not event_ids:
            continue

        observed_set[slug] = {
            "sequence": current_sequence,
            "prefix_digest": _prefix_digest(event_ids),
        }

    digest = hashlib.sha256(
        json.dumps(observed_set, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    marker_id = f"{own_slug}-fold-{digest}"

    own_shard = shard_path(repo_root)
    if own_shard.exists():
        for line in _split_lines(own_shard.read_text(encoding="utf-8")):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # Lenient by design, unlike append_event's tail parse: a
                # malformed line here just means this pre-check can't
                # confirm idempotency from it, not that the shard is
                # unusable. The append below still runs and, if the
                # malformed line is the tail, append_event's own tail
                # parse independently raises TrackerStoreError for it —
                # nothing is silently swallowed.
                continue
            if isinstance(record, dict) and record.get("id") == marker_id:
                return None

    marker = {
        "id": marker_id,
        "kind": "observed_set_fold",
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now_ms() / 1000)),
        # Load-bearing: read_events' "applied_at is not None" filter relies
        # on this staying None to keep markers out of the projection. If
        # that filter is ever removed/narrowed, this marker would reach
        # events.sort and compare None against a string, raising TypeError.
        "applied_at": None,
        "observed_set": observed_set,
    }

    try:
        return append_event(marker, repo_root=repo_root)
    except TrackerStoreDuplicateIdError:
        return None


def resolve_observed_set(
    marker: dict, *, repo_root: Path
) -> dict | _UnknownObservedSet:
    """Re-derive an ``observed_set_fold`` marker's justification against
    CURRENT bytes on every call — nothing is trusted because it was once
    written.

    Returns one of three values (DEC-F, never conflated):
      - a concrete ``{machine: {sequence, prefix_digest}}`` mapping — every
        claimed peer component still validates against current bytes;
      - ``{}`` — the marker genuinely observed nothing (its own
        ``observed_set`` was empty at fold time);
      - ``OBSERVED_SET_UNKNOWN`` — at least one claimed peer component no
        longer validates: the peer shard's current tail is below the claimed
        ``sequence`` (the justifying bytes have left the repository), or the
        recomputed prefix digest over the claimed prefix no longer matches
        the claimed ``prefix_digest`` (tail substitution, mid-prefix
        substitution, or a prefix-closure hole — one digest comparison
        subsumes all three, per § Design; no separate hole-detection rule is
        added here).

    A malformed ``marker["observed_set"]`` (missing or not a dict) is a
    contract violation, not an ``unknown`` — raises ``TrackerStoreError``.

    Each claimed peer shard is read EXACTLY ONCE (a single ``read_text()``
    call) per component — this function must NOT call ``max_sequence``
    (which does its own independent ``read_text()`` of the same shard) as
    a separate read. Both ``current_max`` (the tail-derived sequence used
    for the below-claimed-sequence check) and ``event_ids`` (the
    recomputed digest input) are derived from that one read, for the same
    TOCTOU reason documented on ``fold_observed_set``: a peer append
    landing between two reads of the same shard could make the sequence
    check and the digest recompute disagree about which bytes they are
    each validating against.

    Negative-spec — a future editor must NOT:
      - Collapse ``OBSERVED_SET_UNKNOWN`` into ``{}`` or into a trusted
        maximum anywhere in this function. That collapse is the exact
        over-claim cockpit's property (c) exists to prevent.
      - Re-implement the prefix-digest serialization inline. ALWAYS call
        ``_prefix_digest`` — any drift between fold-time and resolve-time
        digest computation silently breaks the whole mechanism.
      - Validate a claimed component via the sequence check alone. A shard
        whose current tail sequence is >= the claimed value can still have
        had its claimed prefix's bytes replaced (the rebase-and-re-append
        counterexample, § Design "The marker event — content-bound, not
        position-bound") — the full-prefix read and digest recompute is
        mandatory, not an optimization to skip when the sequence check
        passes.

    PROVISIONAL, not a closed reimplementation of cockpit's own
    well-formedness/prefix-closure validator — whether this digest-mismatch
    predicate lands on the same `unknown` cases as theirs is C0's second
    first-wave ask (§ Design, "The prefix-closure/`unknown` predicate is
    provisional"). Revisit if their answer names a different contract.

    Spec backlink: pln-sat-01b-observed-set-fold-actu-8b3f7a
    § Design, "Resolution, and the three-valued return"; § Tasks C2.
    """
    observed_set = marker.get("observed_set")
    if not isinstance(observed_set, dict):
        raise TrackerStoreError(
            "marker is missing a well-formed 'observed_set' mapping: "
            f"{marker.get('observed_set')!r}"
        )
    if not observed_set:
        return {}

    for slug, claim in observed_set.items():
        if not isinstance(claim, dict):
            raise TrackerStoreError(
                f"marker observed_set component for {slug!r} is not a dict: {claim!r}"
            )
        claimed_seq = claim.get("sequence")
        claimed_digest = claim.get("prefix_digest")
        if not isinstance(claimed_seq, int):
            raise TrackerStoreError(
                f"marker observed_set component for {slug!r} has a non-int "
                f"sequence: {claimed_seq!r}"
            )

        # Exactly one read_text() per claimed peer shard: current_max and
        # event_ids below are BOTH derived from this single read, not from
        # a separate max_sequence() call followed by a second read — see
        # the docstring's TOCTOU note above.
        path = shard_path(repo_root, machine=slug)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = _split_lines(text)

        if lines:
            try:
                tail = json.loads(lines[-1])
            except json.JSONDecodeError as exc:
                raise TrackerStoreError(
                    f"malformed tail line in shard {path}: {exc}"
                ) from exc
            if not isinstance(tail, dict):
                raise TrackerStoreError(
                    f"malformed tail line in shard {path}: not a JSON object"
                )
            tail_sequence = tail.get("sequence", 0)
            current_max = tail_sequence if isinstance(tail_sequence, int) else 0
        else:
            current_max = 0

        if current_max < claimed_seq:
            return OBSERVED_SET_UNKNOWN

        event_ids: list[str] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TrackerStoreError(
                    f"malformed line in shard {path}: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise TrackerStoreError(
                    f"malformed line in shard {path}: not a JSON object"
                )
            sequence = record.get("sequence")
            if isinstance(sequence, int) and sequence <= claimed_seq:
                record_id = record.get("id")
                if not record_id:
                    raise TrackerStoreError(
                        f"malformed record in shard {path}: missing or "
                        "empty 'id'"
                    )
                event_ids.append(record_id)

        recomputed_digest = _prefix_digest(event_ids)
        if recomputed_digest != claimed_digest:
            return OBSERVED_SET_UNKNOWN

    return observed_set


def resolve_observed_set_for_event(
    event: dict, *, repo_root: Path
) -> dict | _UnknownObservedSet:
    """Map an arbitrary event to the marker in effect for it, then resolve.

    Implements § Design's event→marker mapping — the answer to cockpit's
    property (a) for any event, byte-derivable with NO peer-shard access:
    the marker in effect for event E at sequence ``s`` in machine M's shard
    is the most recent record in M's OWN shard with ``kind ==
    "observed_set_fold"`` and ``sequence < s``. ``M`` and ``s`` come from
    *event*'s own ``"machine"`` and ``"sequence"`` fields.

    Returns ``OBSERVED_SET_UNKNOWN`` — never ``{}`` — if no such marker
    exists (the event predates M's first-ever fold). This is the
    retroactivity limitation the plan states plainly: it is the safe answer,
    not a bug to design around (§ Design, "The event→marker mapping").
    Otherwise delegates to ``resolve_observed_set``.

    Negative-spec — a future editor must NOT treat "no marker found" as
    "observed nothing" (``{}``). Those are different claims: an empty
    ``observed_set`` is a positive assertion a machine made about itself at
    fold time; no marker at all means no assertion was ever made, which is
    exactly ``unknown``.

    PROVISIONAL alongside ``resolve_observed_set`` — see that function's
    docstring for the pending-cockpit-answer caveat, which applies here too
    since this function delegates to it.

    Spec backlink: pln-sat-01b-observed-set-fold-actu-8b3f7a
    § Design, "The event→marker mapping"; § Tasks C2.
    """
    machine = event.get("machine")
    sequence = event.get("sequence")
    if not isinstance(machine, str) or not machine:
        raise TrackerStoreError(f"event is missing required field: machine ({event!r})")
    if not isinstance(sequence, int):
        raise TrackerStoreError(f"event is missing required field: sequence ({event!r})")

    path = shard_path(repo_root, machine=machine)
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    latest_marker: dict | None = None
    latest_marker_sequence = -1
    for line in _split_lines(text):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrackerStoreError(f"malformed line in shard {path}: {exc}") from exc
        if not isinstance(record, dict):
            raise TrackerStoreError(f"malformed line in shard {path}: not a JSON object")
        if record.get("kind") != "observed_set_fold":
            continue
        record_sequence = record.get("sequence")
        if not isinstance(record_sequence, int) or record_sequence >= sequence:
            continue
        if record_sequence > latest_marker_sequence:
            latest_marker_sequence = record_sequence
            latest_marker = record

    if latest_marker is None:
        return OBSERVED_SET_UNKNOWN

    return resolve_observed_set(latest_marker, repo_root=repo_root)
