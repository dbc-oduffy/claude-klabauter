"""Multi-ask submission contract: types, paths and pure checks shared by the fan-out op, the submit hook and run close.

Leaf module, stdlib only. ``ledger_partition`` answers whether every parent row is claimed by exactly
one slice; ``after_edges`` which slice pairs must be sequenced; ``blocked_by_edges`` the same from
stub ``blocked_by`` (plus holds for out-of-batch blockers); ``release`` which waiting runs a close
frees on their ``after`` predecessors alone (never one in ``skip``). ``CLOSED_BY_KEY`` is the halted status-file key naming the
evidence that closed the run; ``superseded_rel`` / ``read_superseded_by`` are a held slice's
write-once supersession marker. ``AskSlice.source`` is a slice's own ask source (None reads as ``pm``);
``SLICE_SOURCE_KEY`` is the ``em_analysis`` key a relayed slice sizing carries; ``slice_ask_source``
reads it back. ``NOT_FIRED_REASONS`` / ``PLAN_BLITZ_UNFIRED_BUCKETS`` / ``not_fired_entry`` /
``not_fired_refusal`` are the roadmap fan-out's unfired-plan report vocabulary.
``ASKS_ITEM_KEYS`` / ``unknown_keys_refusal`` are the accepted ``asks[]`` entry keys (``stop_after``
among them, value ``plan`` only) and the sentence refusing a key outside an accepted set.
Reference: docs/reference/multi-ask-submission-contract.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from coordinator_core.session.record_homes import home_dir

FANOUT_OP = "dispatch.ask_fanout"
PARENT_SCHEMA = "ask-parent"
PARENT_VERSION = 1

STATUS_READY = "ready"
STATUS_AFTER = "after"
STATUS_QUEUED = "queued"  # legacy status: read, never written
STATUS_RUNNING = "running"
STATUS_LANDED = "landed"
STATUS_HALTED = "halted"
STATUS_HELD = "held"
RUN_STATUSES = (
    STATUS_READY,
    STATUS_AFTER,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_LANDED,
    STATUS_HALTED,
    STATUS_HELD,
)

EDGES_FROM_PARAM = "edges_from"
EDGES_FROM_BLOCKED_BY = "blocked_by"
ITEM_IDS_KEY = "ids"
ITEM_BLOCKED_BY_KEY = "blocked_by"
HELD_KEY = "held"

# Submission params in, reply dict out; the hook injects it so it never imports the op module.
FanoutFn = Callable[[dict, Any], dict]


@dataclass(frozen=True)
class AskSlice:
    """One separable ask. ``writes`` None means no hint was given (overlaps nothing)."""

    label: str
    ask: str
    rows: tuple[str, ...] = ()
    writes: Optional[tuple[str, ...]] = None
    wave: Optional[int] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class SiblingRun:
    """One run of a fan-out: its reserved id, predecessors, slice sizing path and status."""

    run_id: str
    label: str
    after: tuple[str, ...] = ()
    slice_sizing: str = ""
    status: str = STATUS_READY
    hold_reason: str = ""


@dataclass(frozen=True)
class FanoutReply:
    """The frozen reply: exactly ``{parent_id, runs:[{run_id, label, after?}]}``."""

    parent_id: str
    runs: tuple[SiblingRun, ...]

    def to_json(self) -> dict:
        runs = []
        for run in self.runs:
            entry: dict = {"run_id": run.run_id, "label": run.label}
            if run.after:
                entry["after"] = list(run.after)
            runs.append(entry)
        return {"parent_id": self.parent_id, "runs": runs}


@dataclass(frozen=True)
class ParentRecord:
    """Write-once parent record; ``runs`` is in submission order."""

    parent_id: str
    runs: tuple[SiblingRun, ...]
    confirm_split: bool = False

    def to_json(self) -> dict:
        return {
            "schema": PARENT_SCHEMA,
            "version": PARENT_VERSION,
            "parent_id": self.parent_id,
            "confirm_split": self.confirm_split,
            "runs": [
                {
                    "run_id": r.run_id,
                    "label": r.label,
                    "after": list(r.after),
                    "slice_sizing": r.slice_sizing,
                    "status": r.status,
                    **({"hold_reason": r.hold_reason} if r.hold_reason else {}),
                }
                for r in self.runs
            ],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ParentRecord":
        if data.get("schema") != PARENT_SCHEMA or data.get("version") != PARENT_VERSION:
            raise ValueError(f"not an {PARENT_SCHEMA} v{PARENT_VERSION} record")
        runs = tuple(
            SiblingRun(
                run_id=str(r["run_id"]),
                label=str(r.get("label", "")),
                after=tuple(r.get("after", ())),
                slice_sizing=str(r.get("slice_sizing", "")),
                status=str(r.get("status", STATUS_READY)),
                hold_reason=str(r.get("hold_reason", "")),
            )
            for r in data.get("runs", ())
        )
        return cls(
            parent_id=str(data["parent_id"]),
            runs=runs,
            confirm_split=bool(data.get("confirm_split", False)),
        )


def parent_record_rel(parent_id: str) -> str:
    """Runtime-only write-once parent record."""
    return f"scratch/warp/{parent_id}/parent.json"


def run_parent_copy_rel(run_id: str) -> str:
    """Tracked per-run copy of the parent record, landed by that run's own commit."""
    return f"{PurePosixPath(home_dir('', 'warp').replace(os.sep, '/'))}/{run_id}/parent.json"


def run_base_rel(run_id: str) -> str:
    """Write-once file holding a reserved run's base sha, beside its status file."""
    return f"scratch/warp/{run_id}/run-base"


def recorded_run_base(root: Any, run_id: str) -> Optional[str]:
    """The sha a reserved run's first bind recorded as its base, None when no bind has.

    Lives here, not in ``dispatch_emit.op``: ``terminal_commit`` reads it, and importing ``op`` from
    there pulls ``sizing_assemble`` (hence the fleet record) into every ``review_stamp`` importer.
    """
    try:
        return (root / run_base_rel(run_id)).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def status_rel(run_id: str) -> str:
    """Runtime-only status file; only the run itself rewrites it."""
    return f"scratch/warp/{run_id}/sibling-status.json"


CLOSED_BY_KEY = "closed_by"
SUPERSEDED_BY_KEY = "superseded_by"


def superseded_rel(run_id: str) -> str:
    """Runtime-only write-once ``{superseded_by, at}`` marker of a slice a later run replaced."""
    return f"scratch/warp/{run_id}/superseded.json"


def read_superseded_by(root: Any, run_id: str) -> Optional[str]:
    """The run id *run_id*'s supersession marker names, None when absent or unreadable."""
    import json

    try:
        data = json.loads((root / superseded_rel(run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get(SUPERSEDED_BY_KEY) if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def ask_parent_marker(parent_id: str, run_id: str) -> str:
    """The ``em_analysis.ask_parent`` string: ``"<parent_id> <run_id>"``."""
    return f"{parent_id} {run_id}"


def parse_ask_parent(text: Any) -> Optional[tuple[str, str]]:
    """Inverse of ``ask_parent_marker``; None for anything that is not exactly two tokens."""
    if not isinstance(text, str):
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


SLICE_SOURCE_KEY = "ask_source"


def slice_ask_source(sizing: Mapping) -> Optional[str]:
    """The slice sizing's ``em_analysis.ask_source`` string, else None (absent reads as ``pm``)."""
    analysis = sizing.get("em_analysis") if isinstance(sizing, Mapping) else None
    if not isinstance(analysis, Mapping):
        return None
    value = analysis.get(SLICE_SOURCE_KEY)
    return value if isinstance(value, str) else None


INTERACTION_MODE_PARAM = "interaction_mode"
PARENT_SIZING_KEY = "parent_sizing"
NOT_FIRED_KEY = "not_fired"

NOT_FIRED_REASONS = (
    "pulled",
    "replan",
    "errored",
    "surfaced-to-pm",
    "adjudicated",
    "xs-run",
    "routed-elsewhere",
    "absent-from-wave-reply",
    "wave-not-run",
    "after",
    "queued",
    "fanout-refused",
    "commit-not-landed",
    "nothing-to-commit",
    "held",
)

# Trap: a bucket DoE adds to the plan-blitz WAVE RESULT later is read by no line until it is listed
# here, and its plans go unreported.
PLAN_BLITZ_UNFIRED_BUCKETS = (
    ("pulled", "pulled"),
    ("replan", "replan"),
    ("errored", "errored"),
    ("surfacedToPm", "surfaced-to-pm"),
    ("adjudicated", "adjudicated"),
    ("xsRuns", "xs-run"),
    ("routedElsewhere", "routed-elsewhere"),
)


def not_fired_entry(
    *,
    reason: str,
    plan: Optional[str] = None,
    baton: Optional[str] = None,
    wave: Optional[int] = None,
    detail: Optional[str] = None,
) -> dict:
    """A ``{plan, reason, wave?, baton?, detail?}`` item; keys whose value is None are left out."""
    if reason not in NOT_FIRED_REASONS:
        raise ValueError(f"not-fired reason {reason!r} is not in NOT_FIRED_REASONS")
    candidates = (("plan", plan), ("reason", reason), ("wave", wave), ("baton", baton), ("detail", detail))
    return {k: v for k, v in candidates if v is not None}


def not_fired_refusal(value: Any) -> Optional[str]:
    """None for a valid not-fired list, else a message naming the first bad index."""
    if not isinstance(value, (list, tuple)):
        return f"{NOT_FIRED_KEY} must be a list, got {type(value).__name__}"
    for i, item in enumerate(value):
        if not isinstance(item, Mapping):
            return f"{NOT_FIRED_KEY}[{i}] must be an object"
        if item.get("reason") not in NOT_FIRED_REASONS:
            return f"{NOT_FIRED_KEY}[{i}] reason {item.get('reason')!r} is not in NOT_FIRED_REASONS"
        if not any(isinstance(item.get(k), str) and item.get(k) for k in ("plan", "baton")):
            return f"{NOT_FIRED_KEY}[{i}] needs a non-empty str plan or baton"
    return None


ASKS_ITEM_KEYS: tuple[str, ...] = (
    "ask",
    "source",
    "sender",
    "relayed_text",
    "route_preference",
    "em_rationale",
    "em_context",
    "context",
    "label",
    "writes",
    "stop_after",
)


def unknown_keys_refusal(where: str, supplied: Iterable[str], accepted: Sequence[str]) -> Optional[str]:
    """None when every supplied key is accepted, else one sentence naming the first unknown key
    (supplied order) and the accepted keys (declared order)."""
    for key in supplied:
        if key not in accepted:
            return f"{where}: unknown key {key!r}. Accepted: {', '.join(accepted)}"
    return None


def ledger_partition(parent_row_ids: Sequence[str], slices: Sequence[AskSlice]) -> Optional[str]:
    """Refusal text naming every unclaimed and every doubly-claimed parent row, else None.

    No row is exempt. A row a slice claims that the parent does not hold counts as unknown and is named.
    """
    parent = list(dict.fromkeys(parent_row_ids))
    known = set(parent)
    claims: dict[str, int] = {}
    unknown: list[str] = []
    for sl in slices:
        for row in dict.fromkeys(sl.rows):
            claims[row] = claims.get(row, 0) + 1
            if row not in known and row not in unknown:
                unknown.append(row)
    unclaimed = [r for r in parent if claims.get(r, 0) == 0]
    doubled = [r for r in parent if claims.get(r, 0) > 1]
    if not (unclaimed or doubled or unknown):
        return None
    parts = []
    if unclaimed:
        parts.append("unclaimed rows: " + ", ".join(unclaimed))
    if doubled:
        parts.append("doubly-claimed rows: " + ", ".join(doubled))
    if unknown:
        parts.append("rows not in the parent register: " + ", ".join(unknown))
    return "Partition refused; " + "; ".join(parts) + ". Every parent row belongs to exactly one slice."


def _norm(path: str) -> PurePosixPath:
    return PurePosixPath(path.replace("\\", "/").strip("/"))


def _writes_overlap(a: Optional[Sequence[str]], b: Optional[Sequence[str]]) -> bool:
    if a is None or b is None:
        return True
    for x in a:
        px = _norm(x)
        for y in b:
            py = _norm(y)
            if px == py or px in py.parents or py in px.parents:
                return True
    return False


def _earlier_wave(earlier: AskSlice, later: AskSlice) -> bool:
    return earlier.wave is not None and later.wave is not None and earlier.wave < later.wave


def after_edges(slices: Sequence[AskSlice]) -> dict[int, tuple[int, ...]]:
    """Map slice index to the earlier indices it must follow.

    j follows earlier i when both declare writes and those overlap (exact path, or one a directory
    prefix of the other), or when both carry a ``wave`` and i's is lower. A slice with no ``writes``
    hint overlaps nothing: undeclared slices run in parallel and the shared-tree claim mechanism
    arbitrates their overlap. An empty ``writes`` list likewise overlaps nothing.
    """
    edges: dict[int, tuple[int, ...]] = {}
    for j, later in enumerate(slices):
        edges[j] = tuple(
            i
            for i in range(j)
            if (
                slices[i].writes is not None
                and later.writes is not None
                and _writes_overlap(slices[i].writes, later.writes)
            )
            or _earlier_wave(slices[i], later)
        )
    return edges


def blocked_by_edges(
    ids: Sequence[Sequence[str]],
    blocked_by: Sequence[Sequence[str]],
    writes: Sequence[Optional[Sequence[str]]],
) -> tuple[dict[int, tuple[int, ...]], dict[int, str]]:
    """``(edges, holds)`` for parallel per-item sequences of ids, blocked_by and writes.

    ``edges`` maps every item index to the indices it must follow (empty for a held item); ``holds``
    maps a held item to its reason. Item i follows each batch item its blocked_by names, in any
    submission position. A blocked_by naming no batch id, or a held item, holds i with the first such
    id in blocked_by order; holds propagate to dependants. Writes overlap between two non-held items
    that blocked_by does not order adds an edge from the later item in the stable topological order
    (ties by submission index) to the earlier, so edges never cycle and never touch a held item.
    The wave plays no part. Raises ValueError for an id claimed by two items, an item blocked by its
    own id, or a blocked_by cycle among batch items.
    """
    n = len(ids)
    owner: dict[str, int] = {}
    for i, item_ids in enumerate(ids):
        for ident in item_ids:
            if owner.setdefault(ident, i) != i:
                raise ValueError(f"id {ident!r} is claimed by items {owner[ident]} and {i}")
    for i in range(n):
        if any(owner.get(b) == i for b in blocked_by[i]):
            raise ValueError(f"item {i} is blocked by its own id")
    deps = [{owner[b] for b in blocked_by[i] if b in owner} for i in range(n)]

    order: list[int] = []
    remaining = set(range(n))
    while remaining:
        ready = [i for i in sorted(remaining) if not (deps[i] & remaining)]
        if not ready:
            raise ValueError("blocked_by cycle among items " + ", ".join(map(str, sorted(remaining))))
        order.append(ready[0])
        remaining.discard(ready[0])

    holds: dict[int, str] = {}
    for i in order:
        for b in blocked_by[i]:
            if b not in owner:
                holds[i] = f"blocked_by {b}, which is not in this fan-out"
            elif owner[b] in holds:
                holds[i] = f"blocked_by {b}, which is held"
            else:
                continue
            break

    anc: dict[int, set[int]] = {}
    follows: dict[int, set[int]] = {i: set() if i in holds else set(deps[i]) for i in range(n)}
    placed: list[int] = []
    for b in order:
        if b in holds:
            continue
        anc[b] = set()
        for d in follows[b]:
            anc[b] |= anc[d] | {d}
        for a in placed:
            if a not in anc[b] and _writes_overlap(writes[a], writes[b]):
                follows[b].add(a)
                anc[b] |= anc[a] | {a}
        placed.append(b)
    return {i: tuple(sorted(follows[i])) for i in range(n)}, holds


def release(
    parent: ParentRecord,
    statuses: Mapping[str, str],
    skip: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Run ids a close frees, in submission order.

    A run is released when it is waiting (`after` or `queued`) and every `after` predecessor reads
    `landed`. A halted predecessor never releases its successors. A run in *skip* is never freed.
    """
    freed = []
    for run in parent.runs:
        if statuses.get(run.run_id, run.status) not in (STATUS_AFTER, STATUS_QUEUED) or run.run_id in skip:
            continue
        if any(statuses.get(p) != STATUS_LANDED for p in run.after):
            continue
        freed.append(run.run_id)
    return tuple(freed)
