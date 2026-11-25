"""Halt marker for a sizing run: write it, find the halted run a refire resumes, mark it taken.

The marker is `halted.json` in the run's record dir. It is rewritten to `resumed`, never
unlinked: terminal_commit drops record-dir deletions, so an unlinked marker would stay an
uncommitted tracked deletion. The marker may carry `run_base_sha` (the halted run's review base,
read by `halted_run_base`); `halted_ask_run_for` finds a halted `ask-*` run naming a sizing.
`revised_at` is stamped by `mark_revised` when workflow.revise lands on a halted or resumed run and read by `revised_halt`; `mark_resumed` records the stamp it consumed (`revised_consumed_at`), so a resumed marker holds an unread revision only when `revised_at` differs from it (a marker with no consumed key predates the field and holds none). Stdlib plus the atomic writer; no git. A halt over a split dependency tree stamps `live_breakage` and spawns one detached unimatrix-send alert to the live Group EM (`halt_split_tree`); no other subprocess.

`halted_run_for` skips minted-only shell dirs and re-halts a `resumed` marker whose resumer died unstaged.
Trap: a live resumer still at `minted` looks the same as a dead one, so a second refire in that window re-halts it
(pinned by `test_replay_halted_run_shadow.py::test_resumed_with_a_dead_unstaged_resumer_is_halted_again`).

Accepted residual: two concurrent refires of one halted sizing can both read `halted` before
either rewrites it.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.git import call_io_scope
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_BLOCKING_FIELD, HALT_BLOCKING_ITEMS_FIELD, HALT_HELD_ROWS_FIELD, HALT_LIVE_BREAKAGE_FIELD, HALT_PLAN_FIELD,
    HALT_RAG_CORPUS_DOWN, RECORD_DIR_ROOT, RUN_DIR_ROOT, record_terminal_judge_at, refire_next_action_for, stamp_run_stage_at,
)
from coordinator_core.ops.dispatch_emit.rag_use import RESTORE_MESSAGE
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.ops.dispatch_emit.halt_split_tree import alert_live_gem, find_split_trees
from coordinator_core.ops.dispatch_emit.halt_refire import is_exit_next_action, is_refire_next_action

HALTED_FILE = "halted.json"
STATUS_RESUMED = "resumed"
STATUS_LANDED = "landed"
STATUS_SUPERSEDED = "superseded"
SUPERSEDED_EVIDENCE_FIELD = "superseded_evidence"
PATH_DISPOSITIONS_FIELD = "path_dispositions"
DISPOSITION_LANDED = "landed"
DISPOSITION_SUPERSEDED = "superseded"
DISPOSITION_SCRATCH = "scratch-abandoned"
DISPOSITION_LOST = "lost"
STATUS_CLOSED = "closed"
HALT_RUN_BASE_FIELD = "run_base_sha"
HALT_ENGINE_FIELD = "engine_sha"
SUPERSEDED_BY_REFIRE = "superseded-by-refire"
HALT_REASON_FIELD = "reason"
HALT_RUN_ARTIFACTS_FIELD = "run_artifacts"
REVISED_AT_FIELD = "revised_at"
REVISED_CONSUMED_FIELD = "revised_consumed_at"
RETRACTED_AT_FIELD = "retracted_at"
RETRACTED_REASON_FIELD = "retracted_reason"
HALT_RELEASED_FIELD = "released"
HALT_NEXT_ACTION_FIELD = "next_action"
HALT_POST_LAND_TRIPWIRE_RED = "post_land_tripwire_red"
HALT_REFUSED_PATHS_FIELD = "refused_paths"
_SHA40 = re.compile(r"[0-9a-f]{40}")
_DATE_PREFIX = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

#: The `<%Y%m%dT%H%M%S>-<hex6>` tail of a run id; without it a sibling sizing whose stem extends
#: this one's (`foo` vs `foo-slice-1`) shares the `warp-<stem>-` prefix and shadows the newest dir.
_STAMP = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")


def _latest_note_at(record_dir: Path) -> str:
    """Newest `at` across the run's `notes.jsonl` (EM rulings, own or carried); "" when none or unreadable."""
    latest = ""
    try:
        with open(Path(record_dir) / "notes.jsonl", encoding="utf-8") as fh:
            for line in fh:
                try:
                    at = json.loads(line).get("at")
                except (ValueError, AttributeError):
                    continue
                if isinstance(at, str) and at > latest:
                    latest = at
    except OSError:
        return ""
    return latest


def _norm(sizing_rel: str) -> str:
    return PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()


#: Bounded marker reads are opt-in, for an op that turns `IoIndeterminate` into its own reply
#: (`dispatch.ask_stage`). Every other op reads unbounded: those ops scan many runs' markers, and one
#: foreign marker stalled past the bound would fail the whole op (land_run and review prep failed -32603
#: on unrelated runs' halted.json, 59a3f5 and 250289, 2026-11-22).
_BOUNDED_MARKER_READS: "contextvars.ContextVar[bool]" = contextvars.ContextVar("halted_marker_reads_bounded", default=False)


class bounded_marker_reads:
    """Within this block, `halted.json` reads are bounded and raise `IoIndeterminate` on a stall."""

    __slots__ = ("_token",)

    def __enter__(self) -> None:
        self._token = _BOUNDED_MARKER_READS.set(True)

    def __exit__(self, *exc_info: object) -> None:
        _BOUNDED_MARKER_READS.reset(self._token)


def _marker_text(record_dir: Path) -> str:
    """`halted.json` text, read through the open `call_io_scope` (one read per path per scope);
    FileNotFoundError when absent. Inside `bounded_marker_reads`, `IoIndeterminate` (never an OSError)
    when the read stalls past the bound, so `_read` and `read_strict` propagate it rather than map it to
    absence. `_write` drops the cached read."""
    raw = call_io_scope.read_bytes(Path(record_dir) / HALTED_FILE, bounded=_BOUNDED_MARKER_READS.get())
    if raw is None:
        raise FileNotFoundError(HALTED_FILE)
    return raw.decode("utf-8")


def _read(record_dir: Path) -> Optional[dict]:
    try:
        data = json.loads(_marker_text(record_dir))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_strict(record_dir: Path) -> Optional[dict]:
    """The marker, None when `halted.json` is absent; ValueError when it exists but is unreadable,
    not JSON, or not an object. For hold computations, where a corrupt marker must hold."""
    try:
        data = json.loads(_marker_text(record_dir))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable halt marker in {record_dir}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"halt marker in {record_dir} is not an object")
    return data


UNREADABLE_HALT_RECORD = "unreadable-halt-record"


class UnreadableHaltRecord(ValueError):
    """A halt marker that exists but cannot be read; `reason` names the run dir it came from."""

    def __init__(self, run_id: str, detail: str = "") -> None:
        self.run_id = run_id
        self.reason = f"{UNREADABLE_HALT_RECORD}:{run_id}"
        super().__init__(f"{self.reason} {detail}".strip())


def read_strict_run(record_dir: Path) -> Optional[dict]:
    """`read_strict` raising `UnreadableHaltRecord` (run_id = the record dir's name) on a corrupt marker."""
    try:
        return read_strict(Path(record_dir))
    except ValueError as exc:
        raise UnreadableHaltRecord(Path(record_dir).name, str(exc)) from exc


def _write(record_dir: Path, payload: dict) -> Path:
    target = record_dir / HALTED_FILE
    atomic_write_bytes(target, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    call_io_scope.invalidate(path=target)
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    halted_withheld_index.forget_fresh()
    _track_lookup(Path(record_dir), payload)
    return target


#: Machine-local lookup index: `<LOOKUP_INDEX_DIR>/<sha1(sizing_rel)[:16]>/<run_id>`, one empty file per run whose
#: marker is `halted` or `resumed` for that sizing. `_write` is the one writer of `halted.json`, so every transition
#: (halt, `resumed` rewrite, re-halt, landed, closed, superseded) lands here; a per-run file makes each one a single
#: atomic create or unlink, so peers never lose each other's update. The index answers "does any run hold this
#: sizing?" and which `ask-*` runs to read; supersession by a newer run is still decided from the run dir names.
LOOKUP_INDEX_DIR = ".coordinator-local/halted-run-lookup"


def _lookup_key(sizing_rel: str) -> str:
    """Keyed on the sizing's stem, so a sizing that moved between `state/sizings/` and its month subdir keeps its runs."""
    return hashlib.sha1(PurePosixPath(_norm(sizing_rel)).stem.encode("utf-8")).hexdigest()[:16]


_ROOT_OF: dict = {}  # str(record dir) -> repo root; a layout answer, stable for the process
_ROOT_OF_CAP = 8192


def _root_of(record_dir: Path) -> Optional[Path]:
    """The repo root of a `<root>/<RECORD_DIR_ROOT>/<run>` dir, None for any other layout. A hot per-run call of
    the claim pass: an exact-spelling string match answers without building `Path` parents."""
    key = os.fspath(record_dir)
    if key in _ROOT_OF:
        return _ROOT_OF[key]
    posix = key.replace("\\", "/").rstrip("/")
    name = posix.rsplit("/", 1)[-1]
    tail = "/" + RECORD_DIR_ROOT + "/" + name
    if posix.endswith(tail) and len(posix) > len(tail):
        root: Optional[Path] = Path(key[: len(key.rstrip("\\/")) - len(tail)])
    else:
        rd = Path(record_dir)
        try:
            root = rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
        except IndexError:
            root = None
        if root is not None and root / RECORD_DIR_ROOT / rd.name != rd:
            root = None
    if len(_ROOT_OF) >= _ROOT_OF_CAP:
        _ROOT_OF.clear()
    _ROOT_OF[key] = root
    return root


def _track_lookup(record_dir: Path, payload: dict) -> None:
    """Mirror the marker just written into the lookup index; best-effort, the marker is the truth."""
    sizing = payload.get("sizing_rel")
    root = _root_of(record_dir)
    if not isinstance(sizing, str) or not sizing or root is None or not safe_id(record_dir.name):
        return
    entry = root / LOOKUP_INDEX_DIR / _lookup_key(sizing) / record_dir.name
    _put_bound(root, record_dir.name, sizing, _halt_snapshot(payload))
    try:
        if payload.get("status") in (STATUS_HALTED, STATUS_RESUMED):
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.touch()
        else:
            entry.unlink(missing_ok=True)
    except OSError:
        pass


def _indexed_runs(repo_root: Path, sizing_rel: str) -> set:
    """Run ids the lookup index holds for the sizing: one listing of its per-sizing dir, never a scan of the record root.
    Trap: an `ask-*` halt written before the index existed is absent here and is not back-filled."""
    try:
        return set(os.listdir(Path(repo_root) / LOOKUP_INDEX_DIR / _lookup_key(sizing_rel)))
    except OSError:
        return set()


#: Bound-run index, beside the lookup dir: `<LOOKUP_INDEX_DIR>/<sha1(full normalized sizing_rel)[:16]>.runs/` holds an
#: empty `<run_id>` per run whose `run.json` or `halted.json` binds that sizing, plus `<run_id>.halt` (the marker's
#: `HALT_INDEX_FIELDS`, rewritten by every `_write`). `requirements_ledger._write_run` writes `<run_id>`, `_write` both,
#: so `bound_runs` answers from one listing. Keyed on the full path (not the stem) so the set equals the scan's.
BOUND_DIR_SUFFIX = ".runs"
BOUND_HALT_SUFFIX = ".halt"
BOUND_FILLED = ".filled"
HALT_INDEX_FIELDS = (
    "status", "halted", HALT_REASON_FIELD, HALT_RUN_BASE_FIELD, HALT_NEXT_ACTION_FIELD, HALT_RELEASED_FIELD,
    REVISED_AT_FIELD, REVISED_CONSUMED_FIELD, "sha",
)


def _bound_dir(root: Path, sizing_rel: str) -> Path:
    key = hashlib.sha1(_norm(sizing_rel).encode("utf-8")).hexdigest()[:16]
    return Path(root) / LOOKUP_INDEX_DIR / (key + BOUND_DIR_SUFFIX)


def _halt_snapshot(payload: dict) -> dict:
    return {k: payload[k] for k in HALT_INDEX_FIELDS if k in payload}


def _put_bound(root: Path, run_id: str, sizing_rel: str, halt: Optional[dict]) -> None:
    """Record `run_id` as bound to `sizing_rel`, and its halt fields when `halt` is not None; best-effort, the run
    record is the truth."""
    d = _bound_dir(root, sizing_rel)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / run_id).touch()
        if halt is not None:
            atomic_write_bytes(d / (run_id + BOUND_HALT_SUFFIX), json.dumps(halt, sort_keys=True).encode("utf-8"), preserve_mode=False)
    except OSError:
        pass


def track_run_binding(record_dir: Path, sizing_rel: object, *, was: object = None) -> None:
    """Mirror a `run.json` write into the bound-run index; `was` is the sizing the run was bound to before a rebind."""
    root = _root_of(Path(record_dir))
    name = Path(record_dir).name
    if root is None or not safe_id(name):
        return
    if isinstance(sizing_rel, str) and sizing_rel:
        _put_bound(root, name, sizing_rel, None)
    if isinstance(was, str) and was and _norm(was) != _norm(sizing_rel if isinstance(sizing_rel, str) else ""):
        _unbind(root, Path(record_dir), was)


def _unbind(root: Path, record_dir: Path, old: str) -> None:
    """After a rebind away from `old`: drop the index entries of whichever record (marker, run.json) no longer binds it."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    norm = _norm(old)
    d = _bound_dir(root, old)
    marker_binds = _norm(str((_read(record_dir) or {}).get("sizing_rel") or "")) == norm
    run_binds = _norm(str((rl.read_run(record_dir) or {}).get("sizing_rel") or "")) == norm
    try:
        if not marker_binds:
            (d / (record_dir.name + BOUND_HALT_SUFFIX)).unlink(missing_ok=True)
        if not (marker_binds or run_binds):
            (d / record_dir.name).unlink(missing_ok=True)
    except OSError:
        pass


def _bound_candidate(name: str, floor: str, prefix: str) -> bool:
    """The scan's candidate predicate: `ask-<stamp>` on or after the sizing date, or `warp-<stem>-<stamp>`."""
    if name.startswith("ask-"):
        return bool(_STAMP.fullmatch(name[4:])) and name[4:12] >= floor
    return name.startswith(prefix) and bool(_STAMP.fullmatch(name[len(prefix):]))


def _backfill_bound(root: Path, sizing: str, floor: str, prefix: str) -> dict:
    """One date-floored scan of the record root for runs predating the index: write their entries and the
    `BOUND_FILLED` sentinel (only after a complete scan); returns the runs found. Runs once per sizing per machine."""
    found: dict = {}
    complete = True
    try:
        with os.scandir(Path(root) / RECORD_DIR_ROOT) as it:
            for entry in it:
                if not _bound_candidate(entry.name, floor, prefix):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                dpath = Path(entry.path)
                run_bound, marker = False, None
                try:
                    run = json.loads((dpath / "run.json").read_text(encoding="utf-8"))
                    run_bound = isinstance(run, dict) and isinstance(run.get("sizing_rel"), str) and _norm(run["sizing_rel"]) == sizing
                except (OSError, ValueError):
                    pass
                try:
                    data = json.loads((dpath / HALTED_FILE).read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("sizing_rel"), str) and _norm(data["sizing_rel"]) == sizing:
                        marker = _halt_snapshot(data)
                except (OSError, ValueError):
                    pass
                if run_bound or marker is not None:
                    found[entry.name] = marker or {}
                    _put_bound(root, entry.name, sizing, marker)
    except OSError:
        complete = False
    if complete:
        try:
            d = _bound_dir(root, sizing)
            d.mkdir(parents=True, exist_ok=True)
            (d / BOUND_FILLED).touch()
        except OSError:
            pass
    return found


def bound_runs(repo_root: Path, sizing_rel: str) -> dict:
    """`{run_id: halt fields}` of every run whose `run.json` or `halted.json` binds `sizing_rel`, newest-name last, from
    one listing of the sizing's index dir plus one small read per halted run; the halt fields are `HALT_INDEX_FIELDS`
    (`{}` for a run with no marker). Same candidate set as the scan it replaces (`ask-*` on or after the sizing's
    date, `warp-<stem>-*`); an undated sizing answers `{}`. The first call for a sizing whose index was never filled
    scans the record root once to back-fill runs that predate the index; later calls never touch it.
    Trap: the index is machine-local and written only by `_write` and `requirements_ledger._write_run`; a run dir that
    arrives after the fill by any other route (git pull, hand-copied record) is not seen until its index dir is deleted."""
    root = Path(repo_root)
    sizing = _norm(str(sizing_rel))
    stem = PurePosixPath(sizing).stem
    dated = _DATE_PREFIX.match(stem)
    if dated is None:
        return {}
    floor, prefix = "".join(dated.groups()), f"warp-{stem}-"
    d = _bound_dir(root, sizing)
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    if BOUND_FILLED not in names:
        return dict(sorted(_backfill_bound(root, sizing, floor, prefix).items()))
    out: dict = {}
    for name in sorted(names):
        if name == BOUND_FILLED or name.endswith(BOUND_HALT_SUFFIX) or not _bound_candidate(name, floor, prefix):
            continue
        halt: dict = {}
        if (name + BOUND_HALT_SUFFIX) in names:
            try:
                got = json.loads((d / (name + BOUND_HALT_SUFFIX)).read_text(encoding="utf-8"))
                halt = got if isinstance(got, dict) else {}
            except (OSError, ValueError):
                halt = {}
        try:
            if (root / RECORD_DIR_ROOT / name).is_dir():
                out[name] = halt
        except OSError:
            continue
    return out


def _warp_stamps(root: Path, prefix: str) -> list:
    """Stamps of the `<prefix><stamp>` dirs under `root`, from one directory listing; [] on an unreadable root."""
    stamps = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith(prefix) and _STAMP.fullmatch(entry.name[len(prefix):]) and entry.is_dir():
                    stamps.append(entry.name[len(prefix):])
    except OSError:
        return []
    return stamps


def refused_path_entries(record_dir: Path, refused: Optional[list]) -> list:
    """`[{path, measured, landing}]` for each refused path, in the order given, none dropped.

    An entry is a bare path or a dict carrying `path` (and optionally `measured` / `landing`); a missing
    `measured` is read from the run's `measured-rev.json` and a missing `landing` is the path's check-in blob in
    the run's checkout (hash only, no spawn). An unknown blob is None. Independent of the `reason` prose, which
    the compose side cuts at 600 chars. Never raises."""
    rd = Path(record_dir)
    root = _root_of(rd)
    blobs: dict = {}
    try:
        record = json.loads((rd / "measured-rev.json").read_text(encoding="utf-8"))
        raw = record.get("blobs") if isinstance(record, dict) else None
        blobs = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        pass
    out, seen = [], set()
    for item in refused or ():
        entry = item if isinstance(item, dict) else {"path": item}
        path = entry.get("path")
        if not isinstance(path, str) or not path or path in seen:
            continue
        seen.add(path)
        measured, landing = entry.get("measured"), entry.get("landing")
        if measured is None:
            measured = blobs.get(path)
        if landing is None and root is not None:
            try:
                from coordinator_core.git.commit import checkin_blob_sha

                landing = checkin_blob_sha(root, path)
            except Exception:  # noqa: BLE001 -- an unreadable landing blob is recorded as None, never a failed halt
                landing = None
        out.append({
            "path": path,
            "measured": measured if isinstance(measured, str) else None,
            "landing": landing if isinstance(landing, str) else None,
        })
    return out


def write_halt_marker(
    record_dir: Path,
    *,
    halted: str,
    sizing_rel: Optional[str],
    plan_rel: Optional[str] = None,
    blocking: Optional[tuple] = None,
    run_base_sha: Optional[str] = None,
    blocking_items: Optional[list] = None,
    reason: Optional[str] = None,
    next_action: Optional[dict] = None,
    held_rows: Optional[list] = None,
    refused_paths: Optional[list] = None,
) -> Path:
    """Write the `halted` marker into `record_dir` (created if absent); return its path.

    `plan`, the blocking key, the blocking items, the reason, the next_action, the refused paths and the base are written only when given (the base
    only when 40-hex; next_action only when `halt_refire.is_refire_next_action` or `is_exit_next_action` accepts it), so other halts' markers are unchanged.
    `refused_paths` (a landing refusal's differing paths) lands as `refused_paths: [{path, measured, landing}]`, every path kept (`refused_path_entries`).
    """
    payload = {
        "status": STATUS_HALTED,
        "halted": halted,
        "sizing_rel": _norm(sizing_rel) if sizing_rel else None,
    }
    if plan_rel:
        payload[HALT_PLAN_FIELD] = _norm(plan_rel)
    if blocking is not None:
        payload[HALT_BLOCKING_FIELD] = list(blocking)
    if blocking_items is not None:
        payload[HALT_BLOCKING_ITEMS_FIELD] = list(blocking_items)
    if held_rows is not None:
        payload[HALT_HELD_ROWS_FIELD] = list(held_rows)
    if reason:
        payload[HALT_REASON_FIELD] = reason
    if refused_paths:
        entries = refused_path_entries(Path(record_dir), refused_paths)
        if entries:
            payload[HALT_REFUSED_PATHS_FIELD] = entries
    accepted = next_action is not None and (is_refire_next_action(next_action) or is_exit_next_action(next_action))
    if not accepted and halted == HALT_RAG_CORPUS_DOWN and sizing_rel:
        next_action, accepted = refire_next_action_for(
            halted, run_id=Path(record_dir).name, sizing_rel=_norm(sizing_rel), detail=RESTORE_MESSAGE
        ), True
    if accepted:
        payload[HALT_NEXT_ACTION_FIELD] = next_action
    run_base_sha = _earliest_run_base(Path(record_dir), run_base_sha)
    if run_base_sha:
        payload[HALT_RUN_BASE_FIELD] = run_base_sha
    from coordinator_core.ops.dispatch_emit import halt_resume_record

    running_engine = halt_resume_record.engine_sha()
    if running_engine is not None:
        payload[HALT_ENGINE_FIELD] = running_engine
    root = _target_root(Path(record_dir))
    splits = find_split_trees(root) if root is not None else []
    if splits:
        payload[HALT_LIVE_BREAKAGE_FIELD] = splits
    prior = _prior_live_breakage(Path(record_dir))
    target = _write(Path(record_dir), payload)
    if splits and splits != prior:
        alert_live_gem(root, Path(record_dir).name, splits)
    stamp_run_stage_at(Path(record_dir), "halted")
    if sizing_rel:
        _schedule_resume_build(Path(record_dir))
    return target


def _earliest_run_base(record_dir: Path, given: Optional[str]) -> Optional[str]:
    """The run's first base, never a later one: the write-once run-base file, else the base this run's
    earlier halt marker recorded, else `given` (40-hex only). A re-halt's `given` is the then-HEAD, which is
    the run's own earlier halt-record commit. A base resolved without the file is written back to it when the run's scratch dir exists (never created here)."""
    root = _target_root(record_dir)
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base, run_base_rel

    recorded = recorded_run_base(root, record_dir.name) if root is not None else None
    if recorded and _SHA40.fullmatch(recorded):
        return recorded
    base = halted_run_base(record_dir) or (given if given and _SHA40.fullmatch(given) else None)
    if base and root is not None:
        try:
            with open(root / run_base_rel(record_dir.name), "x", encoding="utf-8", newline="\n") as fh:
                fh.write(base + "\n")
        except OSError:
            pass
    return base


def _prior_live_breakage(record_dir: Path):
    """The `live_breakage` already on this run's marker, or None; a re-halt over the same split does not re-alert."""
    try:
        data = json.loads((record_dir / HALTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get(HALT_LIVE_BREAKAGE_FIELD) if isinstance(data, dict) else None


#: Test seam: a callable `(target_root, run_id)` replacing the detached spawn.
_RESUME_SPAWNER = None


def _target_root(record_dir: Path) -> Optional[Path]:
    """The halted run's target root (`<root>/<RECORD_DIR_ROOT>/<run>`); None when the path is too shallow."""
    try:
        return Path(record_dir).parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
    except IndexError:
        return None


def _schedule_resume_build(record_dir: Path) -> None:
    """Start `halt_resume_build` detached for the halted run at `record_dir`
    (`<root>/<RECORD_DIR_ROOT or scratch/warp>/<run>`); never raises and never waits.

    Skipped under pytest unless `_RESUME_SPAWNER` is set, so a fixture halt never starts a real process."""
    rd = Path(record_dir)
    root = _target_root(rd)
    if root is None:
        return
    try:
        if _RESUME_SPAWNER is not None:
            _RESUME_SPAWNER(root, rd.name)
            return
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

        engine_root = Path(__file__).resolve().parents[3]
        spawn_detached(
            str(engine_root), str(Path(__file__).with_name("halt_resume_build.py")), [str(root), rd.name]
        )
    except Exception:  # noqa: BLE001 -- scheduling is best-effort; the marker is the truth
        pass


def halted_run_base(record_dir: Path) -> Optional[str]:
    """The base sha the halted run recorded in its marker, else None (absent, unreadable, not 40-hex)."""
    marker = _read(Path(record_dir))
    value = marker.get(HALT_RUN_BASE_FIELD) if marker else None
    return value if isinstance(value, str) and _SHA40.fullmatch(value) else None


def halted_ask_run_for(repo_root: Path, sizing_rel: str, *, ignore_superseded: bool = False) -> Optional[str]:
    """Return the newest halted `ask-<stamp>` run naming `sizing_rel`, else None.

    Only ask dirs stamped on or after the sizing filename's YYYY-MM-DD prefix are read (None for an
    undated name), and a `warp-<stem>-*` dir stamped later supersedes the ask run unless
    `ignore_superseded` (the notes-carrying lookup, which resumes nothing).
    """
    found = _halted_asks(repo_root, sizing_rel, ignore_superseded=ignore_superseded, first_only=True)
    return found[0] if found else None


def _halted_asks(repo_root: Path, sizing_rel: str, *, ignore_superseded: bool = False, first_only: bool = False) -> list:
    """Halted `ask-<stamp>` runs naming `sizing_rel`, newest first, under `halted_ask_run_for`'s date floor and
    supersession rule; `first_only` stops at the first (the legacy single-answer read)."""
    norm = _norm(sizing_rel)
    stem = PurePosixPath(norm).stem
    dated = _DATE_PREFIX.match(stem)
    if dated is None:
        return []
    floor = "".join(dated.groups())
    root = Path(repo_root) / RECORD_DIR_ROOT
    asks = [
        name[4:] for name in _indexed_runs(repo_root, norm)
        if name.startswith("ask-") and _STAMP.fullmatch(name[4:]) and name[4:12] >= floor
    ]
    newest_warp = max(_warp_stamps(root, f"warp-{stem}-"), default="") if asks and not ignore_superseded else ""
    found = []
    for stamp in sorted(asks, reverse=True):
        if stamp < newest_warp and not ignore_superseded:
            break
        name = f"ask-{stamp}"
        marker = _read(root / name)
        if marker and marker.get("status") == STATUS_HALTED and marker.get("sizing_rel") == norm:
            if not safe_id(name):
                break
            found.append(name)
            if first_only:
                break
    return found


def notes_predecessor(repo_root: Path, sizing_rel: str, *, exclude: Optional[str] = None) -> Optional[str]:
    """Newest prior run of `sizing_rel`, in any terminal state, that holds a live revise note, else None.

    Candidates are `warp-<stem>-<stamp>` dirs and dated `ask-<stamp>` dirs whose `halted.json` or
    `run.json` names the sizing; `exclude` (the run being composed) is skipped. Resumes nothing:
    the caller only reads its notes. One scandir of `scratch/warp`, no git.
    """
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit import run_notes

    norm = _norm(sizing_rel)
    stem = PurePosixPath(norm).stem
    dated = _DATE_PREFIX.match(stem)
    floor = "".join(dated.groups()) if dated else None
    prefix = f"warp-{stem}-"
    root = Path(repo_root) / RECORD_DIR_ROOT
    found = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                name = entry.name
                if name == exclude or not entry.is_dir():
                    continue
                if name.startswith(prefix):
                    stamp = name[len(prefix):]
                elif name.startswith("ask-") and floor is not None:
                    stamp = name[4:]
                    if stamp[:8] < floor:
                        continue
                else:
                    continue
                if _STAMP.fullmatch(stamp) and safe_id(name):
                    found.append((stamp, name))
    except OSError:
        return None
    for _stamp, name in sorted(found, reverse=True):
        rdir = root / name
        bound = (_read(rdir) or {}).get("sizing_rel") or (rl.read_run(rdir) or {}).get("sizing_rel")
        if not isinstance(bound, str) or _norm(bound) != norm:
            continue
        try:
            if run_notes.live(rl.carried_notes(Path(repo_root), name)):
                return name
        except ValueError:
            continue
    return None


def bound_sizing_rel(repo_root: Path, run_id: str) -> Optional[str]:
    """The sizing a run is bound to (its `halted.json`, else its `run.json`), else None. Two small reads of
    one run's record dir; `run_id` must be a safe id."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    if not safe_id(run_id):
        return None
    rdir = Path(repo_root) / RECORD_DIR_ROOT / run_id
    bound = (_read(rdir) or {}).get("sizing_rel") or (rl.read_run(rdir) or {}).get("sizing_rel")
    return _norm(bound) if isinstance(bound, str) and bound else None


def _is_shell(run_dir: Path) -> bool:
    """A minted-only dir: no marker and a progress record that never left `minted` (or none).

    Such a dir holds no work to resume or to supersede with, so it never hides an older halt.
    """
    if (run_dir / HALTED_FILE).exists():
        return False
    try:
        stage = json.loads((run_dir / "run-stage.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return isinstance(stage, dict) and stage.get("stage") in (None, "minted")


#: Prefix of the `halted` value the deferred-emit launcher records when it minted a run but could not verify its script.
MINTED_NOT_STARTED_HALT = "minted, not started"


def is_unstarted_mint_halt(run_dir: Path) -> bool:
    """A run the launcher halted as minted-not-started: the marker's `halted` names that halt and the
    progress record never left `minted` (a `halted` stamp with no `staged_at` is that same state).

    Trap: `write_halt_marker` stamps the stage `halted`, so the stage alone cannot tell a never-staged run from a
    staged one; `staged_at` is what separates them. `_is_shell` stays False for such a run; only
    `dispatch.minted_run start` admits it.
    """
    marker = _read(run_dir)
    halted = marker.get("halted") if marker else None
    if not (isinstance(halted, str) and halted.startswith((MINTED_NOT_STARTED_HALT, "minted_not_started"))):
        return False
    try:
        stage = json.loads((run_dir / "run-stage.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return (
        isinstance(stage, dict)
        and "staged_at" not in stage
        and stage.get("stage") in (None, "minted", "halted")
    )


def halted_run_named(repo_root: Path, run_id: object, sizing_rel: str) -> Optional[str]:
    """`run_id` when it names a `warp-*` or `ask-*` run whose marker is still `halted`, else None.

    A refire carries the run id its halt recorded. A marker bound to another sizing path (the sizing moved or
    was mutated since the halt) is rebound to `sizing_rel` here, so the sizing-keyed lookups downstream resolve
    the same run. One marker read; a run that is not halted answers None and the sizing lookup stands."""
    if not isinstance(run_id, str) or not run_id.startswith(("warp-", "ask-")) or not safe_id(run_id):
        return None
    rd = Path(repo_root) / RECORD_DIR_ROOT / run_id
    marker = _read(rd)
    if marker is not None and marker.get("status") == STATUS_RESUMED:
        # An explicit name is the caller's claim the run is theirs to resume; a resumer that is gone holds nothing.
        if rehalt_if_resumer_dead(repo_root, run_id, "resumer fire ended with no terminal marker"):
            marker = _read(rd)
    if marker is None or marker.get("status") != STATUS_HALTED:
        return None
    norm = _norm(sizing_rel)
    old = marker.get("sizing_rel")
    if old != norm:
        _write(rd, {**marker, "sizing_rel": norm})
        _index_call("add_run", rd, halted_withheld(rd), sizing_rel=norm)
        if isinstance(old, str) and old:
            try:
                (Path(repo_root) / LOOKUP_INDEX_DIR / _lookup_key(old) / run_id).unlink(missing_ok=True)
            except OSError:
                pass
            _unbind(Path(repo_root), rd, old)
    return run_id


class AmbiguousHaltedRuns(ValueError):
    """Several halted runs of one sizing and none is the caller's: `candidates` names every one."""

    caller_facing_validation = True  # ipc renders the message as INVALID_PARAMS, not a bare Internal error

    def __init__(self, sizing_rel: str, candidates) -> None:
        self.candidates = tuple(candidates)
        self.reason = f"ambiguous-halted-runs:{','.join(self.candidates)}"
        super().__init__(
            f"{len(self.candidates)} halted runs of {sizing_rel}, none owned by this session: "
            f"{', '.join(self.candidates)}. Instead: pass `run_id` naming the one to resume."
        )


def _run_stamp(run_id: str) -> str:
    """The `<%Y%m%dT%H%M%S>-<hex6>` tail of a `warp-` or `ask-` run id (comparable across both prefixes)."""
    return run_id[-22:]


def _halted_candidates(repo_root: Path, sizing_rel: str) -> list:
    """Every halted run a refire of `sizing_rel` could resume: the newest `warp-<stem>-*` halt plus each
    unsuperseded halted `ask-*` run. One warp listing and the sizing's indexed ask markers, no scan."""
    warp = halted_run_for(repo_root, sizing_rel)
    return ([warp] if warp else []) + _halted_asks(repo_root, sizing_rel)


def _minted_iso(run_id: str) -> str:
    """A run id's mint stamp as `%Y-%m-%dT%H:%M:%SZ`, comparable with `revised_at`."""
    s = _run_stamp(run_id)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}Z"


def _superseded_revise(root: Path, best: str, best_at: str, ordered: list) -> bool:
    """True when another candidate was minted after `best`'s revise stamp (so it halted after it) or carries
    a later note: the revise then no longer outranks the newest halt. Mint time is the proxy for halt time;
    a run minted before the revise but halted after it is not detected."""
    return any(
        name != best and (_minted_iso(name) > best_at or _latest_note_at(root / name) > best_at)
        for name in ordered
    )


def _prefer(repo_root: Path, sizing_rel: str, candidates: list, caller_session: Optional[str]) -> str:
    """The candidate to resume among two or more: the caller's own newest run; else the latest `revised_at`
    stamp; else the newest. `AmbiguousHaltedRuns` when the caller owns none, no stamp breaks the tie and the
    candidates' emission receipts name different sessions. Receipt reads only, no spawn."""
    ordered = sorted(candidates, key=_run_stamp, reverse=True)
    owners = {name: recorded_emitter(repo_root, name) for name in ordered}
    if caller_session:
        for name in ordered:
            if owners[name] == caller_session:
                return name
    root = Path(repo_root) / RECORD_DIR_ROOT
    stamps = [(at, name) for name in ordered if (at := revised_halt(root / name))]
    if stamps:
        best_at, best = max(stamps)
        if not _superseded_revise(root, best, best_at, ordered):
            return best
    if caller_session and len({o for o in owners.values() if o}) > 1:
        raise AmbiguousHaltedRuns(_norm(sizing_rel), ordered)
    return ordered[0]


def preferred_halted_run(repo_root: Path, sizing_rel: str, *, caller_session: Optional[str] = None) -> Optional[str]:
    """`_prefer`'s answer when the sizing has two or more halted candidates, else None (the legacy
    single-halt lookups stand). For callers that resolve a warp halt and an ask halt separately."""
    candidates = _halted_candidates(repo_root, sizing_rel)
    return _prefer(repo_root, sizing_rel, candidates, caller_session) if len(candidates) > 1 else None


def halted_run_to_resume(
    repo_root: Path, sizing_rel: str, *, build: bool = True, run_id: object = None, caller_session: Optional[str] = None
) -> Optional[str]:
    """The halted run a refire of `sizing_rel` resumes, a `warp-<stem>-*` run or else an `ask-<stamp>`
    one. The submit hook and the emit body both resolve through here so their lookups cannot drift.
    With several halted runs the caller's own newest wins (`_prefer`); a `caller_session` that owns none
    and cannot be told from the rest refuses with `AmbiguousHaltedRuns`.
    A refire's `run_id` (`halted_run_named`) is tried first, so a sizing path change cannot lose the run.
    Reads only this sizing's own runs: the `warp-<stem>-*` listing and the incrementally written index entries,
    never every record dir. `build` is accepted for the hook's call and ignored: no index build exists.
    When nothing is halted, a `superseded-by-refire` close whose successor never launched is voided first
    (`void_unstarted_supersede`), so a plain refire after a failed launch resumes the prior run."""
    found = _halted_run_to_resume(repo_root, sizing_rel, run_id=run_id, caller_session=caller_session)
    if found is None and void_unstarted_supersede(repo_root, sizing_rel, run_id) is not None:
        found = _halted_run_to_resume(repo_root, sizing_rel, run_id=run_id, caller_session=caller_session)
    return found


def _halted_run_to_resume(
    repo_root: Path, sizing_rel: str, *, run_id: object = None, caller_session: Optional[str] = None
) -> Optional[str]:
    """`halted_run_to_resume` without the void of an unstarted refire's supersede."""
    if run_id is not None:
        named = halted_run_named(repo_root, run_id, sizing_rel)
        if named is not None:
            return _successor_or_self(repo_root, sizing_rel, named)
    runs = _indexed_runs(repo_root, _norm(sizing_rel))
    candidates = _halted_candidates(repo_root, sizing_rel)
    if len(candidates) > 1:
        return _successor_or_self(repo_root, sizing_rel, _prefer(repo_root, sizing_rel, candidates, caller_session))
    picked = candidates[0] if candidates else None
    return _successor_or_self(repo_root, sizing_rel, _latest_revised(repo_root, sizing_rel, runs, picked)) if picked else picked


class SupersededHaltedRun(AmbiguousHaltedRuns):
    """The halted run a refire picked has a newer successor (`resumed_from` chain) that is not itself halted."""

    def __init__(self, sizing_rel: str, picked: str, successor: str) -> None:
        self.candidates = (picked, successor)
        self.reason = f"superseded-halted-run:{picked},{successor}"
        ValueError.__init__(
            self,
            f"{picked} of {sizing_rel} was resumed by {successor}, which is not halted. "
            f"Instead: pass `run_id` naming the run to resume, or refire after {successor} halts.",
        )


def _resumes(root: Path, name: str, target: str) -> bool:
    """True when `name`'s run.json `resumed_from` chain reaches `target` (cycle-safe, `CARRIED_HOP_CAP` hops)."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    seen = {name}
    cur = name
    for _ in range(rl.CARRIED_HOP_CAP):
        prev = (rl.read_run(root / cur) or {}).get(rl.RESUMED_FROM_FIELD)
        if not (isinstance(prev, str) and safe_id(prev)) or prev in seen:
            return False
        if prev == target:
            return True
        seen.add(prev)
        cur = prev
    return False


def _successor_or_self(repo_root: Path, sizing_rel: str, picked: str) -> str:
    """`picked`, unless a newer run of the sizing resumed it (its `resumed_from` chain reaches `picked`): then
    the newest such successor when its marker is `halted`, else `SupersededHaltedRun` naming both.
    Successors are the `warp-<stem>-*` dirs plus the sizing's indexed `ask-*` runs. Trap: an `ask-*` successor
    that is landed, closed or superseded left the index and is not seen.
    A minted-only shell successor holds no work and is passed over. Reads only the sizing's own run dirs."""
    norm = _norm(sizing_rel)
    prefix = f"warp-{PurePosixPath(norm).stem}-"
    root = Path(repo_root) / RECORD_DIR_ROOT
    floor = _run_stamp(picked)
    names = [prefix + s for s in _warp_stamps(root, prefix)]
    names += [n for n in _indexed_runs(repo_root, norm) if n.startswith("ask-") and _STAMP.fullmatch(n[4:])]
    for name in sorted((n for n in names if _run_stamp(n) > floor), key=_run_stamp, reverse=True):
        if not safe_id(name) or not _resumes(root, name, picked) or _is_shell(root / name):
            continue
        marker = _read(root / name)
        if marker is not None and marker.get("status") == STATUS_HALTED:
            return name
        if _retired_for(marker, picked):
            continue
        raise SupersededHaltedRun(_norm(sizing_rel), picked, name)
    return picked


def _retired_for(marker: Optional[dict], run_id: str) -> bool:
    """True when `marker` retires a never-launched shell in favour of `run_id` (`void_unstarted_supersede`): that
    shell holds no work and is no successor of the run it was minted to replace."""
    evidence = _superseded_evidence(marker, SUPERSEDED_BY_SUCCESSOR)
    return evidence is not None and evidence.get("successor") == run_id


def _superseded_evidence(marker: Optional[dict], kind: str) -> Optional[dict]:
    """The evidence dict of a `superseded` marker when its `kind` matches, else None."""
    if not marker or marker.get("status") != STATUS_SUPERSEDED:
        return None
    evidence = marker.get(SUPERSEDED_EVIDENCE_FIELD)
    return evidence if isinstance(evidence, dict) and evidence.get("kind") == kind else None


def _latest_revised(repo_root: Path, sizing_rel: str, runs: set, picked: str) -> str:
    """Among the sizing's indexed `halted` runs, the one the latest `workflow.revise` stamped (`revised_at`), else
    `picked`. A refire resumes the run whose notes.jsonl holds the ruling, never a sibling halt that lacks it.
    Only runs other than `picked` are read, so a sizing with one halt costs no extra marker read."""
    root = Path(repo_root) / RECORD_DIR_ROOT
    best_at, best = "", None
    for name in sorted(runs):
        if name == picked or not safe_id(name):
            continue
        at = revised_halt(root / name)
        if at and at >= best_at:
            best_at, best = at, name
    if best is None or best_at <= (revised_halt(root / picked) or ""):
        return picked
    # Trap: a later ruling posted on another run of this sizing (`notes.jsonl`) supersedes `best`'s;
    # without this the OLDEST halt of a lineage won (example-game-repo 06ca65: cc36f8 over 0845c4).
    if _superseded_revise(root, best, best_at, [n for n in runs if safe_id(n)]):
        return picked
    return halted_run_named(repo_root, best, sizing_rel) or picked


def halted_run_for(repo_root: Path, sizing_rel: str) -> Optional[str]:
    """Return the run id a refire of `sizing_rel` resumes, else None.

    Newest `warp-<stem>-*` dir first (run ids embed %Y%m%dT%H%M%S); a minted-only shell is skipped,
    so a newer unmarked run that got past `minted` still means the halted one was superseded. A
    `resumed` marker whose every newer dir is such a shell (the resuming run died before staging,
    e.g. its halt commit was denied) is rewritten `halted` and returned.
    """
    norm = _norm(sizing_rel)
    prefix = f"warp-{PurePosixPath(norm).stem}-"
    root = Path(repo_root) / RECORD_DIR_ROOT
    names = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith(prefix) and _STAMP.fullmatch(entry.name[len(prefix):]) and entry.is_dir():
                    names.append(entry.name)
    except OSError:
        return None
    skipped = 0
    for name in sorted(names, reverse=True):
        if not safe_id(name):
            return None
        if _is_shell(root / name):
            skipped += 1
            continue
        marker = _read(root / name)
        bound = marker.get("sizing_rel") if marker else None
        if not isinstance(bound, str) or PurePosixPath(_norm(bound)).stem != PurePosixPath(norm).stem:
            return None
        status = marker.get("status")
        if bound != norm and status in (STATUS_HALTED, STATUS_RESUMED):
            marker = {**marker, "sizing_rel": norm}
            _write(root / name, marker)
            _index_call("add_run", root / name, halted_withheld(root / name), sizing_rel=norm)
        if status == STATUS_HALTED:
            return name
        if status == STATUS_RESUMED and skipped:
            _write(root / name, {**marker, "status": STATUS_HALTED})
            _index_call("add_run", root / name, halted_withheld(root / name), sizing_rel=norm)
            return name
        return None
    return None


#: The `rebind_blocker` reason for a recorded emitter that is no longer a live session.
EMITTER_GONE = "emitter-gone"


def recorded_emitter(repo_root: Path, run_id: str) -> Optional[str]:
    """The session id the halted run's emission receipt names, else None (no script, no receipt,
    unreadable, or no session recorded)."""
    from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path

    script = Path(repo_root) / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    try:
        receipt = json.loads(emission_receipt_path(script).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recorded = receipt.get("session_id") if isinstance(receipt, dict) else None
    return recorded if recorded and isinstance(recorded, str) else None


def rebind_blocker(repo_root: Path, run_id: str, session_id: str) -> Optional[str]:
    """Return a reason `session_id` must not take the halted run's script path, else None.

    None when the run has no script or receipt, the receipt names `session_id` or no session, or
    the recorded session is live. `EMITTER_GONE` when the recorded session is not live. One
    receipt read, one `session_live` call, no spawn.
    """
    from coordinator_core.session.liveness import session_live

    recorded = recorded_emitter(repo_root, run_id)
    if recorded is None or recorded == session_id:
        return None
    return None if session_live(recorded) else EMITTER_GONE


def _index_call(fn: str, record_dir: Path, *args, **kwargs) -> None:
    """Mirror a marker transition into the halted-withheld index of the checkout holding `record_dir`
    (`<root>/<RECORD_DIR_ROOT>/<run>`). Best-effort: the marker write is the truth, so an index
    failure never fails the transition. Imported lazily: the index module imports this one."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    rd = Path(record_dir)
    target = {"add_run": idx.add_run, "remove_run": idx.remove_run}[fn]
    try:
        target(rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)], rd.name, *args, **kwargs)
    except (OSError, ValueError, IndexError):
        pass


def _live_index_drop(record_dir: Path) -> None:
    """Drop the run's row from the live-run index of the checkout holding `record_dir`, so a run whose
    marker left `halted` stops refusing overlapping asks. Never raises (`index_remove` swallows)."""
    from coordinator_core.ops.dispatch_emit import live_run_writes

    rd = Path(record_dir)
    try:
        root = rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
    except IndexError:
        return
    live_run_writes.index_remove(root, rd.name)


def _withheld_prints(record_dir: Path, withheld: dict) -> dict:
    """Blob id of each withheld path as it stands in the checkout holding `record_dir`; a path that
    is absent or unreadable is left out. The same fingerprint `_record_withheld_prints` records."""
    from coordinator_core.ops.dispatch_emit.kept_work import fingerprint

    rd = Path(record_dir)
    try:
        root = rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
    except IndexError:
        return {}
    return {p: f for p in sorted(withheld) if isinstance(p, str) and (f := fingerprint(root, p)) is not None}


def record_withheld(record_dir: Path, withheld: dict) -> bool:
    """Stamp ``run_artifacts.withheld`` (``{path: reason}``) on a ``halted`` marker so a refire can
    tell product the halt left dirty on purpose from product it never touched; False and no write
    when nothing was withheld or the marker is absent or not halted."""
    marker = _read(Path(record_dir))
    if not withheld or marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, HALT_RUN_ARTIFACTS_FIELD: {"withheld": dict(sorted(withheld.items()))}})
    _index_call(
        "add_run", record_dir, withheld,
        sizing_rel=marker.get("sizing_rel") or "", prints=_withheld_prints(record_dir, withheld),
    )
    return True


def record_resume_stage(record_dir: Path, stage: dict) -> bool:
    """Stamp ``next_action.resume.stage`` (the kept.json pointer) on a ``halted`` marker whose
    ``next_action`` is a refire envelope; False and no write otherwise."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    action = marker.get(HALT_NEXT_ACTION_FIELD)
    if not is_refire_next_action(action):
        return False
    stamped = {**action, "resume": {**action["resume"], "stage": dict(stage)}}
    _write(Path(record_dir), {**marker, HALT_NEXT_ACTION_FIELD: stamped})
    return True


def halted_withheld(record_dir: Path) -> dict:
    """``{path: reason}`` the halt recorded as withheld; empty when absent or malformed."""
    return _withheld_of(_read(Path(record_dir)))


def _withheld_of(marker: Optional[dict]) -> dict:
    arts = marker.get(HALT_RUN_ARTIFACTS_FIELD) if marker else None
    raw = arts.get("withheld") if isinstance(arts, dict) else None
    return {k: v for k, v in raw.items() if isinstance(k, str)} if isinstance(raw, dict) else {}


def fresh_index_entry(record_dir: Path) -> "Optional[halted_withheld_index.RunEntry]":
    """The `halted_withheld_index` entry of the run owning `record_dir` (`<root>/<RECORD_DIR_ROOT>/<run>`)
    while it is fresh against the marker (`halted_withheld_index.fresh_entry`), else None: the caller
    opens `halted.json`. None for a dir outside that layout."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    root = _root_of(record_dir)
    if root is None:
        return None
    return idx.fresh_entry(root, os.path.basename(os.fspath(record_dir).rstrip("\\/")))


def halted_withheld_strict(record_dir: Path) -> dict:
    """`halted_withheld` over the strict read: empty when the marker is absent, `UnreadableHaltRecord` when corrupt.
    A run with a fresh `halted_withheld_index` entry (`fresh_index_entry`) is answered without opening its
    marker: `{path: ""}`. Trap: the reasons are empty on that path; a caller needing them reads the marker."""
    entry = fresh_index_entry(record_dir)
    if entry is not None:
        return {p: "" for p in entry.paths}
    return _withheld_of(read_strict_run(record_dir))


def mark_revised(record_dir: Path, at: str) -> bool:
    """Stamp `revised_at` on a `halted` or `resumed` marker; False and no write when it is absent or neither."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") not in (STATUS_HALTED, STATUS_RESUMED):
        return False
    _write(Path(record_dir), {**marker, REVISED_AT_FIELD: at})
    return True


def revised_halt(record_dir: Path) -> Optional[str]:
    """The `revised_at` value of a `halted` marker, or of a `resumed` one whose stamp the resume never consumed
    (a revision no replayed stage reads), else None. One marker read."""
    marker = _read(Path(record_dir))
    if marker is None:
        return None
    status = marker.get("status")
    if status not in (STATUS_HALTED, STATUS_RESUMED):
        return None
    value = marker.get(REVISED_AT_FIELD)
    if not isinstance(value, str) or not value:
        return None
    if status == STATUS_RESUMED and (REVISED_CONSUMED_FIELD not in marker or marker[REVISED_CONSUMED_FIELD] == value):
        return None
    return value


def unread_revision_ids(record_dir: Path) -> list:
    """Ids of the run's notes stamped after the revision the last resume consumed, oldest first; [] when none."""
    marker = _read(Path(record_dir)) or {}
    consumed = marker.get(REVISED_CONSUMED_FIELD)
    since = consumed if isinstance(consumed, str) else ""
    from coordinator_core.ops.dispatch_emit import run_notes

    return [e["id"] for e in run_notes.live(run_notes.entries(Path(record_dir) / "notes.jsonl", "")) if e["at"] > since]


def record_released(record_dir: Path, sha: str, reason: str) -> bool:
    """Stamp ``released: {reason, sha}`` on a ``halted`` marker, leaving it ``halted`` and resumable;
    False and no write when the marker is absent or not halted."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, HALT_RELEASED_FIELD: {"reason": reason, "sha": sha}})
    return True


def mark_resumed(record_dir: Path) -> bool:
    """Rewrite a `halted` marker as `resumed`; False when no halted marker is present."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, "status": STATUS_RESUMED, REVISED_CONSUMED_FIELD: marker.get(REVISED_AT_FIELD) or ""})
    _index_call("remove_run", record_dir)
    _live_index_drop(record_dir)
    return True


STALL_CLEARED_FIELD = "stall_cleared"


def mark_stall_cleared(record_dir: Path, evidence: dict) -> bool:
    """Rewrite a `halted` / `staged_stall` marker as `resumed` carrying `evidence` under `STALL_CLEARED_FIELD`
    and return the stage to `staged`; False and no write for any other marker. The run never stopped, so
    unlike `mark_resumed` its live-run row is kept."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED or marker.get("halted") != "staged_stall":
        return False
    _write(Path(record_dir), {**marker, "status": STATUS_RESUMED, STALL_CLEARED_FIELD: evidence})
    _index_call("remove_run", record_dir)
    stamp_run_stage_at(Path(record_dir), "staged")
    return True


def mark_retracted(record_dir: Path, reason: str) -> bool:
    """Rewrite a `halted` marker as `resumed` carrying `retracted_at` / `retracted_reason`, and re-stamp the
    run `staged`; False when no halted marker is present.

    Unlike `mark_resumed` the live-run index row stays: the run's driver is live, so it must keep refusing
    overlapping asks and stay in `staged_stall.scan`'s walk. The `staged` stamp is what that scan reads (a halt
    stamps the run `halted`)."""
    from datetime import datetime, timezone

    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {
        **marker, "status": STATUS_RESUMED, REVISED_CONSUMED_FIELD: marker.get(REVISED_AT_FIELD) or "",
        RETRACTED_AT_FIELD: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), RETRACTED_REASON_FIELD: reason,
    })
    stamp_run_stage_at(Path(record_dir), "staged")
    _index_call("remove_run", record_dir)
    return True


HALT_FIRE_ENDED = "fire_ended"
_MAX_FIRE_RECORDS = 64


def fire_live(repo_root: Path, run_id: str) -> bool:
    """True when a headless fire record naming `run_id` is pending or running with a live driver pid.

    One registry listing, at most `_MAX_FIRE_RECORDS` small reads; a registry that cannot be read counts as
    live (unknown is never a verdict that strands a run). Never spawns."""
    from coordinator_core.session_hub_path import hub_for_common

    try:
        registry = hub_for_common(Path(repo_root) / ".git") / "workflow-fires"
        with os.scandir(registry) as it:
            entries = sorted(
                ((e.stat().st_mtime, e.path) for e in it if e.name.endswith(".json") and e.is_file()), reverse=True
            )
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for _, path in entries[:_MAX_FIRE_RECORDS]:
        try:
            record = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("run_id") != run_id:
            continue
        if record.get("state") not in ("pending", "running"):
            continue
        from coordinator_core.ops.workflow_fire.fire import _pid_alive

        if record.get("state") == "pending" or _pid_alive(record.get("pid")):
            return True
    return False


def rehalt_if_resumer_dead(repo_root: Path, run_id: str, reason: str) -> bool:
    """Rewrite a `resumed` marker as `halted` (`fire_ended`) when no live fire executes the run; False and no
    write otherwise.

    `resumed` means a resumer owns the run. When that fire ended without writing a terminal marker (a
    timeout kill, a crashed runtime) nothing else flips it, and every halted-only lookup then misses the run
    and mints a fresh one over its kept work. Called by the headless executor when its fire fails, and by
    `halted_run_named` for a run whose fire died before this writer existed."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        return False
    rd = Path(repo_root) / RECORD_DIR_ROOT / run_id
    marker = _read(rd)
    if marker is None or marker.get("status") != STATUS_RESUMED or fire_live(repo_root, run_id):
        return False
    sizing_rel = marker.get("sizing_rel") if isinstance(marker.get("sizing_rel"), str) else None
    _write(rd, {**marker, "status": STATUS_HALTED, "halted": HALT_FIRE_ENDED, HALT_REASON_FIELD: reason[:500]})
    _index_call("add_run", rd, halted_withheld(rd), sizing_rel=sizing_rel)
    stamp_run_stage_at(rd, "halted")
    if sizing_rel:
        _schedule_resume_build(rd)
    return True


def mark_superseded(record_dir: Path, expected: dict, evidence: dict, dispositions: Optional[dict] = None) -> bool:
    """Rewrite the marker as `superseded` with `evidence` only if it still equals `expected` (the marker the
    caller verified against); False and no write when it is absent, changed, or no longer `halted`.

    `dispositions` (path -> DISPOSITION_*), when given, is also recorded under PATH_DISPOSITIONS_FIELD."""
    marker = _read(Path(record_dir))
    if marker is None or marker != expected or marker.get("status") != STATUS_HALTED:
        return False
    updated = {**marker, "status": STATUS_SUPERSEDED, SUPERSEDED_EVIDENCE_FIELD: evidence}
    if dispositions is not None:
        updated[PATH_DISPOSITIONS_FIELD] = dispositions
    _write(Path(record_dir), updated)
    _live_index_drop(record_dir)
    _record_judge(record_dir)
    return True


def mark_landed(record_dir: Path, sha: str, *, without_verdict: bool = False) -> bool:
    """Rewrite a `halted` or `resumed` marker as `landed` at `sha`; False and no write for any other status.

    A `landed` marker is skipped by `halted_run_for` / `halted_ask_run_for`, so a refire no longer resumes it.
    `without_verdict` stamps `landed_without_verdict: true`; the default writes no such key.
    """
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") not in (STATUS_HALTED, STATUS_RESUMED):
        return False
    extra = {"landed_without_verdict": True} if without_verdict else {}
    _write(Path(record_dir), {**marker, "status": STATUS_LANDED, "sha": sha, **extra})
    _index_call("remove_run", record_dir)
    _live_index_drop(record_dir)
    stamp_run_stage_at(Path(record_dir), "landed")
    return True


#: `halted` kind of a run that ran and ended with no halt record; mirrors `commit_stage_halt.HALT_UNRECORDED_END`.
UNRECORDED_END_HALT = "run-ended-unrecorded"
#: `zero_run_cause` a test-leg verdict reports -> the `halted` kind the run's script halts with; mirrors `ask_compose`'s co-tenant halts.
CO_TENANT_HALT_BY_CAUSE = {
    "co_tenant_dependency_on_halted_run": "co_tenant_dependency_on_halted_run",
    "co-tenant-wait": "co_tenant_wait",
    "co_tenant_cycle": "co_tenant_cycle",
}
_CAUSE_EVIDENCE_FILES = ("overlay-verdict.json", "test-leg.json")


def _unrecorded_halt_kind(record_dir: Path) -> tuple[str, str]:
    """(halted kind, reason) for a ran run with no marker: a co-tenant `zero_run_cause` in the run's test-leg
    verdicts names that halt, else `UNRECORDED_END_HALT`."""
    for name in _CAUSE_EVIDENCE_FILES:
        try:
            doc = json.loads((record_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        kind = CO_TENANT_HALT_BY_CAUSE.get(doc.get("zero_run_cause")) if isinstance(doc, dict) else None
        if kind:
            return kind, f"the test leg ran no tests: {doc['zero_run_cause']}; the run ended with no halt record"
    return UNRECORDED_END_HALT, "run ended with no halt record (a halt whose commit stage could not record it, e.g. no run token)"


def unrecorded_halt(repo_root: Path, run_id: str, record_dir: Path) -> Optional[dict]:
    """The halt a run that ran but ended with no marker would be recorded under: `{halted, reason, sizing_rel}`, else None. Writes nothing.

    A run ran when its `run-stage.json` left `minted`; one at `minted`, `landed` or with no stage file never
    ran or already closed, and a present marker is never overwritten.
    """
    record_dir = Path(record_dir)
    if (record_dir / HALTED_FILE).exists():
        return None
    try:
        stage = json.loads((record_dir / "run-stage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stage, dict) or stage.get("stage") in (None, "minted", "landed"):
        return None
    kind, reason = _unrecorded_halt_kind(record_dir)
    return {"halted": kind, "reason": reason, "sizing_rel": bound_sizing_rel(repo_root, run_id)}


def record_unrecorded_halt(record_dir: Path, halt: dict) -> bool:
    """Write the `halted` marker for an `unrecorded_halt` result; False when the write failed. `_write` alone: no resume build."""
    try:
        _write(Path(record_dir), {"status": STATUS_HALTED, "halted": halt["halted"], "sizing_rel": halt["sizing_rel"], "reason": halt["reason"]})
    except OSError:
        return False
    return True


def reopen_red(record_dir: Path, sha: str, reds: list) -> Path:
    """Reopen a run whose post-land tripwire re-run went red: a `landed` or absent marker becomes `halted` /
    `HALT_POST_LAND_TRIPWIRE_RED` at `sha` carrying `reds`; any other status is kept and only gains `reds`.

    Returns the marker path; never raises (a failed write leaves the marker as it was). A reopened marker with a
    `sizing_rel` re-enters the lookup and halted-withheld indexes, so the owner's next fire of that sizing resumes it.
    Not `write_halt_marker`: that schedules a resume build.
    """
    rd = Path(record_dir)
    target = rd / HALTED_FILE
    try:
        marker = _read(rd) or {}
        if marker.get("status") in (None, STATUS_LANDED):
            payload = {**marker, "status": STATUS_HALTED, "halted": HALT_POST_LAND_TRIPWIRE_RED, "sha": sha, "reds": list(reds)}
        else:
            payload = {**marker, "reds": list(reds)}
        _write(rd, payload)
        if payload["status"] == STATUS_HALTED:
            sizing = payload.get("sizing_rel")
            if isinstance(sizing, str) and sizing:
                _index_call("add_run", rd, halted_withheld(rd), sizing_rel=sizing)
            stamp_run_stage_at(rd, "halted")
    except Exception:  # noqa: BLE001 -- the caller is a detached post-step with no one to catch
        pass
    return target


def mark_closed(record_dir: Path, reason: str, at: str) -> bool:
    """Rewrite a `halted` or `resumed` marker as `closed` with `{reason, at}`: a terminal end for a
    halt that will never land (nothing withheld, owner gone). False and no write for any other status."""
    marker = _read(Path(record_dir))
    if not reason or marker is None or marker.get("status") not in (STATUS_HALTED, STATUS_RESUMED):
        return False
    _write(Path(record_dir), {**marker, "status": STATUS_CLOSED, "closed": {"reason": reason, "at": at}})
    _index_call("remove_run", record_dir)
    _live_index_drop(record_dir)
    _record_judge(record_dir)
    return True


def ran_under_engine(record_dir: Path) -> Optional[str]:
    """The engine sha the halt marker stamped (40-hex), else None (absent, unreadable, unstamped).

    Trap: never falls back to `resume.json`; every recompose restamps its `engine_sha` with the current
    engine, so a run resumed after a swap would read as current."""
    marker = _read(Path(record_dir))
    value = marker.get(HALT_ENGINE_FIELD) if marker else None
    return value if isinstance(value, str) and _SHA40.fullmatch(value) else None


def mark_superseded_by_refire(
    record_dir: Path, *, prior_engine: Optional[str], engine: Optional[str], at: str, successor: Optional[str] = None,
) -> bool:
    """Rewrite a `halted` or `resumed` marker as `superseded` with evidence kind `superseded-by-refire`: a
    fresh refire closes the prior run and credits none of its kept chunks. False and no write for any other
    status (landed, closed, superseded) or an absent marker.

    The evidence records `prior_status` (the status overwritten) and, when known, the `successor` run id; a
    caller that mints the successor after this call stamps it with `stamp_refire_successor`. The pair lets
    `void_unstarted_supersede` undo the close when the successor's script never launched."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") not in (STATUS_HALTED, STATUS_RESUMED):
        return False
    evidence = {
        "kind": SUPERSEDED_BY_REFIRE, "prior_engine": prior_engine, "engine": engine, "at": at, "kept_credit": False,
        "prior_status": marker["status"],
    }
    if successor:
        evidence["successor"] = successor
    _write(Path(record_dir), {**marker, "status": STATUS_SUPERSEDED, SUPERSEDED_EVIDENCE_FIELD: evidence})
    _index_call("remove_run", record_dir)
    _live_index_drop(record_dir)
    _record_judge(record_dir)
    return True


def _refire_evidence(marker: Optional[dict]) -> Optional[dict]:
    """The `superseded-by-refire` evidence of a `superseded` marker, else None."""
    return _superseded_evidence(marker, SUPERSEDED_BY_REFIRE)


def stamp_refire_successor(record_dir: Path, successor: str) -> bool:
    """Name `successor` in a `superseded-by-refire` marker that has none yet; False and no write for any other
    marker, an unsafe id, or a marker already naming a successor."""
    marker = _read(Path(record_dir))
    evidence = _refire_evidence(marker)
    if evidence is None or evidence.get("successor") or not isinstance(successor, str) or not safe_id(successor):
        return False
    _write(Path(record_dir), {**marker, SUPERSEDED_EVIDENCE_FIELD: {**evidence, "successor": successor}})
    return True


VOIDED_SUPERSEDE_FIELD = "voided_supersede"
_VOID_SCAN_CAP = 3


def _void_refire_supersede(root: Path, name: str, bound: dict) -> bool:
    """Restore run `name` when its `superseded-by-refire` close names a successor whose script never launched.

    The marker returns to `halted` (a `resumed` prior returns to `halted` too: the resumer it had was closed
    by the refire), the evidence moves under `VOIDED_SUPERSEDE_FIELD`, and the never-started successor is
    retired so it is not read as the run's successor. Skipped when a halted or resumed run other than the
    successor is newer than `name`: that run, not this one, is the refire's target."""
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import successor_never_started

    rd = root / RECORD_DIR_ROOT / name
    marker = _read(rd)
    evidence = _refire_evidence(marker)
    successor = evidence.get("successor") if evidence else None
    if (
        evidence is None or not isinstance(successor, str) or not safe_id(successor) or successor == name
        or evidence.get("prior_status") not in (STATUS_HALTED, STATUS_RESUMED)
        or not isinstance(marker.get("sizing_rel"), str)
    ):
        return False
    stamp = _run_stamp(name)
    if any(
        other != successor and _run_stamp(other) > stamp and (halt or {}).get("status") in (STATUS_HALTED, STATUS_RESUMED)
        for other, halt in bound.items()
    ):
        return False
    if not successor_never_started(root, successor):
        return False
    try:
        from coordinator_core.ops.dispatch_emit import run_token

        run_token.consume(root, successor)  # the never-launched successor's token would read it live
    except (OSError, ValueError):
        return False  # a live token keeps the successor reading live: leave the close in place
    restored = {k: v for k, v in marker.items() if k != SUPERSEDED_EVIDENCE_FIELD}
    restored.update({"status": STATUS_HALTED, VOIDED_SUPERSEDE_FIELD: evidence})
    _write(rd, restored)
    _index_call("add_run", rd, halted_withheld(rd), sizing_rel=marker["sizing_rel"])
    from datetime import datetime, timezone

    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir as run_record_dir

    srd = run_record_dir(root, successor)  # a minted run's record can sit under the scratch tree
    if not mark_shell_superseded(srd, successor=name, sizing_rel=marker["sizing_rel"], at=at):
        held = _read(srd)
        if held is not None and held.get("status") == STATUS_HALTED:  # a minted-not-started halt
            mark_superseded(
                srd, held, {"kind": SUPERSEDED_BY_SUCCESSOR, "successor": name, "at": at, "kept_credit": False},
            )
    return True


def void_unstarted_supersede(repo_root: Path, sizing_rel: str, run_id: object = None) -> Optional[str]:
    """Undo the newest `superseded-by-refire` close of `sizing_rel` whose successor never launched; the run id
    restored to `halted`, else None. `run_id`, when it names a run, is tried first. Reads the sizing's bound-run
    listing and at most `_VOID_SCAN_CAP` superseded markers; no git, no subprocess."""
    root = Path(repo_root)
    names = []
    if isinstance(run_id, str) and run_id.startswith(("warp-", "ask-")) and safe_id(run_id):
        names.append(run_id)
    if not names and not _bound_dir(root, _norm(str(sizing_rel))).is_dir():
        # Trap: bound_runs back-fills by scanning every record dir; a superseded close is always written through
        # _write, which fills the bound dir, so its absence means there is nothing to void and the scan is waste.
        return None
    bound = bound_runs(root, sizing_rel)
    superseded = sorted(
        (n for n, halt in bound.items() if n not in names and (halt or {}).get("status") == STATUS_SUPERSEDED),
        key=_run_stamp, reverse=True,
    )
    for name in names + superseded[:_VOID_SCAN_CAP]:
        if _void_refire_supersede(root, name, bound):
            return name
    return None


SUPERSEDED_BY_SUCCESSOR = "superseded-by-successor"


def mark_shell_superseded(record_dir: Path, *, successor: str, sizing_rel: str, at: str) -> bool:
    """Retire a minted-only shell whose sizing a `successor` run id took over: write a `superseded` marker
    (evidence kind `superseded-by-successor`, `superseded_by` the successor) and stamp the run-stage `halted`,
    so the minted reaper (`_stale_at_minted`) never halts it abandoned. False and no write when a marker
    already exists or the run-stage record reads past `minted` (a shell only)."""
    rd = Path(record_dir)
    if (rd / HALTED_FILE).exists() or not safe_id(successor):
        return False
    try:
        stage = json.loads((rd / "run-stage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(stage, dict) or stage.get("stage") not in (None, "minted"):
        return False
    _write(rd, {
        "status": STATUS_SUPERSEDED,
        "halted": rd.name,
        "sizing_rel": _norm(sizing_rel),
        SUPERSEDED_EVIDENCE_FIELD: {"kind": SUPERSEDED_BY_SUCCESSOR, "successor": successor, "at": at, "kept_credit": False},
        PATH_DISPOSITIONS_FIELD: {},
        "superseded_by": successor,
    })
    _live_index_drop(rd)
    stamp_run_stage_at(rd, "halted")
    _record_judge(rd)
    return True


def _record_judge(record_dir: Path) -> None:
    """Record the engine's not_met verdict for a run closed or superseded without one; best-effort."""
    try:
        record_terminal_judge_at(Path(record_dir))
    except (ValueError, OSError):
        pass


def mark_predecessors_landed(repo_root: Path, run_id: str, sha: str) -> list:
    """Stamp each `resumed` predecessor of `run_id` (its run.json `resumed_from` chain) landed at `sha`.

    Returns the run ids stamped. A predecessor not `resumed` (already landed, or halted again) is left
    alone; the walk is cycle-safe and capped at `CARRIED_HOP_CAP` hops. Never raises on a bad link.
    """
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    root = Path(repo_root)
    stamped: list = []
    visited = {run_id}
    cur = run_id
    for _ in range(rl.CARRIED_HOP_CAP):
        try:
            prev = (rl.read_run(record_dir(root, cur)) or {}).get(rl.RESUMED_FROM_FIELD)
            if not (isinstance(prev, str) and safe_id(prev)) or prev in visited:
                break
            marker = _read(record_dir(root, prev))
            if marker is not None and marker.get("status") == STATUS_RESUMED and mark_landed(record_dir(root, prev), sha):
                stamped.append(prev)
        except (OSError, ValueError):
            break
        visited.add(prev)
        cur = prev
    return stamped
