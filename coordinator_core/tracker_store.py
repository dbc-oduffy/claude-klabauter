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
        # Merges the flat live shard (events.<slug>.jsonl) with any
        # rotated-out months (<YYYY-MM>/events.<slug>.jsonl) -- rotation
        # (rotate_month) relocates a closed month's lines out of the live
        # shard but never renumbers or reorders them, and the cross-shard
        # sort below recovers the right order regardless of which file a
        # line currently lives in. See docs/plans/2026-08-18-sat-06-
        # cockpit-consumption-seam.md § RULING 2026-08-20.
        shard_files = sorted(shard_dir.glob(EVENTS_SHARD_GLOB)) + sorted(
            shard_dir.glob(f"*/{EVENTS_SHARD_GLOB}")
        )
        for shard_file in shard_files:
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


def _peer_shard_paths(repo_root: Path, slug: str) -> list[Path]:
    """Every shard file holding *slug*'s full event history, oldest to
    newest: any rotated ``<YYYY-MM>/events.<slug>.jsonl`` partitions in
    ascending month order, followed by the live flat shard
    (``events.<slug>.jsonl``) if it exists.

    Read order matters to callers that fold this into a running digest or
    sequence: ``sequence`` is monotonic across a machine's whole history and
    ``rotate_month`` never renumbers it, so a rotated month's lines always
    precede whatever remains in the live shard.
    """
    shard_dir = repo_root / EVENTS_DIR_RELPATH
    rotated = sorted(shard_dir.glob(f"*/events.{slug}.jsonl"))
    paths = list(rotated)
    live = shard_dir / f"events.{slug}.jsonl"
    if live.exists():
        paths.append(live)
    return paths


def _all_machine_slugs(repo_root: Path) -> set[str]:
    """Every machine slug with a shard anywhere on disk — flat live shard or
    a rotated partition — so a peer that has been fully rotated out of its
    live shard (were that ever to happen) is still visited by
    ``fold_observed_set``.
    """
    shard_dir = repo_root / EVENTS_DIR_RELPATH
    prefix, suffix = "events.", ".jsonl"
    slugs: set[str] = set()
    for shard_file in shard_dir.glob(EVENTS_SHARD_GLOB):
        slugs.add(shard_file.name[len(prefix) : -len(suffix)])
    for shard_file in shard_dir.glob(f"*/{EVENTS_SHARD_GLOB}"):
        slugs.add(shard_file.name[len(prefix) : -len(suffix)])
    return slugs


def _event_month(line: str) -> str | None:
    """The ``YYYY-MM`` prefix of a shard line's ``observed_at`` field, or
    ``None`` if the line is malformed or the field is missing/not a string.

    ``rotate_month`` treats a line it cannot date as belonging to the live
    shard — never rotated — the same conservative default the rest of this
    module applies to anything it cannot parse confidently.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    observed_at = record.get("observed_at")
    if not isinstance(observed_at, str) or len(observed_at) < 7:
        return None
    return observed_at[:7]


def rotate_month(*, repo_root: Path, month: str, machine: str | None = None) -> int:
    """Relocate a CLOSED month's events out of the live flat shard into
    ``state/sovereign-tracker/<month>/events.<slug>.jsonl``.

    Standalone maintenance step per the C7 ruling (rotation-on-close, not
    partition-on-write — docs/plans/2026-08-18-sat-06-cockpit-consumption-
    seam.md § RULING 2026-08-20 "C7 is rotation-on-close, not partition-on-
    write"). Never called from the write path — ``append_event`` and
    ``append_events`` are untouched by this function's existence: sequence,
    the tail read, own-shard dedup, and ``locked_rmw`` all keep their
    current shape.

    *month* is a ``YYYY-MM`` string; only lines whose ``observed_at`` starts
    with that prefix are relocated (a line this function cannot date, per
    ``_event_month``, is left in the live shard, never moved).

    Returns the count of events relocated — 0 if none matched. This makes a
    repeated call for the same month idempotent: the first call already
    removed those lines from the live shard, so a second call has nothing
    left to move. Also idempotent against a prior run that reached the
    rotated file but not the live-shard trim (e.g. an interrupted run): a
    candidate line whose ``id`` already appears in the rotated file is
    skipped rather than appended a second time.

    Ordering: the rotated file is written BEFORE the live shard is
    trimmed, so a crash between the two steps leaves the data present in
    both places (safe — readers already merge across both layouts, see
    ``read_events``/``fold_observed_set``/``resolve_observed_set``) rather
    than in neither.

    Locks the live shard through the same ``locked_rmw`` target
    ``append_event`` uses, so rotation never races a concurrent append.
    """
    slug = machine if machine is not None else machine_slug()
    live = shard_path(repo_root, machine=slug)
    if not live.exists():
        return 0

    candidates = [
        line
        for line in _split_lines(live.read_text(encoding="utf-8"))
        if _event_month(line) == month
    ]
    if not candidates:
        return 0

    rotated_dir = repo_root / EVENTS_DIR_RELPATH / month
    rotated_dir.mkdir(parents=True, exist_ok=True)
    rotated_path = rotated_dir / f"events.{slug}.jsonl"

    existing_ids: set = set()
    existing_text = ""
    if rotated_path.exists():
        existing_text = rotated_path.read_text(encoding="utf-8")
        for line in _split_lines(existing_text):
            try:
                existing_record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(existing_record, dict) and existing_record.get("id"):
                existing_ids.add(existing_record["id"])

    new_lines: list[str] = []
    for line in candidates:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        record_id = record.get("id") if isinstance(record, dict) else None
        if record_id is not None and record_id in existing_ids:
            continue
        new_lines.append(line)

    if new_lines:
        if existing_text and not existing_text.endswith("\n"):
            existing_text += "\n"
        rotated_path.write_text(
            existing_text + "".join(line + "\n" for line in new_lines),
            encoding="utf-8",
        )

    def _mutate(old_text: str) -> str:
        remaining = [
            line for line in _split_lines(old_text) if _event_month(line) != month
        ]
        return "".join(line + "\n" for line in remaining)

    locked_rmw(live, _mutate, repo_root=repo_root, missing_ok=True)
    return len(candidates)


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


def _well_formedness(sequences: list[int]) -> tuple[int | None, int | None]:
    """cockpit's A12 well-formedness predicate over ONE shard's full
    ``sequence`` list, read in SHARD-FILE ORDER — never sorted.

    Returns ``(first_defect, resolves_unknown_from)``:
      - ``first_defect`` — the 1-based POSITION at which *sequences* first
        fails contiguous ``1..len(sequences)`` (the first index ``i``, 1-based,
        where ``sequences[i-1] != i``). ``None`` if the whole shard is
        well-formed.
      - ``resolves_unknown_from`` — the LOWEST claimed ``sequence`` for which
        resolution must answer ``unknown``: ``min(first_defect,
        sequences[first_defect - 1])``. Distinct from ``first_defect`` on a
        duplicate — a duplicate's file POSITION and its untrustworthy VALUE
        are different numbers (e.g. shard ``[1, 2, 3, 3]``: the defect is at
        file position 4, but the value duplicated is 3, so a claim of 3 is
        already unresolvable even though position 3 itself was fine). A
        claim strictly below ``resolves_unknown_from`` still resolves
        concretely (A12 item 3, the point-of-failure scope: a shard that
        goes bad late must not retroactively invalidate its own good
        prefix). ``None`` when the shard is well-formed.

    Deliberately NOT sorted: shard-file order is the input a git text-merge
    can reorder without reordering applies, and reordering here would hide
    exactly the defect this predicate exists to catch (cockpit's
    ``out-of-file-order`` vector — ``[1, 3, 2, 4]`` is a contiguous SET but a
    non-monotonic file order, and must resolve ``unknown`` from position 2).

    Conformance oracle: example-cockpit-repo
    ``docs/reference/tracker-well-formedness-vectors.json`` (normative spec:
    their ``docs/wiki/tracker-observed-set-and-concurrency.md`` § VALIDATOR
    CONTRACT (A12)) — every named vector in that file reproduces under this
    exact rule. C1b wires the live conformance harness against that fixture;
    this function is the implementation it checks.
    """
    for position, value in enumerate(sequences, start=1):
        if value != position:
            return position, min(position, value)
    return None, None


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

    # Visits every peer slug found across BOTH layouts — the flat live
    # shard and any rotated-out <YYYY-MM>/events.<slug>.jsonl partitions
    # (see docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md §
    # RULING 2026-08-20). A rotated peer's history is split across
    # multiple files; _peer_shard_paths orders them oldest-to-newest so
    # event_ids/current_sequence below accumulate in the same order the
    # peer originally appended them, and each file is still read EXACTLY
    # ONCE — sequence and prefix_digest are derived from these reads alone,
    # never a second, independent call to max_sequence() — see the
    # docstring's TOCTOU note above.
    observed_set: dict[str, dict[str, object]] = {}
    for slug in sorted(_all_machine_slugs(repo_root)):
        if slug == own_slug:
            continue

        event_ids: list[str] = []
        current_sequence = 0
        for shard_file in _peer_shard_paths(repo_root, slug):
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


def resolve_observed_set(marker: dict, *, repo_root: Path) -> dict:
    """Re-derive an ``observed_set_fold`` marker's justification against
    CURRENT bytes on every call — nothing is trusted because it was once
    written.

    Resolution granularity is PER COMPONENT, not per marker (cockpit's
    ratified § VALIDATOR CONTRACT (A12) item 5, § RESOLUTION GRANULARITY IS
    PER COMPONENT). The return is ALWAYS a ``dict`` — never the sentinel at
    top level — whose per-component VALUES carry the three-valued result:
      - a concrete ``{sequence, prefix_digest}`` value — this claimed
        component still validates against current bytes;
      - ``OBSERVED_SET_UNKNOWN`` — this claimed component no longer
        validates. Two INDEPENDENT checks, not one: the digest recompute
        detects that bytes CHANGED (tail substitution or mid-prefix
        substitution — the claimed prefix's ids no longer hash to the
        claimed ``prefix_digest``); ``_well_formedness`` detects a hole
        that was ALREADY THERE at fold time (a peer shard holding
        sequences 1, 2, 4 for a claim of 4 recomputes an identical digest
        over the same three ids, so the digest check alone cannot see the
        gap at 3). Also unknown if the peer shard's current tail is below
        the claimed ``sequence`` (the justifying bytes have left the
        repository).
    A caller gets e.g. ``{peer_A: {sequence, prefix_digest}, peer_B:
    OBSERVED_SET_UNKNOWN}`` and can still order events against ``peer_A``,
    degrading to incomparable only against ``peer_B`` — one damaged
    component no longer blinds the caller against every peer.

    The top-level return is ``{}`` only for the marker's own genuinely-empty
    ``observed_set`` — a positive assertion that nothing was observed (its
    own ``observed_set`` was empty at fold time). This is a real
    signature-semantics change for callers versus the prior whole-marker
    contract: ``resolve_observed_set_for_event`` still returns
    ``OBSERVED_SET_UNKNOWN`` at its own top level for the no-marker case
    (no assertion was ever made), so the two functions' top-level return
    shapes now differ on purpose — see that function's own docstring.

    The success path always returns a FRESH dict, never ``marker
    ["observed_set"]`` by reference — a caller mutating the result must
    never corrupt the in-memory marker record.

    A malformed ``marker["observed_set"]`` (missing or not a dict), a
    non-dict component, or a non-int claimed ``sequence`` is a contract
    violation, not an ``unknown`` — raises ``TrackerStoreError`` exactly as
    before; these raises are NOT accumulated per component.

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
        maximum anywhere in this function, for any component (DEC-6; their
        § VALIDATOR CONTRACT (A12) item 7). That collapse is the exact
        over-claim cockpit's property (c) exists to prevent.
      - Return early on the first failing component, collapsing the WHOLE
        marker to ``OBSERVED_SET_UNKNOWN``. Resolution is per component
        (A12 item 5) — one damaged peer must not blind the caller against
        every other peer's still-valid claim.
      - Return ``marker["observed_set"]`` (or any of its nested values) by
        reference on the all-concrete path. Build and return a fresh
        mapping every time.
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

    Well-formedness predicate adopted from cockpit's ratified § VALIDATOR
    CONTRACT (A12): a claimed peer prefix must be contiguous ``1..claimed_seq``
    with no duplicates and no gaps, or the component resolves ``unknown`` —
    see ``_well_formedness``. No longer provisional as of tmrg-03 C1a;
    the prior caveat here pended cockpit's answer, which now names this
    contract.

    Spec backlink: pln-sat-01b-observed-set-fold-actu-8b3f7a
    § Design, "Resolution, and the three-valued return"; § Tasks C2.
    docs/plans/2026-08-18-tmrg-03-ordering-contract-in-tracker-store.md
    § C1a, § C2a.
    """
    observed_set = marker.get("observed_set")
    if not isinstance(observed_set, dict):
        raise TrackerStoreError(
            "marker is missing a well-formed 'observed_set' mapping: "
            f"{marker.get('observed_set')!r}"
        )
    if not observed_set:
        return {}

    resolved: dict[str, object] = {}
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

        # Concatenates every shard file for this peer — any rotated
        # <YYYY-MM>/events.<slug>.jsonl partitions (ascending) then the
        # live flat shard — so a claim whose justifying bytes have been
        # rotated out of the live shard is still visible here (see
        # docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md §
        # RULING 2026-08-20). Each file is read EXACTLY ONCE via
        # _peer_shard_paths; current_max and event_ids below are BOTH
        # derived from these reads, not from a separate max_sequence()
        # call followed by a second read — see the docstring's TOCTOU
        # note above. `path` is kept only as a display name for error
        # messages below.
        path = shard_path(repo_root, machine=slug)
        text = "".join(
            shard_file.read_text(encoding="utf-8")
            for shard_file in _peer_shard_paths(repo_root, slug)
        )
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
            resolved[slug] = OBSERVED_SET_UNKNOWN
            continue

        event_ids: list[str] = []
        shard_sequences: list[int] = []
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
            if not isinstance(sequence, int):
                continue
            # shard_sequences covers the WHOLE shard, in file order,
            # regardless of claimed_seq — cockpit's well-formedness predicate
            # is a property of the shard, not of one claim (see
            # _well_formedness). event_ids stays filtered to
            # sequence <= claimed_seq — it is the digest recompute's input,
            # a separate, claim-scoped check.
            shard_sequences.append(sequence)
            if sequence <= claimed_seq:
                record_id = record.get("id")
                if not record_id:
                    raise TrackerStoreError(
                        f"malformed record in shard {path}: missing or "
                        "empty 'id'"
                    )
                event_ids.append(record_id)

        # cockpit's A12 well-formedness predicate, over the SAME
        # shard_sequences collected in the pass above — no second read.
        # Independent of the digest recompute below: a gap that was already
        # present when the marker was written (e.g. sequences 1, 2, 4 for a
        # claim of 4) recomputes an identical digest over the same three ids
        # and would pass the digest check alone. Point-of-failure scope (A12
        # item 3): a claim strictly below resolves_unknown_from still
        # resolves concretely — a shard that goes bad late must not
        # retroactively invalidate its own good prefix.
        _, resolves_unknown_from = _well_formedness(shard_sequences)
        if resolves_unknown_from is not None and claimed_seq >= resolves_unknown_from:
            resolved[slug] = OBSERVED_SET_UNKNOWN
            continue

        recomputed_digest = _prefix_digest(event_ids)
        if recomputed_digest != claimed_digest:
            resolved[slug] = OBSERVED_SET_UNKNOWN
            continue

        resolved[slug] = {"sequence": claimed_seq, "prefix_digest": claimed_digest}

    return resolved


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

    No longer PROVISIONAL — delegates to ``resolve_observed_set``, which
    adopted cockpit's ratified § VALIDATOR CONTRACT (A12) well-formedness
    predicate as of tmrg-03 C1a; see that function's docstring.

    Point-of-failure scope (their § VALIDATOR CONTRACT (A12) item 3) is a
    per-event property, and this is the function that actually exposes it:
    a peer shard that goes bad at position 40 must leave events whose
    marker claims a sequence below 40 resolving concretely, while events
    whose marker claims 40 or above resolve ``unknown`` for that
    component. This function needs no code of its own for that scoping —
    ``resolve_observed_set`` already applies it PER COMPONENT (a claim
    strictly below ``resolves_unknown_from`` resolves concrete; see
    ``_well_formedness``), and this function's only job is to pick the one
    marker in effect for *event* and delegate. Verified here — not just
    assumed — by tests that walk the actual event path (distinct events
    selecting distinct markers with distinct claims against one defective
    peer shard), because a test that only exercises ``resolve_observed_set``
    directly does not prove this function's own marker-selection step
    preserves it.

    Return-shape asymmetry with ``resolve_observed_set``, deliberate and
    NOT to be smoothed over: this function's own top-level return may
    still be the bare ``OBSERVED_SET_UNKNOWN`` sentinel — the no-marker
    case above, where no assertion was ever made for *event*.
    ``resolve_observed_set`` itself never returns the bare sentinel at top
    level as of C2a; it always returns a ``dict`` (possibly ``{}`` for a
    genuinely-empty ``observed_set``, possibly with per-component
    ``OBSERVED_SET_UNKNOWN`` values). Once this function finds a marker and
    delegates, its return takes on ``resolve_observed_set``'s dict shape;
    only the no-marker branch above stays the bare sentinel.

    Negative-spec: marker selection breaks ties on ``(sequence, id)``, never
    on line position. ``sequence`` is unique only under one working tree's
    lock — a git text-merge of a shard can leave two markers at the same
    ``sequence``, and it can reorder lines without reordering applies, so
    first-in-file-order is an artifact of how the shard was written to disk
    rather than a fact about event order.

    Spec backlink: pln-sat-01b-observed-set-fold-actu-8b3f7a
    § Design, "The event→marker mapping"; § Tasks C2;
    ``cross-repo/inbox/2026-08-18-example-cockpit-repo-em-tmrg-03-ordering-contract-and-prefix-closure-ownership.md`` § (e).
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
    latest_marker_key: tuple[int, str] | None = None
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
        record_id = record.get("id")
        record_key = (record_sequence, record_id if isinstance(record_id, str) else "")
        if latest_marker_key is None or record_key > latest_marker_key:
            latest_marker_key = record_key
            latest_marker = record

    if latest_marker is None:
        return OBSERVED_SET_UNKNOWN

    return resolve_observed_set(latest_marker, repo_root=repo_root)


# ---------------------------------------------------------------------------
# Causal comparator (tmrg-03 C3) — a linear extension of the causal partial
# order over observed-set vectors, kept a wholly separate entrypoint from
# ``read_events`` per DEC-4 (AC8: ``read_events``' signature and default
# ordering behaviour do not change).
#
# Normative spec: example-cockpit-repo
# ``docs/wiki/tracker-observed-set-and-concurrency.md`` § MIXED-TYPE
# DOMINATION COMPARE (A14) and § CONSUMPTION CONSTRAINT — that wiki wins on
# any conflict with prose elsewhere, including
# docs/plans/2026-08-18-tmrg-03-ordering-contract-in-tracker-store.md's own
# C3 task text. Cockpit owns the comparator as a SPECIFICATION end to end
# (their § OWNERSHIP); this is a conformant second implementation of their
# one spec, not a second contract.
# ---------------------------------------------------------------------------

CAUSAL_ORDER_EQUAL = "equal"
"""Every component of both vectors compares equal — same causal position."""

CAUSAL_ORDER_A_DOMINATES = "a_dominates"
"""Vector A dominates vector B (A14): every component of B is ``<=`` its A
counterpart, with at least one strictly ``<``. A is causally LATER than B —
A has observed everything B has, plus more."""

CAUSAL_ORDER_B_DOMINATES = "b_dominates"
"""The mirror of ``CAUSAL_ORDER_A_DOMINATES`` — B dominates A, B is causally
later."""

CAUSAL_ORDER_CONCURRENT = "concurrent"
"""A genuine antichain: some component names A as later, another names B as
later, and no component is ``unknown``. Neither vector dominates — this is a
real, decided concurrent-modification finding, not a missing-information
one, and must never be reported the same way as
``CAUSAL_ORDER_INDETERMINATE``."""

CAUSAL_ORDER_INDETERMINATE = "indeterminate"
"""At least one component's comparison could not be decided (A13): a
resolved-to-``OBSERVED_SET_UNKNOWN`` component, an equal-``sequence``
digest-divergence, or a whole vector that is the bare ``OBSERVED_SET_UNKNOWN``
sentinel (no assertion was ever made about that side at all). Per A13, an
``unknown`` component always propagates to the whole-axis answer — it must
never be silently outvoted by a decided component elsewhere, and it must
never collapse into ``CAUSAL_ORDER_CONCURRENT`` (a decided finding) or into
either dominates value (a trusted maximum)."""

_ABSENT = object()
"""Internal sentinel distinguishing "this vector has no entry for this peer"
(A14's ``absent`` row) from a real component value — never returned or
compared with ``==`` outside ``_compare_observed_set_component``."""


def _compare_observed_set_component(component_a: object, component_b: object) -> str:
    """One key's A14 compare. Returns ``"lt"``, ``"gt"``, ``"eq"``, or
    ``"unknown"`` — never a five-valued axis result; that aggregation is
    ``compare_observed_set_vectors``'s job, over every key in the union.

    Implements the table verbatim:

    | Case | Result |
    |---|---|
    | Key absent on one side | ``absent == {sequence: 0}``, compares below any present component; no digest test |
    | Either side ``unknown`` | ``"unknown"``; never compared |
    | Both present, ``sequence`` differs | order on ``sequence`` alone — digests carry NO signal here |
    | Both present, ``sequence`` equal, digest equal | ``"eq"`` |
    | Both present, ``sequence`` equal, digest differs | ``"unknown"`` — histories diverged |

    *component_a* / *component_b* are each one of: ``_ABSENT`` (key missing
    from that side's vector), ``OBSERVED_SET_UNKNOWN`` (that component
    itself resolved unknown), or a concrete ``{"sequence": int,
    "prefix_digest": str}`` mapping.

    ``prefix_digest`` is NOT an ordering key — a digest is not orderable.
    Its entire participation is the equal-``sequence`` divergence test in
    the last two table rows; a differing ``sequence`` decides the order on
    its own, digests unconsulted (a longer prefix is EXPECTED to hash
    differently, so comparing digests there would be a false unknown).

    Degenerate case not named by the table: both sides absent (never
    reached — this is only called for keys in the union of both vectors'
    key sets, so at least one side is always present) and a present
    component naming ``sequence: 0`` compared against an absent side (both
    normalise to ``0`` with no digest to test either way) — treated as
    ``"eq"``. Not exercised in practice: ``fold_observed_set`` never emits a
    peer component for a peer shard with zero events.
    """
    if component_a is OBSERVED_SET_UNKNOWN or component_b is OBSERVED_SET_UNKNOWN:
        return "unknown"

    if component_a is _ABSENT:
        seq_a: int = 0
    else:
        seq_a = component_a["sequence"]  # type: ignore[index]

    if component_b is _ABSENT:
        seq_b: int = 0
    else:
        seq_b = component_b["sequence"]  # type: ignore[index]

    if seq_a != seq_b:
        return "lt" if seq_a < seq_b else "gt"

    if component_a is _ABSENT or component_b is _ABSENT:
        return "eq"

    digest_a = component_a["prefix_digest"]  # type: ignore[index]
    digest_b = component_b["prefix_digest"]  # type: ignore[index]
    return "eq" if digest_a == digest_b else "unknown"


def _resolve_self_component(
    machine: str, sequence: int, *, repo_root: Path
) -> dict | _UnknownObservedSet:
    """cockpit's A14 self-normalisation: recompute *machine*'s own
    ``{sequence, prefix_digest}`` component fresh from its OWN shard, at
    RESOLVE time — never from a value stamped at append time (§
    MIXED-TYPE DOMINATION COMPARE (A14), "Resolve time, never append
    time"). Stamping this at append would give ``append_event`` a reason
    to derive a digest at write time, breaking AC1c/AC7's append-path
    no-peer-shard-read guarantee; independently, a digest frozen at
    append hashes bytes a later git merge rewrites, so it could never
    detect the rewrite it exists to detect.

    Reads *machine*'s own shard via its own ``read_text()`` call — a
    SECOND, independent read of that shard within one
    ``resolve_observed_set_union_for_event`` invocation, not a reuse of
    any read already performed. *machine*/*sequence* come from the
    caller-supplied *event* dict itself, never from a shard read; the
    caller's own shard read happens separately, inside
    ``resolve_observed_set_for_event`` (which reads *machine*'s shard to
    scan for the marker in effect). This function adds no PEER-shard
    access over that call, so ``resolve_observed_set_for_event``'s
    byte-derivability property still holds unchanged — it just does so
    via two own-shard reads, not one.

    The two reads are not atomic with each other, so this is the same
    double-read *shape* ``resolve_observed_set`` and ``fold_observed_set``
    warn against for peer shards — but narrower in exposure, not just in
    frequency: a peer shard can be rewritten wholesale by a git merge
    between two reads, which is what those TOCTOU warnings guard against.
    *machine*'s own shard cannot — under this module's own append
    discipline (``append_event``/``append_events``, both single-writer via
    ``locked_rmw`` on this exact machine), it is append-only and
    monotonic: a write landing between the marker scan's read and this
    read can only add new lines at strictly higher ``sequence`` values, it
    can never mutate or reorder a line either read already saw. Every line
    at or below *sequence* — the whole input to this function's
    well-formedness check and digest — is therefore already immutable by
    the time of either read. The two reads can disagree only about lines
    ABOVE *sequence*, which this function does not consult. Accepted as
    benign on that basis, not asserted as a single shared read.

    Applies cockpit's DEC-5 / § SELF-SHARD APPLICABILITY: the
    well-formedness precondition covers the reader's OWN shard too, not
    peers only. An event at or after its own shard's first defect
    (``_well_formedness``) reports ``OBSERVED_SET_UNKNOWN`` for its
    self-component, exactly as a peer's defect would for that peer's
    component — reusing ``_well_formedness`` rather than a second
    predicate.

    Spec backlink: docs/plans/2026-08-18-tmrg-03-ordering-contract-in-tracker-store.md
    § C4; example-cockpit-repo docs/wiki/tracker-observed-set-and-concurrency.md
    § MIXED-TYPE DOMINATION COMPARE (A14), § SELF-SHARD APPLICABILITY (DEC-5).
    """
    path = shard_path(repo_root, machine=machine)
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    event_ids: list[str] = []
    shard_sequences: list[int] = []
    for line in _split_lines(text):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrackerStoreError(f"malformed line in shard {path}: {exc}") from exc
        if not isinstance(record, dict):
            raise TrackerStoreError(f"malformed line in shard {path}: not a JSON object")
        record_sequence = record.get("sequence")
        if not isinstance(record_sequence, int):
            continue
        shard_sequences.append(record_sequence)
        if record_sequence <= sequence:
            record_id = record.get("id")
            if not record_id:
                raise TrackerStoreError(
                    f"malformed record in shard {path}: missing or empty 'id'"
                )
            event_ids.append(record_id)

    _, resolves_unknown_from = _well_formedness(shard_sequences)
    if resolves_unknown_from is not None and sequence >= resolves_unknown_from:
        return OBSERVED_SET_UNKNOWN

    return {"sequence": sequence, "prefix_digest": _prefix_digest(event_ids)}


def resolve_observed_set_union_for_event(
    event: dict, *, repo_root: Path
) -> dict | _UnknownObservedSet:
    """``W(E)`` — cockpit's § SELF-COMPONENT RECONCILIATION (A10): the union
    of *event*'s peer-only vector with its OWN self-component.

    ```
    W(E) = resolve_observed_set_for_event(E)      # peers, from the marker in effect
         ∪ { machine(E): (sequence(E), id(E)) }    # self, from E's OWN record
    ```

    normalised to ``{sequence, prefix_digest}`` per § MIXED-TYPE DOMINATION
    COMPARE (A14) — the digest computed HERE, at resolve time, over
    *machine*'s own shard prefix ``1..sequence(E)`` via
    ``_resolve_self_component`` — so ``compare_observed_set_vectors`` sees
    exactly one homogeneous component type across the whole union.

    The self-component comes from *event*'s OWN ``machine``/``sequence``
    fields — NEVER from the marker's placement that
    ``resolve_observed_set_for_event`` walks to find peers. That is the
    whole point of this function:
    ``resolve_observed_set_for_event`` maps every event between two fold
    markers to the SAME marker (§ SELF-COMPONENT RECONCILIATION, "the
    failure mode of getting this wrong"), so two same-machine applied
    events sharing one fold window get an identical peer payload — correct
    and expected. If the self-component were ALSO taken from the marker's
    own placement, both events' FULL vectors would be identical: neither
    dominates, and the axis would be falsely reported
    ``CAUSAL_ORDER_CONCURRENT``. Reading the self-component from each
    event's own record instead gives the later event a strictly greater
    own-``sequence`` than the earlier one, so it correctly dominates —
    no conflict.

    If no marker was ever in effect for *event* (the bare
    ``OBSERVED_SET_UNKNOWN`` sentinel from ``resolve_observed_set_for_event``
    — no assertion was ever made about peers), the whole union is
    ``OBSERVED_SET_UNKNOWN`` too: there is no peer payload to union a
    self-component onto.

    No peer-shard access is added by this function over
    ``resolve_observed_set_for_event`` alone — see
    ``_resolve_self_component``'s own docstring.

    ``compare_observed_set_vectors`` (C3) is UNCHANGED by this function —
    it only changes what gets fed into that compare, per that function's
    own "Self-component note".

    Spec backlink: docs/plans/2026-08-18-tmrg-03-ordering-contract-in-tracker-store.md
    § C4; example-cockpit-repo docs/wiki/tracker-observed-set-and-concurrency.md
    § SELF-COMPONENT RECONCILIATION (A10), § MIXED-TYPE DOMINATION COMPARE
    (A14).
    """
    machine = event.get("machine")
    sequence = event.get("sequence")
    if not isinstance(machine, str) or not machine:
        raise TrackerStoreError(f"event is missing required field: machine ({event!r})")
    if not isinstance(sequence, int):
        raise TrackerStoreError(f"event is missing required field: sequence ({event!r})")

    peer_vector = resolve_observed_set_for_event(event, repo_root=repo_root)
    if peer_vector is OBSERVED_SET_UNKNOWN:
        return OBSERVED_SET_UNKNOWN

    union = dict(peer_vector)
    union[machine] = _resolve_self_component(machine, sequence, repo_root=repo_root)
    return union


def compare_observed_set_vectors(vector_a, vector_b) -> str:
    """The causal comparator (cockpit's § MIXED-TYPE DOMINATION COMPARE,
    A14) over two observed-set vectors. Pure and read-free — takes whatever
    ``resolve_observed_set_for_event`` (or a future self-augmented union of
    it — see the module note below) already resolved; does no shard I/O of
    its own.

    *vector_a* / *vector_b* are each either the bare ``OBSERVED_SET_UNKNOWN``
    sentinel (no marker was ever in effect for that side — no assertion was
    ever made) or a ``dict`` mapping peer slug to either
    ``OBSERVED_SET_UNKNOWN`` (that one component resolved unknown) or a
    concrete ``{"sequence": int, "prefix_digest": str}`` mapping — exactly
    ``resolve_observed_set_for_event``'s return shape.

    Domination (A14, quoted): ``W(A)`` dominates ``W(B)`` iff every
    component of ``B`` is ``<=`` its counterpart in ``A``, with at least one
    strictly ``<``. Any ``unknown`` component leaves the pair undecided and
    the axis ``indeterminate`` (A13) — checked FIRST, before any directional
    tally, so an unknown component can never be silently outvoted by a
    decided component elsewhere in the same vector.

    Returns one of ``CAUSAL_ORDER_EQUAL``, ``CAUSAL_ORDER_A_DOMINATES``,
    ``CAUSAL_ORDER_B_DOMINATES``, ``CAUSAL_ORDER_CONCURRENT`` (a genuine,
    decided antichain — no unknowns, but neither side dominates), or
    ``CAUSAL_ORDER_INDETERMINATE`` (an unknown component, or an entire side
    that is the bare sentinel, decided nothing).

    Comparison is over the UNION of both vectors' peer key sets — a peer
    named by only one side compares via the ``absent`` row, never raises,
    never silently drops that peer from the compare.

    Negative-spec: ``CAUSAL_ORDER_INDETERMINATE`` must never collapse into
    ``CAUSAL_ORDER_EQUAL`` (a false "nothing changed"), into
    ``CAUSAL_ORDER_CONCURRENT`` (a false positive — that value is reserved
    for a DECIDED antichain), or into either dominates value (the exact
    trusted-maximum over-claim cockpit's contract exists to prevent).

    Self-component note (tmrg-03 scope split): this function is intentionally
    agnostic to what a vector's keys and components MEAN — it compares
    whatever mapping it is given. C4 adds the writing machine's own
    self-component to ``W(E)`` at the point the vector is CONSTRUCTED
    (``resolve_observed_set_union_for_event``, unioning
    ``resolve_observed_set_for_event``'s peer-only result with a self entry
    normalised to this same ``{sequence, prefix_digest}`` shape, digest
    recomputed at resolve time over the event's own shard prefix per A14's
    "normalise, then compare" rule) — never here. This function did not
    change to accommodate that; it was, and remains, the seam C4 builds on
    top of, not one it rewrites.

    Spec backlink: docs/plans/2026-08-18-tmrg-03-ordering-contract-in-tracker-store.md
    § C3; example-cockpit-repo docs/wiki/tracker-observed-set-and-concurrency.md
    § MIXED-TYPE DOMINATION COMPARE (A14).
    """
    if vector_a is OBSERVED_SET_UNKNOWN or vector_b is OBSERVED_SET_UNKNOWN:
        return CAUSAL_ORDER_INDETERMINATE

    keys = set(vector_a) | set(vector_b)

    saw_unknown = False
    saw_lt = False
    saw_gt = False
    for key in keys:
        component_a = vector_a.get(key, _ABSENT)
        component_b = vector_b.get(key, _ABSENT)
        result = _compare_observed_set_component(component_a, component_b)
        if result == "unknown":
            saw_unknown = True
        elif result == "lt":
            saw_lt = True
        elif result == "gt":
            saw_gt = True

    if saw_unknown:
        return CAUSAL_ORDER_INDETERMINATE
    if saw_lt and saw_gt:
        return CAUSAL_ORDER_CONCURRENT
    if saw_lt:
        # Every component of A is <= its B counterpart, at least one <.
        return CAUSAL_ORDER_B_DOMINATES
    if saw_gt:
        return CAUSAL_ORDER_A_DOMINATES
    return CAUSAL_ORDER_EQUAL


def compare_events_causal_order(event_a: dict, event_b: dict, *, repo_root: Path) -> int:
    """The linear-extension entrypoint: a total order over applied events,
    causal-vector-first, ``(machine, id)``-tiebreak second — kept
    deliberately separate from ``read_events`` (DEC-4, AC8:
    ``read_events``' signature and default ordering behaviour do not
    change).

    Resolves each event's UNION observed-set vector ``W(E)`` via
    ``resolve_observed_set_union_for_event`` (C4 — peers from the marker in
    effect, plus the writing machine's own self-component from *event*'s
    own record) and compares them with ``compare_observed_set_vectors``.
    When that compare decides a direction (one side dominates), the
    dominating event is causally LATER and sorts after the other. For
    every pair the vector compare proves incomparable —
    ``CAUSAL_ORDER_EQUAL``, ``CAUSAL_ORDER_CONCURRENT``, or
    ``CAUSAL_ORDER_INDETERMINATE`` alike — order falls through to the
    stable ``(machine, id)`` key (``machine`` names the shard, ``id`` is
    cockpit's globally-unique primary key), never to ``applied_at``:
    ``applied_at`` is wall-clock-stamped per machine and is not trusted to
    order two applied events across machines once causal information has
    already spoken for a pair — and it does not participate here even when
    the vector compare has nothing to say, because it never speaks FOR a
    pair to begin with, only alongside one.

    Returns ``-1`` if *event_a* sorts before *event_b*, ``1`` if after, and
    ``0`` only when both events carry the same ``(machine, id)`` — i.e. the
    same event compared against itself. Suitable as a
    ``functools.cmp_to_key`` comparator.

    C3's known incompleteness is now closed: two applied events from the
    SAME machine sharing one fold marker (cockpit's § SELF-COMPONENT
    RECONCILIATION worked example) got identical PEER vectors under C3 and
    fell straight through to the ``(machine, id)`` tiebreak — stable but
    not causally informed for that pair. Under
    ``resolve_observed_set_union_for_event`` the two events' self-components
    differ (the later event has the strictly greater own-``sequence``), so
    ``compare_observed_set_vectors`` now decides that pair causally instead
    of falling through.

    CONSUMPTION CONSTRAINT (cockpit's wiki, same section name — a hard
    constraint on every value this comparator touches, not a footnote): a
    vector ``sequence`` component indexes SHARD position, including marker
    slots that ``read_events`` filters out (``applied_at is None``). This
    function never compares a vector ``sequence`` component against a
    projection-derived count (a ``read_events``-output index, a ``len()``,
    an enumerate position) — the fallback key deliberately uses ``machine``
    and ``id`` only, never ``sequence``, so this function cannot even be
    tempted into that mistake. A future editor extending the fallback key
    must not add ``sequence`` to it for exactly this reason.

    Spec backlink: docs/plans/2026-08-18-tmrg-03-ordering-contract-in-tracker-store.md
    § C3; example-cockpit-repo docs/wiki/tracker-observed-set-and-concurrency.md
    § 4.3's linear extension, § CONSUMPTION CONSTRAINT.
    """
    vector_a = resolve_observed_set_union_for_event(event_a, repo_root=repo_root)
    vector_b = resolve_observed_set_union_for_event(event_b, repo_root=repo_root)
    outcome = compare_observed_set_vectors(vector_a, vector_b)

    if outcome == CAUSAL_ORDER_A_DOMINATES:
        return 1
    if outcome == CAUSAL_ORDER_B_DOMINATES:
        return -1

    key_a = _fallback_tiebreak_key(event_a)
    key_b = _fallback_tiebreak_key(event_b)
    if key_a == key_b:
        return 0
    return -1 if key_a < key_b else 1


def _fallback_tiebreak_key(event: dict) -> tuple[str, str]:
    """The ``(machine, id)`` fallback key for ``compare_events_causal_order``,
    validated the same way ``resolve_observed_set_for_event`` validates those
    two fields on its own ``event`` argument — a contract violation surfaces
    as a named ``TrackerStoreError``, never as an incidental ``TypeError``
    from comparing a tuple with a ``None`` in it, and never as a silently
    fabricated default identity (``""``, ``0``) that would order two events
    on made-up grounds.

    Both fields are required by every caller in this module today
    (``resolve_observed_set_union_for_event`` and its own callees already
    demand ``machine``; cockpit's applied-event shape guarantees ``id``) —
    this only makes that precondition explicit at the one call site that
    was reading the fields without checking them.
    """
    machine = event.get("machine")
    event_id = event.get("id")
    if not isinstance(machine, str) or not machine:
        raise TrackerStoreError(f"event is missing required field: machine ({event!r})")
    if not isinstance(event_id, str) or not event_id:
        raise TrackerStoreError(f"event is missing required field: id ({event!r})")
    return (machine, event_id)
