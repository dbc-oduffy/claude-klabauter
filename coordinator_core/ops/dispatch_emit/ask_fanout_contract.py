"""Multi-ask submission contract: types, paths and pure checks shared by the fan-out op, the submit hook and run close.

Leaf module, stdlib only. ``ledger_partition`` answers whether every parent row is claimed by exactly
one slice; ``after_edges`` which slice pairs must be sequenced; ``concurrent_run_budget`` how many
sibling runs of one parent may run at once; ``admit`` which ready runs wait; ``release`` which runs a
close frees. ``AskSlice.source`` is a slice's own ask source (None reads as ``pm``);
``SLICE_SOURCE_KEY`` is the ``em_analysis`` key a relayed slice sizing carries; ``slice_ask_source``
reads it back. Reference: docs/reference/multi-ask-submission-contract.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Optional, Sequence

FANOUT_OP = "dispatch.ask_fanout"
PARENT_SCHEMA = "ask-parent"
PARENT_VERSION = 1

STATUS_READY = "ready"
STATUS_AFTER = "after"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_LANDED = "landed"
STATUS_HALTED = "halted"
RUN_STATUSES = (
    STATUS_READY,
    STATUS_AFTER,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_LANDED,
    STATUS_HALTED,
)

# Submission params in, reply dict out; the hook injects it so it never imports the op module.
FanoutFn = Callable[[dict, Any], dict]


@dataclass(frozen=True)
class AskSlice:
    """One separable ask. ``writes`` None means no hint was given (overlaps everything)."""

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
    return f"state/warp/{run_id}/parent.json"


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

    j follows earlier i when their writes overlap (exact path, or one a directory prefix of the other)
    or either carries no hint, or when both carry a ``wave`` and i's is lower. An empty ``writes``
    list is a hint: it overlaps nothing.
    """
    edges: dict[int, tuple[int, ...]] = {}
    for j, later in enumerate(slices):
        edges[j] = tuple(
            i
            for i in range(j)
            if _writes_overlap(slices[i].writes, later.writes) or _earlier_wave(slices[i], later)
        )
    return edges


def concurrent_run_budget(physical_cores: Optional[int] = None) -> int:
    """``max(1, physical_cores // 4)``; reads the box only when the argument is None."""
    if physical_cores is None:
        from coordinator_core.benchmarks.concurrency_probe import default_physical_cores

        physical_cores = default_physical_cores()
    return max(1, physical_cores // 4)


def admit(runs: Sequence[SiblingRun], budget: int) -> tuple[SiblingRun, ...]:
    """Mark ready runs beyond the budget ``queued``, in submission order; never drops a run.

    Running runs occupy slots first; `after` runs wait on predecessors, not on the budget.
    """
    slots = max(0, budget - sum(1 for r in runs if r.status == STATUS_RUNNING))
    out = []
    for run in runs:
        if run.status in (STATUS_READY, STATUS_QUEUED):
            if slots > 0:
                slots -= 1
                run = _with_status(run, STATUS_READY)
            else:
                run = _with_status(run, STATUS_QUEUED)
        out.append(run)
    return tuple(out)


def _with_status(run: SiblingRun, status: str) -> SiblingRun:
    return SiblingRun(run.run_id, run.label, run.after, run.slice_sizing, status)


def release(parent: ParentRecord, statuses: Mapping[str, str], budget: int) -> tuple[str, ...]:
    """Run ids a close frees, in submission order.

    A run is released when it is waiting (`after` or `queued`), every `after`
    predecessor reads `landed`, and a budget slot is free. A halted predecessor never releases its
    successors.
    """
    def status_of(run: SiblingRun) -> str:
        return statuses.get(run.run_id, run.status)

    slots = budget - sum(1 for r in parent.runs if status_of(r) == STATUS_RUNNING)
    freed = []
    for run in parent.runs:
        if status_of(run) not in (STATUS_AFTER, STATUS_QUEUED):
            continue
        if any(statuses.get(p) != STATUS_LANDED for p in run.after):
            continue
        if slots <= 0:
            break
        slots -= 1
        freed.append(run.run_id)
    return tuple(freed)
