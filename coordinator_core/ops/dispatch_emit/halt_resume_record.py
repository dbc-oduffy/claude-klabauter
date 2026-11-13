"""The halt-resume record: halt-time work stored beside `halted.json`, plus the bounded-read seam.

`resume.json` carries what a halted run already determined (composed script, hoisted fragments,
sizing gate verdict, landed-state sha) with a fingerprint of its inputs. A reader checks the
fingerprint and never recomputes. No git spawn and no listing here (the caller resolves the halted run first); every read is deadline-bound
through `bounded_read`, which a stalled disk cannot hold past its deadline.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from pathlib import Path
from typing import Optional, Union

from coordinator_core.atomic_replace import atomic_write_bytes

RESUME_FILE = "resume.json"
REFIRE_LOG_FILE = "refire-launches.jsonl"
BUILDING_FILE = "resume.building"
RECOMPOSE_TTL_S = 300.0
SCHEMA = "halt-resume-record/1"
RECORD_FIELDS = ("composed_script_rel", "hoisted_fragments", "gate_verdict", "landed_state_sha")


class _Sentinel:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name

    def __bool__(self) -> bool:
        return False


MISSING = _Sentinel("MISSING")
STALLED = _Sentinel("STALLED")

_inflight: set[str] = set()
_inflight_lock = threading.Lock()


OVERSIZE = _Sentinel("OVERSIZE")
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


def _read_file(path: Path) -> bytes:
    return path.read_bytes()


_idle_workers: list = []
_idle_lock = threading.Lock()


def _worker_loop(jobs: "queue.SimpleQueue") -> None:
    while True:
        job = jobs.get()
        try:
            job()
        finally:
            with _idle_lock:
                _idle_workers.append(jobs)


def _run_on_worker(job) -> None:
    """Run `job` on an idle daemon worker, starting one only when none is idle. A worker stuck in a
    stalled read is not idle, so a later read never queues behind it."""
    with _idle_lock:
        jobs = _idle_workers.pop() if _idle_workers else None
    if jobs is None:
        jobs = queue.SimpleQueue()
        threading.Thread(target=_worker_loop, args=(jobs,), daemon=True).start()
    jobs.put(job)


def bounded_read(
    path: Union[str, Path], deadline_ms: float, max_bytes: int = DEFAULT_MAX_BYTES
) -> Union[bytes, _Sentinel]:
    """Bytes of `path`, `MISSING` when absent or unreadable, `OVERSIZE` when the file exceeds
    `max_bytes`, `STALLED` when the read outlives `deadline_ms`. The size check runs before the
    read, on the worker thread. The read runs on a daemon thread left to finish after a stall; at
    most one read per path is in flight, so a second call while one is outstanding returns
    `STALLED` at once."""
    key = str(path)
    with _inflight_lock:
        if key in _inflight:
            return STALLED
        _inflight.add(key)
    box: list = []
    done = threading.Event()

    def work() -> None:
        try:
            if Path(key).stat().st_size > max_bytes:
                box.append(OVERSIZE)
            else:
                data = _read_file(Path(key))
                box.append(OVERSIZE if len(data) > max_bytes else data)
        except OSError:
            box.append(MISSING)
        except BaseException:
            box.append(MISSING)
        finally:
            with _inflight_lock:
                _inflight.discard(key)
            done.set()

    try:
        _run_on_worker(work)
    except BaseException:
        with _inflight_lock:
            _inflight.discard(key)
        return STALLED
    if not done.wait(max(deadline_ms, 0) / 1000.0):
        return STALLED
    return box[0]


_write_inflight: set[str] = set()
_write_inflight_lock = threading.Lock()


def write_outstanding(path: Union[str, Path]) -> bool:
    """True while a `bounded_write` to `path` is still running on its worker thread."""
    with _write_inflight_lock:
        return str(path) in _write_inflight


def bounded_write(path: Union[str, Path], data: bytes, deadline_ms: float) -> Optional[_Sentinel]:
    """Atomically write `data` to `path`: `None` on success, `STALLED` when the write outlives
    `deadline_ms`; an `OSError` on the worker is re-raised here. The write keeps running on a daemon
    thread after a stall (it may still land). At most one write per path is in flight, so a second
    call while one is outstanding returns `STALLED` at once."""
    key = str(path)
    with _write_inflight_lock:
        if key in _write_inflight:
            return STALLED
        _write_inflight.add(key)
    box: list = []
    done = threading.Event()

    def work() -> None:
        try:
            atomic_write_bytes(Path(key), data)
        except BaseException as exc:
            box.append(exc)
        finally:
            with _write_inflight_lock:
                _write_inflight.discard(key)
            done.set()

    try:
        threading.Thread(target=work, daemon=True).start()
    except BaseException:
        with _write_inflight_lock:
            _write_inflight.discard(key)
        return STALLED
    if not done.wait(max(deadline_ms, 0) / 1000.0):
        return STALLED
    if box:
        raise box[0]
    return None


HINT_DIR = ".coordinator-local/halt-resume-hint"
HINT_READ_DEADLINE_MS = 50.0


def _hint_path(root: Union[str, Path], sizing_rel: str) -> Path:
    key = hashlib.sha256(sizing_rel.replace("\\", "/").encode("utf-8")).hexdigest()[:20]
    return Path(root) / HINT_DIR / f"{key}.txt"


def write_hint(root: Union[str, Path], sizing_rel: str, run_id: str) -> None:
    """Remember the halted run a refire of `sizing_rel` resumes, so a refire whose halted-run lookup stalls can still name it."""
    path = _hint_path(root, sizing_rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, run_id.encode("utf-8"))


def read_hint(root: Union[str, Path], sizing_rel: str) -> Optional[str]:
    """The run id `write_hint` stored for `sizing_rel`; None when absent, unreadable, unsafe or stalled past `HINT_READ_DEADLINE_MS`."""
    raw = bounded_read(_hint_path(root, sizing_rel), HINT_READ_DEADLINE_MS)
    if not isinstance(raw, bytes):
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    return text if text and all(c.isalnum() or c in "-_" for c in text) else None


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def fingerprint_of(root: Union[str, Path], plan_rel: Optional[str], sizing_rel: Optional[str], halt_head: Optional[str]) -> dict:
    """The record's validity key: sha256 of the plan and sizing file bytes (None for an absent or
    unnamed file) and the HEAD at halt. Hashes bytes; spawns nothing."""
    root = Path(root)
    return {
        "plan_sha256": _sha256_file(root / plan_rel) if plan_rel else None,
        "sizing_sha256": _sha256_file(root / sizing_rel) if sizing_rel else None,
        "halt_head": halt_head,
    }


def inherited_stop_after(root: Union[str, Path], record_dir: Union[str, Path]) -> Optional[str]:
    """`plan` when the halted run's lineage still owes a park; None once the run's own halt is the park
    (a deliberate resume of a parked run stages) or no ancestor carries one. Spawns nothing."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PARKED, lineage_stop_after

    marker = halted_run._read(Path(record_dir))
    if marker and marker.get("halted") == HALT_PARKED:
        return None
    return lineage_stop_after(Path(root), Path(record_dir).name)


_ENGINE_SHA: list = []


def engine_sha() -> Optional[str]:
    """The running engine checkout's HEAD, read from the git dir (no spawn) once per process: an
    engine swap starts a new process, so a process never sees two answers; None when unresolvable."""
    if not _ENGINE_SHA:
        from coordinator_core.git.git_state import head_sha
        from coordinator_core.git.repo_root import _walk_for_repo

        found = _walk_for_repo(Path(__file__).resolve().parent)
        _ENGINE_SHA.append(head_sha(found[1], fresh=True) if found else None)
    return _ENGINE_SHA[0]


def fingerprint_for_run(
    root: Union[str, Path],
    record_dir: Union[str, Path],
    sizing_rel: Optional[str],
    plan_rel: Optional[str],
    landed_state_sha: Union[str, None, _Sentinel] = MISSING,
) -> dict:
    """The one fingerprint both sides compute: `plan_rel` is the resolved resume plan (the producer
    derives it, the record stores it as `resume_plan_rel`, the reader passes the resolution it
    wants served) and the halt HEAD is `halted_run_base(record_dir)`, stable across peer commits.
    `landed_state_sha` joins the key only when given (the reader never computes it: that costs a
    git spawn). `engine_sha` names the engine that composes or reads, so a record composed by another
    engine reads stale. Spawns nothing."""
    from coordinator_core.ops.dispatch_emit.halted_run import halted_run_base, revised_halt

    fp = fingerprint_of(root, plan_rel, sizing_rel, halted_run_base(Path(record_dir)))
    fp["resume_plan_rel"] = plan_rel
    fp["revised_at"] = revised_halt(Path(record_dir))
    fp["stop_after"] = inherited_stop_after(root, record_dir)
    fp["engine_sha"] = engine_sha()
    if landed_state_sha is not MISSING:
        fp["landed_state_sha"] = landed_state_sha
    return fp


def resolve_fingerprint_plan(root: Union[str, Path], record_dir: Union[str, Path], sizing_rel: str) -> Optional[str]:
    """The plan both sides hash, resolved from the sizing and the halt marker alone (never from the
    record): the sizing's `plan:` edge, else `s_plan_path` at arm S, else the marker's halt plan.
    Spawns nothing; a sizing that will not load falls through to the marker's plan."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD
    from coordinator_core.ops.dispatch_emit import sizing_fire

    try:
        sizing = sizing_fire.load_sizing(Path(root), sizing_rel)
    except (OSError, ValueError, sizing_fire.SizingFireRefused):
        sizing = {}
    edge = sizing.get("plan")
    if isinstance(edge, str) and edge.strip():
        return edge
    try:
        if sizing_fire.resolve_arm(sizing) == sizing_fire.ARM_S:
            return sizing_fire.s_plan_path(sizing_rel)
    except sizing_fire.SizingFireRefused:
        pass
    marker = halted_run._read(Path(record_dir))
    plan = marker.get(HALT_PLAN_FIELD) if marker else None
    return plan if isinstance(plan, str) and plan else None


def claim_recompose(record_dir: Union[str, Path], now: float, ttl_s: float) -> bool:
    """True when the caller may spawn the recompose: it writes `resume.building`, which a spawn
    younger than `ttl_s` leaves standing so a refire does not stack a second one. The builder
    removes it when it ends."""
    stamp = Path(record_dir) / BUILDING_FILE
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        with stamp.open("x", encoding="utf-8") as fh:
            fh.write(repr(now))
        return True
    except FileExistsError:
        pass
    except OSError:
        return True
    try:
        if now - float(stamp.read_text(encoding="utf-8")) < ttl_s:
            return False
    except (OSError, ValueError):
        pass
    try:
        stamp.write_text(repr(now), encoding="utf-8")
    except OSError:
        pass
    return True


def release_recompose(record_dir: Union[str, Path]) -> None:
    try:
        (Path(record_dir) / BUILDING_FILE).unlink()
    except OSError:
        pass


def build_record(
    *,
    composed_script_rel: Optional[str],
    hoisted_fragments,
    gate_verdict,
    landed_state_sha: Optional[str],
    fingerprint: dict,
    resume_plan_rel: Optional[str] = None,
    retry_not_before: Optional[float] = None,
    stop_after: Optional[str] = None,
) -> dict:
    """A record dict in the on-disk shape; `retry_not_before` is set only on a refused build and
    `stop_after` only when the composed script carries the inherited park."""
    return {
        "stop_after": stop_after,
        "retry_not_before": retry_not_before,
        "schema": SCHEMA,
        "resume_plan_rel": resume_plan_rel,
        "composed_script_rel": composed_script_rel,
        "hoisted_fragments": hoisted_fragments,
        "gate_verdict": gate_verdict,
        "landed_state_sha": landed_state_sha,
        "fingerprint": dict(fingerprint),
    }


def _run_ended(record_dir: Path) -> bool:
    """True when the run's marker is `landed` or `closed`: it has nothing to resume, so no record writer touches its dir."""
    from coordinator_core.ops.dispatch_emit import halted_run

    marker = halted_run._read(record_dir)
    return bool(marker) and marker.get("status") in (halted_run.STATUS_LANDED, halted_run.STATUS_CLOSED)


def write(record_dir: Union[str, Path], record: dict) -> Path:
    """Atomically write `resume.json` into `record_dir` (created if absent); return its path. A run whose
    marker is `landed` or `closed` has nothing to resume: the write is dropped and the path returned."""
    record_dir = Path(record_dir)
    target = record_dir / RESUME_FILE
    if _run_ended(record_dir):
        return target
    record_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return target


def write_refire_record(
    record_dir: Union[str, Path], script_path: Union[str, Path], kind: str, composed_by: Optional[str], now: float
) -> None:
    """Append one line to `refire-launches.jsonl` in `record_dir`: the script a refire launched, its
    sha256 (None when unreadable), `kind` ("stored" or "launcher"), `composed_by` (the engine sha that
    composed it, None when unknown), the running `engine_sha` and `now`. Dropped for a `landed` or `closed` run. Raises OSError on a failed write."""
    record_dir = Path(record_dir)
    if _run_ended(record_dir):
        return
    record_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "script_path": Path(script_path).as_posix(),
        "script_sha256": _sha256_file(Path(script_path)),
        "kind": kind,
        "composed_by_engine_sha": composed_by,
        "engine_sha": engine_sha(),
        "launched_at": now,
    }
    with open(record_dir / REFIRE_LOG_FILE, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(line) + "\n")


def read(record_dir: Union[str, Path], deadline_ms: float) -> Union[dict, _Sentinel]:
    """The record, `MISSING` (absent, corrupt or wrong schema), or `STALLED` (read past deadline)."""
    raw = bounded_read(Path(record_dir) / RESUME_FILE, deadline_ms)
    if raw is MISSING or raw is STALLED:
        return raw
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return MISSING
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or not isinstance(record.get("fingerprint"), dict):
        return MISSING
    return record


def matches(record: dict, fingerprint: dict) -> bool:
    """True when the record's fingerprint equals `fingerprint` on plan sha, sizing sha, halt HEAD and
    `revised_at` (a revise after the record was built leaves it stale), and also on
    `resume_plan_rel`, `landed_state_sha`, `stop_after` and `engine_sha` when `fingerprint` carries
    them (a record with no stored `engine_sha` is stale against any engine). A None
    halt HEAD never matches (nothing pins the record to a halt), and the record's own plan and
    landed-state fields must agree with its stored fingerprint."""
    stored = record.get("fingerprint") or {}
    if stored.get("halt_head") is None or fingerprint.get("halt_head") is None:
        return False
    keys = ["plan_sha256", "sizing_sha256", "halt_head", "revised_at"]
    keys += [k for k in ("resume_plan_rel", "landed_state_sha", "stop_after", "engine_sha") if k in fingerprint]
    if not all(stored.get(k) == fingerprint.get(k) for k in keys):
        return False
    if "resume_plan_rel" in stored and stored["resume_plan_rel"] != record.get("resume_plan_rel"):
        return False
    if "landed_state_sha" in stored and stored["landed_state_sha"] != record.get("landed_state_sha"):
        return False
    return True


def in_backoff(record: dict, now: float) -> bool:
    """True while a refused build's `retry_not_before` (epoch seconds) is still ahead of `now`: the
    caller serves the stored refusal and spawns no recompose."""
    until = record.get("retry_not_before")
    return isinstance(until, (int, float)) and not isinstance(until, bool) and now < until


JSON_READ_DEADLINE_MS = 250.0
JSON_READ_MAX_BYTES = 4 * 1024 * 1024


def bounded_json_object(
    path: Union[str, Path], deadline_ms: float = JSON_READ_DEADLINE_MS, max_bytes: int = JSON_READ_MAX_BYTES
) -> Optional[dict]:
    """The JSON object in `path`; None when absent, unreadable, oversize, stalled, malformed or not an object."""
    raw = bounded_read(path, deadline_ms, max_bytes)
    if not isinstance(raw, bytes):
        return None
    try:
        got = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return got if isinstance(got, dict) else None
