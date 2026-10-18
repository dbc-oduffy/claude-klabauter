"""
coordinator_core.ops.dispatch_emit.run_close -- the run-close leg vocabulary, close-block
builder and close-job record.

Purpose: the shared interface of ``dispatch.terminal_commit``'s ``close`` block. The inline
leg modules and ``close.background`` both speak ``LegResult``; the close-job record is how a
queued leg's outcome reaches the reader. The record also carries ``review_receipt`` and the
range endpoints ``run_base_sha`` / ``terminal_sha``; ``render_unreceipted`` words the
``review`` leg's detail.

Negative-spec:
  - No git, no subprocess, no heavy import: terminal_commit has an 80ms cold-import bar.
  - A run-bound leg nobody reported is ``failed`` ("leg did not run"), never absent.
  - A ``queued`` leg is not ``outstanding`` at reply time; ``read_close_job`` is what turns
    an unfinished one into ``failed``.
  - No detail or record carries an absolute, drive-lettered or backslashed path.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

RUN_BOUND_LEGS: tuple[str, ...] = (
    "review",
    "plan-reconciliation",
    "lessons",
    "completion-entry",
    "baton-ship",
    "baton-drain",
    "orientation-refresh",
    "memo-lifecycle",
    "post-close",
)
SESSION_BOUND_LEGS: tuple[str, ...] = ("session-shape", "session-residue")
STATUSES: frozenset[str] = frozenset(
    {"ran", "discharged", "not-applicable", "queued", "failed", "session-bound"}
)

_DRIVE = re.compile(r"^[A-Za-z]:")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
DETAIL_CAP = 160
_TOKEN_EDGES = "\"'()[]{}<>,;`"


def _unhygienic_token(text: str) -> Optional[str]:
    for raw in text.split():
        token = raw.strip(_TOKEN_EDGES)
        if "\\" in token or token.startswith("/") or _DRIVE.match(token):
            return token
    return None


_PATHISH = re.compile(r"[A-Za-z]:[\\/]\S*|\S*\\\S*|(?<!\w)/\S+")


def scrub_detail(text: str, root: Optional[Union[str, os.PathLike]] = None) -> str:
    """Return *text* with whitespace collapsed, every non-repo-relative path made safe, capped at ``DETAIL_CAP``.

    A path token under *root* becomes its repo-relative POSIX form; any other path-shaped token
    becomes ``<path>``. The result passes ``check_path_hygiene``. Pure string work.
    """
    prefix = ""
    if root is not None:
        prefix = os.fspath(root).replace("\\", "/").rstrip("/").casefold() + "/"

    def _swap(match: "re.Match[str]") -> str:
        token = match.group(0)
        if prefix:
            norm = token.replace("\\", "/")
            # The prefix ends in "/" so <drive>:/repo-other never matches root <drive>:/repo.
            if norm.casefold().startswith(prefix) and len(norm) > len(prefix):
                rel = norm[len(prefix):]
                if not _unhygienic_token(rel):
                    return rel
        return "<path>"

    return _PATHISH.sub(_swap, " ".join(str(text).split()))[:DETAIL_CAP]


def check_path_hygiene(detail: str) -> None:
    """Raise ValueError when *detail* carries an absolute, drive-lettered or backslashed path."""
    bad = _unhygienic_token(detail)
    if bad is not None:
        raise ValueError(f"leg detail carries a non-repo-relative path: {bad!r}")


@dataclass(frozen=True)
class LegResult:
    """One leg's outcome; ``detail`` is refused if it names a non-repo-relative path."""

    status: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown leg status: {self.status!r}")
        check_path_hygiene(self.detail)

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


CLOSE_KINDS: tuple[str, ...] = ("plan-run", "mise-run", "xs-s-run", "run-less-xs-s")


def _check_kind(kind: str) -> None:
    if kind not in CLOSE_KINDS:
        raise ValueError(f"unknown close kind: {kind!r}")


def kind_presets(kind: str, *, plan_governs: bool) -> dict[str, LegResult]:
    """The run-bound legs *kind* does not owe, each ``not-applicable`` with the ledger's reason.

    *plan_governs* matters only for ``xs-s-run``: true for an S spec-dispatch plan, false for
    a minted XS spine. Every leg not returned is paid by its runner for every kind.
    """
    _check_kind(kind)
    if kind == "mise-run":
        return {
            "plan-reconciliation": LegResult(
                "not-applicable", "mise run: no governing plan; source rows coded on their plans"
            ),
            "memo-lifecycle": LegResult(
                "not-applicable", "mise run: memo lifecycle has no run home (ledger)"
            ),
        }
    if kind == "xs-s-run" and not plan_governs:
        return {"plan-reconciliation": LegResult("not-applicable", "XS run: no plan governs")}
    return {}


def build_close_block(
    run_id: str,
    applies: bool,
    legs: Mapping[str, LegResult],
    completion_entry: Any = None,
    *,
    kind: str = "plan-run",
) -> dict[str, Any]:
    """Build ``{applies, kind, run_id, legs, outstanding, completion_entry}``.

    ``outstanding`` is every run-bound leg whose status is ``failed``. With ``applies``
    false every run-bound leg is ``not-applicable``. *kind* must be in ``CLOSE_KINDS``.
    """
    _check_kind(kind)
    check_path_hygiene(run_id)
    out: dict[str, dict[str, str]] = {}
    for leg in RUN_BOUND_LEGS:
        if not applies:
            result = LegResult("not-applicable", "run does not land code")
        else:
            result = legs.get(leg) or LegResult("failed", "leg did not run")
        out[leg] = result.to_dict()
    for leg in SESSION_BOUND_LEGS:
        out[leg] = LegResult("session-bound").to_dict()
    outstanding = [leg for leg in RUN_BOUND_LEGS if out[leg]["status"] == "failed"]
    return {
        "applies": applies,
        "kind": kind,
        "run_id": run_id,
        "legs": out,
        "outstanding": outstanding,
        "completion_entry": completion_entry,
    }


def render_unreceipted(
    uncovered: Iterable[str],
    *,
    truncated: bool,
    cap: int,
) -> str:
    """The ``review`` leg detail naming unreceipted ancestor commits, at most ``DETAIL_CAP`` chars.

    *uncovered* is newest first; shas render as 12 chars and what does not fit becomes
    ``(+N more)``. A *truncated* walk names where review scope stopped (the oldest uncovered
    sha) and still leads with the newest uncovered sha.
    """
    shas = [sha[:12] for sha in uncovered]
    if truncated:
        stop = shas[-1] if shas else ""
        text = f"range truncated at {cap} commits"
        if stop:
            text += f"; review scope not computed past {stop}"
        if shas:
            text += f"; newest uncovered {shas[0]}"
        return text[:DETAIL_CAP]
    head = f"{len(shas)} unreceipted ancestor commits"
    if not shas:
        return head
    head += ": "
    shown: list[str] = []
    for index, sha in enumerate(shas):
        rest = len(shas) - index - 1
        candidate = ", ".join([*shown, sha])
        suffix = f" (+{rest} more)" if rest else ""
        if shown and len(head) + len(candidate) + len(suffix) > DETAIL_CAP:
            break
        shown.append(sha)
    hidden = len(shas) - len(shown)
    return head + ", ".join(shown) + (f" (+{hidden} more)" if hidden else "")


def close_job_path(run_id: str) -> str:
    """Repo-relative forward-slash path of the run's close-job record."""
    return f".coordinator-local/close-jobs/{run_id}.json"


def _job_file(root: Path, run_id: str) -> Path:
    return Path(root) / close_job_path(run_id)


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def write_close_job(
    root: Path,
    run_id: str,
    queued_legs: Iterable[str],
    deadline: datetime,
    *,
    session_id: Optional[str] = None,
    plan_rel: Optional[str] = None,
    source_memo: Optional[str] = None,
    claimed_batons: Iterable[str] = (),
    completion_entry: Optional[str] = None,
    settled: Optional[Mapping[str, LegResult]] = None,
    review_receipt: Optional[str] = None,
    run_base_sha: Optional[str] = None,
    terminal_sha: Optional[str] = None,
    criterion: Optional[Mapping[str, Any]] = None,
    falsifier_broken: Optional[Sequence[str]] = None,
) -> str:
    """Write the record in one atomic write; return its repo-relative path.

    Beyond the queued legs and deadline it carries the session attribution
    ``session_run_closes`` reads and the inline legs *settled* at write time. *review_receipt*
    is the repo-relative path whose ``review_stamp`` proves the review; the two shas are the
    run's range endpoints, full 40-hex or ``None``. *criterion* is the judge's verdict as
    returned (not a leg detail, so not path-checked); *falsifier_broken* is the fired tells,
    an empty list meaning BROKEN with none named.
    """
    _check_criterion(criterion)
    _check_falsifier_broken(falsifier_broken)
    for name, sha in (("run_base_sha", run_base_sha), ("terminal_sha", terminal_sha)):
        if sha is not None and not _FULL_SHA.match(sha):
            raise ValueError(f"{name} is not a full 40-hex sha: {sha!r}")
    legs = {leg: {"status": "queued", "detail": ""} for leg in queued_legs}
    for leg in legs:
        if leg not in RUN_BOUND_LEGS:
            raise ValueError(f"not a run-bound leg: {leg!r}")
    settled_dict: dict[str, dict[str, str]] = {}
    for leg, result in (settled or {}).items():
        if leg not in RUN_BOUND_LEGS:
            raise ValueError(f"not a run-bound leg: {leg!r}")
        if leg in legs:
            raise ValueError(f"leg both queued and settled: {leg!r}")
        settled_dict[leg] = result.to_dict()
    batons = list(claimed_batons)
    for text in (run_id, session_id, plan_rel, source_memo, completion_entry, review_receipt, *batons):
        if text is not None:
            check_path_hygiene(text)
    _write_atomic(
        _job_file(root, run_id),
        {
            "run_id": run_id,
            "deadline": deadline.isoformat(),
            "legs": legs,
            "session_id": session_id,
            "plan_rel": plan_rel,
            "source_memo": source_memo,
            "claimed_batons": batons,
            "completion_entry": completion_entry,
            "settled": settled_dict,
            "review_receipt": review_receipt,
            "run_base_sha": run_base_sha,
            "terminal_sha": terminal_sha,
            "criterion": None if criterion is None else dict(criterion),
            "falsifier_broken": None if falsifier_broken is None else list(falsifier_broken),
        },
    )
    return close_job_path(run_id)


UNCHANGED: Any = object()


def _check_criterion(criterion: Any) -> None:
    if criterion is not None and not isinstance(criterion, Mapping):
        raise ValueError(f"criterion is not a mapping: {type(criterion).__name__}")


def _check_falsifier_broken(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, (list, tuple)) or not all(isinstance(i, str) for i in value):
        raise ValueError("falsifier_broken is not a list of str")


def amend_close_job(
    root: Path,
    run_id: str,
    *,
    criterion: Optional[Mapping[str, Any]] = None,
    falsifier_broken: Any = UNCHANGED,
    review_receipt: Optional[str] = None,
    settled: Optional[Mapping[str, LegResult]] = None,
    queue: Iterable[str] = (),
    deadline: Optional[datetime] = None,
) -> None:
    """Change only the given fields of an existing record, in one atomic write.

    ``falsifier_broken=None`` writes null (mark read, not BROKEN); ``UNCHANGED`` leaves the
    key as it was, absent included. *queue* sets each named run-bound leg ``queued`` and
    requires *deadline*; legs still ``queued`` past the old deadline are written ``failed``
    before it is replaced.
    """
    _check_criterion(criterion)
    if falsifier_broken is not UNCHANGED:
        _check_falsifier_broken(falsifier_broken)
    if review_receipt is not None:
        check_path_hygiene(review_receipt)
    queued = list(queue)
    if queued and deadline is None:
        raise ValueError("queue requires deadline")
    for leg in queued:
        if leg not in RUN_BOUND_LEGS:
            raise ValueError(f"not a run-bound leg: {leg!r}")
    settled_dict: dict[str, dict[str, str]] = {}
    for leg, result in (settled or {}).items():
        if leg not in RUN_BOUND_LEGS:
            raise ValueError(f"not a run-bound leg: {leg!r}")
        if leg in queued:
            raise ValueError(f"leg both queued and settled: {leg!r}")
        settled_dict[leg] = result.to_dict()
    record = _load(root, run_id)
    if record is None:
        raise FileNotFoundError(close_job_path(run_id))
    for leg in settled_dict:
        if leg in record["legs"]:
            raise ValueError(f"leg both queued and settled: {leg!r}")
    if criterion is not None:
        record["criterion"] = dict(criterion)
    if falsifier_broken is not UNCHANGED:
        record["falsifier_broken"] = None if falsifier_broken is None else list(falsifier_broken)
    if review_receipt is not None:
        record["review_receipt"] = review_receipt
    if settled_dict:
        merged = record.get("settled")
        record["settled"] = {**(merged if isinstance(merged, dict) else {}), **settled_dict}
    if deadline is not None:
        try:
            old = datetime.fromisoformat(str(record.get("deadline", "")))
            now = datetime.now(old.tzinfo)
        except ValueError:
            now = datetime.now(deadline.tzinfo)
        for leg, result in _resolve(record, run_id, now).items():
            entry = record["legs"][leg]
            if result.status == "failed" and isinstance(entry, dict) and entry.get("status") == "queued":
                record["legs"][leg] = result.to_dict()
        record["deadline"] = deadline.isoformat()
    for leg in queued:
        record["legs"][leg] = {"status": "queued", "detail": ""}
    _write_atomic(_job_file(root, run_id), record)


def _load(root: Path, run_id: str) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(_job_file(root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("legs"), dict) else None


def resolve_close_job_stem(root: Path, run_id: str) -> Optional[str]:
    """Return the stem of the run's on-disk close-job record, or None when neither exists.

    *run_id* wins when it reads a record; else ``<run_id>.workflow``. A *run_id* already
    ending ``.workflow`` is never suffixed a second time.
    """
    candidates = [run_id]
    if not run_id.endswith(".workflow"):
        candidates.append(f"{run_id}.workflow")
    for stem in candidates:
        if _load(root, stem) is not None:
            return stem
    return None


def record_leg_outcome(
    root: Path, run_id: str, leg: str, status: str, detail: str = ""
) -> None:
    """Record *leg*'s outcome in the run's close-job record (the job's only write)."""
    result = LegResult(status, detail)
    record = _load(root, run_id)
    if record is None:
        raise FileNotFoundError(close_job_path(run_id))
    if leg not in record["legs"]:
        raise ValueError(f"leg not queued in {close_job_path(run_id)}: {leg!r}")
    record["legs"][leg] = result.to_dict()
    _write_atomic(_job_file(root, run_id), record)


def read_close_job(
    root: Path,
    run_id: str,
    now: datetime,
    legs: Iterable[str] = (),
) -> dict[str, LegResult]:
    """Resolve each queued leg to its recorded outcome.

    A leg is ``failed`` when the record is absent (*legs* names the legs owed then) or
    the deadline has passed with the leg still unrecorded; otherwise it stays ``queued``.
    """
    record = _load(root, run_id)
    if record is None:
        return {leg: LegResult("failed", "job record missing") for leg in legs}
    return _resolve(record, run_id, now)


def _resolve(record: dict[str, Any], run_id: str, now: datetime) -> dict[str, LegResult]:
    deadline_text = str(record.get("deadline", ""))
    try:
        late = now > datetime.fromisoformat(deadline_text)
    except ValueError:
        late = True
    resolved: dict[str, LegResult] = {}
    for leg, entry in record["legs"].items():
        status = entry.get("status") if isinstance(entry, dict) else None
        if status in STATUSES and status != "queued":
            resolved[leg] = LegResult(status, str(entry.get("detail", "")))
        elif late:
            resolved[leg] = LegResult("failed", f"job not finished by {deadline_text}")
        else:
            resolved[leg] = LegResult("queued", f"job record {close_job_path(run_id)}")
    return resolved


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


ROWS_COMMITTED_KEY = "rows_committed"


def sibling_release(
    root: Path,
    run_id: str,
    landed_sha: Optional[str],
    budget: Optional[int] = None,
    rows_committed: bool = False,
    closed_by: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Close *run_id*'s sibling status and name what the close frees.

    ``None`` unless the run's own status file exists (the slice bind wrote it). Otherwise rewrites only
    that file (``landed`` with *landed_sha*, or ``halted`` when it is ``None``), reads the write-once
    parent record and every sibling's status file, and returns ``{parent_id, released: [fire call],
    blocked: [{run_id, slice_sizing}]}``. A halted run frees its budget slot but releases no ``after``
    successor, unless it halted with *rows_committed* (every row's work landed) or *closed_by* (the plan
    or other evidence that closed it outside the engine): either mark is stored in its status file, survives
    a later halted rewrite, and such a predecessor releases its successors as a landed one does. ``blocked``
    names each waiting run behind a halted-with-open-rows or blocked predecessor. A waiting run carrying a
    supersession marker is never released and never blocked; it is reported under ``superseded:
    [{run_id, superseded_by}]``, a key present only when non-empty. A ``landed``
    status is never rewritten as halted: that call returns ``None`` and writes nothing.
    """
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
    from coordinator_core.ops.dispatch_emit import sibling_hold
    from coordinator_core.session import claimed_write
    from coordinator_core.warp_fire import fire_command

    base = Path(root)
    own = base / contract.status_rel(run_id)
    prior = _read_json(own)
    if prior is None:
        return None
    if not landed_sha and prior.get("status") == contract.STATUS_LANDED:
        return None  # invariant: a landed run is never halted
    entry: dict[str, Any] = {"status": contract.STATUS_HALTED}
    if landed_sha:
        entry = {"status": contract.STATUS_LANDED, "sha": landed_sha}
    elif rows_committed or (
        prior.get("status") == contract.STATUS_HALTED and prior.get(ROWS_COMMITTED_KEY) is True
    ):
        entry[ROWS_COMMITTED_KEY] = True
    if not landed_sha:
        kept = closed_by or (
            prior.get(contract.CLOSED_BY_KEY) if prior.get("status") == contract.STATUS_HALTED else None
        )
        if isinstance(kept, str) and kept:
            entry[contract.CLOSED_BY_KEY] = kept
    if isinstance(prior.get("parent_id"), str):
        entry["parent_id"] = prior["parent_id"]
    claimed_write.replace_text(own, json.dumps(entry, sort_keys=True) + "\n")

    parent_id = entry.get("parent_id") or _own_parent_id(base, run_id, contract)
    parent_data = _read_json(base / contract.parent_record_rel(parent_id)) if parent_id else None
    if parent_data is None:
        return None
    try:
        parent = contract.ParentRecord.from_json(parent_data)
    except (ValueError, KeyError, TypeError):
        return None
    statuses: dict[str, str] = {}
    complete: set[str] = set()
    for run in parent.runs:
        data = _read_json(base / contract.status_rel(run.run_id))
        status = data.get("status") if data else None
        if status == contract.STATUS_RUNNING and run.run_id != run_id:
            if sibling_hold.reconcile_running(base, run.run_id):
                status = contract.STATUS_LANDED
        statuses[run.run_id] = status if status in contract.RUN_STATUSES else run.status
        if status == contract.STATUS_HALTED and (
            data.get(ROWS_COMMITTED_KEY) is True or data.get(contract.CLOSED_BY_KEY)
        ):
            complete.add(run.run_id)
    budget = contract.concurrent_run_budget() if budget is None else budget
    as_landed = {rid: (contract.STATUS_LANDED if rid in complete else st) for rid, st in statuses.items()}
    waiting = (contract.STATUS_AFTER, contract.STATUS_QUEUED)
    superseded: list[dict[str, str]] = []
    for run in parent.runs:
        if statuses[run.run_id] in waiting:
            by = contract.read_superseded_by(base, run.run_id)
            if by is not None:
                superseded.append({"run_id": run.run_id, "superseded_by": by})
    skip = frozenset(item["run_id"] for item in superseded)
    freed = contract.release(parent, as_landed, budget, skip)
    by_id = {r.run_id: r for r in parent.runs}
    dead = {rid for rid, st in statuses.items() if st == contract.STATUS_HALTED and rid not in complete}
    blocked: list[dict[str, str]] = []
    for run in parent.runs:
        if statuses[run.run_id] not in waiting or run.run_id in skip:
            continue
        if any(p in dead for p in run.after):
            dead.add(run.run_id)
            blocked.append({"run_id": run.run_id, "slice_sizing": run.slice_sizing})
    reply: dict[str, Any] = {
        "parent_id": parent.parent_id,
        "released": [fire_command(by_id[rid].slice_sizing) for rid in freed],
        "blocked": blocked,
    }
    if superseded:
        reply["superseded"] = superseded
    return reply


def close_halted_runs_for_plan(root: Path, plan_rel: str, sizing_rel: str) -> dict[str, Any]:
    """Close the halted run(s) naming *sizing_rel* under *plan_rel* and gather what each close frees.

    Returns ``{closed: [run_id], released: [fire call], superseded: [{run_id, superseded_by}]}``. The
    status file is the one a run's own close writes; a halted run with none (never bound) is skipped.
    No subprocess, no git.
    """
    from coordinator_core.ops.dispatch_emit import halted_run

    base = Path(root)
    out: dict[str, Any] = {"closed": [], "released": [], "superseded": []}
    found = (
        halted_run.halted_ask_run_for(base, sizing_rel),
        halted_run.halted_run_for(base, sizing_rel),
    )
    for rid in dict.fromkeys(r for r in found if r):
        reply = sibling_release(base, rid, None, closed_by=plan_rel)
        if reply is None:
            continue
        out["closed"].append(rid)
        out["released"].extend(x for x in reply["released"] if x not in out["released"])
        out["superseded"].extend(reply.get("superseded", ()))
    return out


def _own_parent_id(base: Path, run_id: str, contract: Any) -> Optional[str]:
    copy = _read_json(base / contract.run_parent_copy_rel(run_id))
    parent_id = copy.get("parent_id") if copy else None
    return parent_id if isinstance(parent_id, str) and parent_id else None


DISCHARGED_BY_KEY = "discharged_by"
DISCHARGING_STATUSES: frozenset[str] = frozenset({"ran", "discharged"})


@dataclass(frozen=True)
class RunClose:
    """One close that may have paid legs: a plan-run's record or a run-less entry.

    For a run-less close ``run_id`` is the entry's repo-relative path and ``legs`` holds
    only ``completion-entry``.
    """

    run_id: str
    plan_rel: Optional[str] = None
    source_memo: Optional[str] = None
    claimed_batons: tuple[str, ...] = ()
    completion_entry: Optional[str] = None
    legs: Mapping[str, LegResult] | None = None
    review_receipt: Optional[str] = None
    run_base_sha: Optional[str] = None
    terminal_sha: Optional[str] = None


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _parse_close(record: dict[str, Any], run_id: str, now: datetime) -> RunClose:
    legs: dict[str, LegResult] = {}
    settled = record.get("settled") or {}
    if not isinstance(settled, dict):
        raise ValueError("settled is not a mapping")
    for leg, entry in settled.items():
        if leg not in RUN_BOUND_LEGS or not isinstance(entry, dict):
            raise ValueError(f"bad settled leg: {leg!r}")
        legs[leg] = LegResult(str(entry.get("status")), str(entry.get("detail", "")))
    legs.update(_resolve(record, run_id, now))
    batons = record.get("claimed_batons") or []
    if not isinstance(batons, list):
        raise ValueError("claimed_batons is not a list")
    return RunClose(
        run_id=run_id,
        plan_rel=_str_or_none(record.get("plan_rel")),
        source_memo=_str_or_none(record.get("source_memo")),
        claimed_batons=tuple(str(b) for b in batons),
        completion_entry=_str_or_none(record.get("completion_entry")),
        legs=legs,
        review_receipt=_str_or_none(record.get("review_receipt")),
        run_base_sha=_str_or_none(record.get("run_base_sha")),
        terminal_sha=_str_or_none(record.get("terminal_sha")),
    )


def session_run_closes(
    root: Path, sid: str, now: datetime, since: Optional[float] = None
) -> tuple[list[RunClose], list[str]]:
    """Return ``(closes, unreadable)`` for the plan-run records *sid* wrote, oldest first.

    Files with ``st_mtime`` before *since* (epoch seconds) are skipped on ``stat`` alone.
    A record without ``session_id`` (WS1 era) is never attributed. A malformed record is
    named in *unreadable* (repo-relative), never dropped.
    """
    jobs = Path(root) / ".coordinator-local" / "close-jobs"
    found: list[tuple[float, str, RunClose]] = []
    unreadable: list[str] = []
    for path in jobs.glob("*.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if since is not None and mtime < since:
            continue
        rel = close_job_path(path.stem)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("legs"), dict):
                raise ValueError("not a close-job record")
            if data.get("session_id") != sid:
                continue
            found.append((mtime, path.stem, _parse_close(data, path.stem, now)))
        except (OSError, ValueError):
            unreadable.append(rel)
    found.sort(key=lambda item: (item[0], item[1]))
    return [close for _, _, close in found], sorted(unreadable)


def run_less_close(root: Path, sid: str) -> Optional[RunClose]:
    """The close keyed on *sid*'s own filled completion entry, or ``None``.

    One glob on the session's sid6 suffix. The entry must carry ``authored_by == sid``,
    no ``run_id`` and no scaffold residue; it pays only ``completion-entry``.
    """
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter
    from coordinator_core.ops.coordinator_complete_entry import scaffold_residue_fields

    base = Path(root)
    for path in sorted(base.glob(f"archive/completed/*/*-{sid[-6:]}.md"), reverse=True):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text).get("frontmatter") or {}
        if fm.get("authored_by") != sid or fm.get("run_id"):
            continue
        if scaffold_residue_fields(str(path)):
            continue
        rel = path.relative_to(base).as_posix()
        return RunClose(
            run_id=rel,
            completion_entry=rel,
            legs={"completion-entry": LegResult("ran", rel)},
        )
    return None


def session_closes(
    root: Path, sid: str, now: datetime, since: Optional[float] = None
) -> tuple[list[RunClose], list[str]]:
    """``session_run_closes`` with *sid*'s run-less close (if any) prepended, oldest first."""
    closes, unreadable = session_run_closes(root, sid, now, since)
    run_less = run_less_close(root, sid)
    if run_less is not None:
        closes = [run_less, *closes]
    return closes, unreadable


def dir_birth_time(path: Path) -> Optional[float]:
    """Creation time (epoch seconds) of *path*, or ``None`` when the platform has none.

    Never ``st_mtime`` (advances per entry added) and never POSIX ``st_ctime`` (inode change).
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    birth = getattr(st, "st_birthtime", None)
    if birth is None and os.name == "nt":
        birth = st.st_ctime
    return None if birth is None else float(birth)


def discharge(
    directives: Iterable[dict[str, Any]],
    leg_of: Callable[[dict[str, Any]], Optional[str]],
    closes: Iterable[RunClose],
    eligible: Optional[Callable[[dict[str, Any], RunClose], bool]] = None,
    unreadable: Iterable[str] = (),
) -> dict[str, Any]:
    """Stamp ``discharged_by`` on every directive and return the gate payload.

    ``leg_of`` maps a directive to its run-bound leg (``None`` for an unmapped one).
    A mapped directive is discharged by the latest close (last in *closes*) whose leg
    status is in ``DISCHARGING_STATUSES`` and for which ``eligible`` holds. Payload:
    ``{applies, run_ids, outstanding, discharged, verdict, reason}``; ``verdict`` is
    ``indeterminate`` whenever *unreadable* is non-empty.
    """
    ordered = list(closes)
    applies = bool(ordered)
    discharged: dict[str, str] = {}
    outstanding: list[str] = []
    for directive in directives:
        directive[DISCHARGED_BY_KEY] = None
        leg = leg_of(directive)
        if leg is None:
            continue
        did = str(directive.get("id"))
        for close in reversed(ordered):
            result = (close.legs or {}).get(leg)
            if result is None or result.status not in DISCHARGING_STATUSES:
                continue
            if eligible is not None and not eligible(directive, close):
                continue
            directive[DISCHARGED_BY_KEY] = close.run_id
            discharged[did] = close.run_id
            break
        else:
            if applies:
                outstanding.append(did)
    bad = list(unreadable)
    if bad:
        verdict, reason = "indeterminate", f"unreadable close-job records: {', '.join(bad)}"
    else:
        verdict, reason = ("applicable" if applies else "not-applicable"), ""
    return {
        "applies": applies,
        "run_ids": [close.run_id for close in ordered],
        "outstanding": outstanding,
        "discharged": discharged,
        "verdict": verdict,
        "reason": reason,
    }
