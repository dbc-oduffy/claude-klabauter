"""`AckStore` -- the accept-process record that a MUTATING dispatch was asked
before it ran, so `warm.request_status` can answer a poll without reading
side effects.

Implements `coordinator_core/contract/dispatch-ack-reconcile-contract.md`
(D1-D5). This module imports only the stdlib -- it is loaded by both the
accept process and, indirectly, by tests that construct a store in
isolation.
"""

from __future__ import annotations

import calendar
import itertools
import json
import os
import shutil
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable, Optional

DEFAULT_CAPACITY = 4000
RESULT_RETAIN_COUNT = 64
RESULT_RETAIN_BYTES = 4 * 1024 * 1024

#: Bounds on the per-record outcome digest kept beside the result LRU: at most
#: DEFAULT_CAPACITY records x RESULT_DIGEST_BYTES resident (about 8 MiB).
RESULT_DIGEST_BYTES = 2048
RESULT_DIGEST_KEYS = 32
RESULT_DIGEST_STR_CHARS = 200
#: Door-form stdout larger than this is not parsed; the digest says "stdout_unparsed".
RESULT_STDOUT_PARSE_BYTES = 1 << 20

#: A finished failure's reason text, kept on the record (not in the result LRU) at
#: most FAILURE_TEXT_BYTES long, dropped only once read AND FAILURE_TEXT_MIN_SECS old.
FAILURE_TEXT_BYTES = 2048
FAILURE_TEXT_MIN_SECS = 600.0

DROPPED_OVER_CAP = "over-cap"
DROPPED_NOT_SERIALISABLE = "not-serialisable"
DROPPED_EVICTED = "evicted"

_ADMITTED = "admitted"
_STAMPED = "stamped"
_TOMBSTONED = "tombstoned"

STATE_NOT_RECEIVED = "not_received"
STATE_QUEUED = "queued"
STATE_EXECUTING = "executing"
STATE_FINISHED = "finished"
STATE_UNKNOWABLE = "unknowable"

REASON_ENGINE_RESTARTED = "engine-restarted"
REASON_EXPIRED = "expired"
REASON_NO_RESIDENT_ENGINE = "no-resident-engine"
REASON_FOREIGN_ENGINE = "foreign-engine"
#: The pool worker that owned an admitted dispatch is gone: a pool-worker liveness fact, never
#: derived from abandonment. Terminal; rides on `unknowable` so the state set is unchanged.
REASON_WORKER_DEAD = "worker_dead"

#: Two readings of one pid's start time further apart than this name different processes.
_START_TIME_TOLERANCE_SECS = 1.0

#: Longest a status poll waits on a worker locator's claim-file read; a read that does not answer leaves
#: the record unresolved until the next poll, so a stalled filesystem never holds `warm.request_status`.
_LOCATE_DEADLINE_SECS = 0.05


def pid_start_time(pid: Optional[int]) -> Optional[float]:
    """`pid`'s process start time (epoch seconds) where psutil is importable, else None. Distinguishes
    the process that held a pid from a later one that reused it. Never raises."""
    if pid is None:
        return None
    try:
        import psutil

        return float(psutil.Process(int(pid)).create_time())
    except Exception:  # noqa: BLE001 -- no psutil, no such pid, access denied: no reading
        return None

OUTCOME_RESULT = "result"
OUTCOME_ERROR = "error"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_WORKER_LOST = "worker-lost"
#: The accept process terminated the worker at its result deadline; the op may have half-applied.
OUTCOME_KILLED = "killed"
OUTCOME_NOT_DISPATCHED = "not-dispatched"

_FINISHED_OUTCOMES = (OUTCOME_RESULT, OUTCOME_ERROR)

#: Answers "how long past its executing bound may a still-unstamped record read
#: `executing` (overdue) before a poll reads it abandoned?" A multiple of the
#: record's own bound, never a raised op budget. 10x the 30s bound is 300s, the
#: inline executor join (`asyncio.run`), so a handler that finishes inside that
#: join is read as executing; past it the supersede rule in `stamp`, not this
#: ceiling, carries a late result.
OVERDUE_CEILING_FACTOR = 10
_UNKNOWABLE_OUTCOMES = (OUTCOME_ABANDONED, OUTCOME_WORKER_LOST, OUTCOME_KILLED)


#: Per-process call counter; `next()` on an `itertools.count` is atomic under the GIL.
_dispatch_seq = itertools.count()


def mint_key(pid: Optional[int] = None) -> str:
    """The one dispatch-key minter, `<pid>-<seq>-<monotonic_ns>` (contract § 1).

    The sequence keeps two calls distinct when a coarse monotonic clock
    (Windows) returns the same tick twice.

    TRAP: the nanosecond stamp must stay the LAST field -- `mint_ns_of` reads
    the tail as the mint time, so a trailing sequence reads as "minted at ns 0".
    The clock must be the one `boot_ns` uses: eviction lifts `low_water_ns` to an
    evicted key's tail, so a key from another clock domain refuses every later
    key from this one.
    """
    return f"{os.getpid() if pid is None else pid}-{next(_dispatch_seq)}-{time.monotonic_ns()}"


def mint_ns_of(key: str) -> Optional[int]:
    tail = key.rpartition("-")[2]
    try:
        return int(tail)
    except (ValueError, TypeError):
        return None


def _scalars(obj: dict) -> dict:
    """Top-level bool/int/float/None and short-string values of `obj`, at most
    RESULT_DIGEST_KEYS of them; every other key is named under "omitted"."""
    out: dict = {}
    omitted: list = []
    for k, v in obj.items():
        name = str(k)
        if v is None or isinstance(v, (bool, int, float)) or (
            isinstance(v, str) and len(v) <= RESULT_DIGEST_STR_CHARS
        ):
            if len(out) < RESULT_DIGEST_KEYS:
                out[name] = v
                continue
        if len(omitted) < RESULT_DIGEST_KEYS:
            omitted.append(name[:RESULT_DIGEST_STR_CHARS])
    if omitted:
        out["omitted"] = omitted
    return out


def _digest_size(digest: dict) -> int:
    return len(json.dumps(digest, separators=(",", ":")).encode("utf-8"))


def _outcome_digest(result: Any) -> dict:
    """What a landed op reported, in at most RESULT_DIGEST_BYTES.

    Reads one dict level, plus one JSON-parsed level of a string `stdout` (the
    `invoke.from_argv` door form). Never serialises the whole result, so a
    non-serialisable reply still digests. Runs outside the store lock.
    """
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    digest = _scalars(result)
    stdout = result.get("stdout")
    if isinstance(stdout, str) and stdout.lstrip().startswith("{"):
        if len(stdout) > RESULT_STDOUT_PARSE_BYTES:
            parsed = None
            digest["stdout_unparsed"] = True
        else:
            try:
                parsed = json.loads(stdout)
            except (ValueError, RecursionError):
                parsed = None
        if isinstance(parsed, dict):
            digest["stdout"] = _scalars(parsed)
    try:
        if _digest_size(digest) <= RESULT_DIGEST_BYTES:
            return digest
    except (TypeError, ValueError):
        pass
    digest.pop("stdout", None)
    digest.pop("omitted", None)
    for name in reversed(list(digest)):
        if _digest_size(digest) <= RESULT_DIGEST_BYTES - 20:  # room for "truncated"
            break
        del digest[name]
    digest["truncated"] = True
    return digest


def _clip_text(text: str) -> str:
    return text.encode("utf-8")[:FAILURE_TEXT_BYTES].decode("utf-8", "ignore")


def _failure_text_of(result: Any) -> Optional[str]:
    """The reason text of a landed op that failed (nonzero `exit_code`): its
    stderr, else its `error`, else its stdout, else None. At most FAILURE_TEXT_BYTES.
    An `invoke.from_argv` op carries its JSON-RPC error in stdout with stderr empty."""
    if not isinstance(result, dict):
        return None
    code = result.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, int) or code == 0:
        return None
    for field in ("stderr", "error", "stdout"):
        value = result.get(field)
        if isinstance(value, str) and value.strip():
            return _clip_text(value)
        if value:
            try:
                return _clip_text(json.dumps(value))
            except (TypeError, ValueError, RecursionError):
                return _clip_text(str(value))
    return None


JOURNAL_RETAIN_SECS = 24 * 3600.0
_JOURNAL_SUFFIX = ".outcome.json"
_LAND_SUFFIX = ".land.json"
#: Present once the one-off sweep of the pre-bucket flat entries has finished.
_LEGACY_MARKER = ".legacy-swept"
_DAY_SECS = 86400.0
#: Entries one prune pass may handle (bucket drops and legacy moves/deletes); the
#: retention thread resumes the next pass where this one stopped, paced by the gap.
PRUNE_ENTRY_BUDGET = 500
_PASS_GAP_SECS = 0.05
_prune_lock = threading.Lock()
_retention_started: dict = {}
_retention_started_lock = threading.Lock()
LAND_VERDICT_COMMITTED = "committed"
LAND_VERDICT_NEVER_RAN = "never-ran"
LAND_VERDICT_UNRESOLVED = "unresolved"
_JOURNAL_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


JOURNAL_DIRNAME = "ack-journal"


def journal_dir_under(svc_dir: Any) -> str:
    """The one journal location beside the engine's service files; the accept process
    and the client both derive it here, so a client reads what a worker journaled."""
    return os.path.join(os.fspath(svc_dir), JOURNAL_DIRNAME)


def journal_dir_beside(sibling_dir: Any) -> Optional[str]:
    """The journal for a directory under the same svc dir (the claims dir a pool worker
    is handed), so a worker derives it with no extra argument. None for no directory."""
    if not sibling_dir:
        return None
    return journal_dir_under(os.path.dirname(os.fspath(sibling_dir).rstrip("/\\")))


def _bucket_name(ts: float) -> str:
    """The UTC-day bucket dir name (`d20261116`) an entry written at `ts` lives under."""
    return time.strftime("d%Y%m%d", time.gmtime(ts))


def _bucket_end(name: str) -> Optional[float]:
    """Epoch second a bucket's day ends, or None when `name` is not a bucket name."""
    if len(name) != 9 or name[0] != "d":
        return None
    try:
        return calendar.timegm(time.strptime(name[1:], "%Y%m%d")) + _DAY_SECS
    except ValueError:
        return None


def _valid_key(journal_dir: Any, key: Any) -> bool:
    return bool(journal_dir) and isinstance(key, str) and bool(key) and set(key) <= _JOURNAL_SAFE_CHARS


def _write_path(journal_dir: Any, key: Any, suffix: str) -> Optional[str]:
    """Where `key`'s entry is written now: under today's bucket."""
    if not _valid_key(journal_dir, key):
        return None
    return os.path.join(os.fspath(journal_dir), _bucket_name(time.time()), key + suffix)


def _read_paths(journal_dir: Any, key: Any, suffix: str):
    """Candidate paths for `key`'s entry, newest first: today's bucket, yesterday's
    (an entry is read within JOURNAL_RETAIN_SECS of its write, one day), then the
    pre-bucket flat path until the legacy sweep has finished.

    TRAP: the key's tail is a monotonic stamp, not wall time, so the bucket cannot be
    derived from the key; a read probes the two buckets a live entry can sit in."""
    if not _valid_key(journal_dir, key):
        return
    base = os.fspath(journal_dir)
    now = time.time()
    yield os.path.join(base, _bucket_name(now), key + suffix)
    yield os.path.join(base, _bucket_name(now - _DAY_SECS), key + suffix)
    if not os.path.exists(os.path.join(base, _LEGACY_MARKER)):
        yield os.path.join(base, key + suffix)


def _read_json(journal_dir: Any, key: Any, suffix: str) -> Any:
    for path in _read_paths(journal_dir, key, suffix):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError, RecursionError):
            continue
    return None


def _entry_exists(journal_dir: Any, key: Any, suffix: str) -> bool:
    return any(os.path.exists(path) for path in _read_paths(journal_dir, key, suffix))


def _retention_loop(journal_dir: str) -> None:
    """Run bounded prune passes, paced, until the legacy sweep has finished."""
    try:
        while prune_journal(journal_dir) is False:
            time.sleep(_PASS_GAP_SECS)
    except Exception:  # noqa: BLE001 -- retention is best effort
        pass


def _start_retention(journal_dir: str) -> None:
    """One daemon thread that drops expired buckets and runs the legacy sweep."""
    threading.Thread(
        target=_retention_loop, args=(journal_dir,), name="ack-journal-retention", daemon=True
    ).start()


def retain_journal(journal_dir: Any) -> None:
    """Start retention for `journal_dir` at most once per UTC day in this process.

    TRAP: call only from the accept (server) process. A pool worker is recycled, so
    a worker-started sweep restarts from zero; workers journal through
    `record_outcome` / `record_land_intent`, which never start it."""
    base = os.fspath(journal_dir)
    day = _bucket_name(time.time())
    with _retention_started_lock:
        if _retention_started.get(base) == day:
            return
        _retention_started[base] = day
    try:
        _start_retention(base)
    except Exception:  # noqa: BLE001 -- retention is best effort; the entry is already durable
        pass


def _write_json(journal_dir: Any, path: str, payload: dict) -> bool:
    """Write `payload` to `path` by tmp + `os.replace`, creating its bucket. The write
    never deletes and never starts retention."""
    tmp = f"{path}.{os.getpid()}.tmp"
    bucket = os.path.dirname(path)
    try:
        try:
            os.mkdir(bucket)
        except FileExistsError:
            pass
        except FileNotFoundError:
            os.makedirs(os.fspath(journal_dir), exist_ok=True)
            try:
                os.mkdir(bucket)
            except FileExistsError:
                pass
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError, RecursionError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def record_outcome(
    journal_dir: Any,
    key: str,
    outcome: str,
    *,
    result: Any = None,
    error_code: Optional[int] = None,
    error_message: Optional[str] = None,
) -> bool:
    """Durably record that `key`'s op landed (`result`) or failed (`error`); an existing
    entry is kept (check-then-replace, not atomic: two racing writers record the same outcome). Callable from the pool worker the moment its handler returns, so the
    outcome survives a worker killed, a broken pool or an engine restart before the
    accept process can stamp it. Stdlib-only, never raises; True when an entry now exists."""
    path = _write_path(journal_dir, key, _JOURNAL_SUFFIX)
    if path is None or outcome not in _FINISHED_OUTCOMES:
        return False
    if _entry_exists(journal_dir, key, _JOURNAL_SUFFIX):
        return True
    entry: dict = {"outcome": outcome, "error_code": error_code}
    if outcome == OUTCOME_ERROR:
        entry["failure_text"] = _clip_text(str(error_message or ""))
    else:
        size, dropped = AckStore._measure(result)
        entry["result_digest"] = _outcome_digest(result)
        failure = _failure_text_of(result)
        if failure:
            entry["failure_text"] = failure
        if size is not None:
            entry["result"] = result
        else:
            entry["result_dropped"] = dropped
    return _write_json(journal_dir, path, entry)


def read_outcome(journal_dir: Any, key: str) -> Optional[dict]:
    """The `finished` poll payload journaled for `key`, or None. Never raises."""
    entry = _read_json(journal_dir, key, _JOURNAL_SUFFIX)
    if not isinstance(entry, dict) or entry.get("outcome") not in _FINISHED_OUTCOMES:
        return None
    payload: dict = {"state": STATE_FINISHED, "outcome": entry["outcome"], "journaled": True}
    if entry["outcome"] == OUTCOME_ERROR and entry.get("error_code") is not None:
        payload["error_code"] = entry["error_code"]
    if entry.get("failure_text") is not None:
        payload["failure_text"] = entry["failure_text"]
    if "result" in entry:
        payload["result"] = entry["result"]
    elif entry["outcome"] == OUTCOME_RESULT:
        payload["result_digest"] = entry.get("result_digest") or {}
        payload["result_dropped"] = entry.get("result_dropped", DROPPED_EVICTED)
    return payload


def record_land_intent(journal_dir: Any, key: str, record: dict) -> bool:
    """Durably write `key`'s land record (`<key>.land.json`) by one `os.replace`, so a
    later phase ("claimed", "committing", "committed") overwrites the earlier one whole.
    Callable from the pool worker; stdlib-only, never raises; True when written."""
    path = _write_path(journal_dir, key, _LAND_SUFFIX)
    if path is None or not isinstance(record, dict):
        return False
    return _write_json(journal_dir, path, record)


def read_land_intent(journal_dir: Any, key: str) -> Optional[dict]:
    """The land record written for `key`, or None. Never raises."""
    record = _read_json(journal_dir, key, _LAND_SUFFIX)
    return record if isinstance(record, dict) else None


_IMPORTING_SUFFIX = ".importing"
_IMPORT_GATE_SUFFIX = ".import-gate"


def _create_exclusive(path: str) -> Optional[bool]:
    """Atomic create of an empty `path`, bucket dir included: True when this caller created
    it, False when it already existed, None when the filesystem refused. Never raises."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    except OSError:
        return None
    return True


def begin_import_phase(journal_dir: Any, key: Any) -> Optional[str]:
    """Worker side: mark `key` as inside the cold import of its op's module, before any
    handler code runs. Returns the marker path to hand `end_import_phase`, or None when
    the marker could not be written (the dispatch then stays indeterminate). Never raises."""
    path = _write_path(journal_dir, key, _IMPORTING_SUFFIX)
    if path is None or _create_exclusive(path) is not True:
        return None
    return path


def end_import_phase(marker: Optional[str]) -> bool:
    """Worker side: the import is done; True when the handler may now run. The gate is one
    O_EXCL create shared with `abandon_import_phase`, so exactly one of them wins: False
    means the caller was already told the op never ran, and the handler must not start.
    No marker answers True. A filesystem that refuses the gate also answers True, after removing
    the importing marker so a later `abandon_import_phase` finds no import in flight and never
    reports the running handler as retry-safe. Never raises."""
    if marker is None:
        return True
    gate = marker[: -len(_IMPORTING_SUFFIX)] + _IMPORT_GATE_SUFFIX
    won = _create_exclusive(gate)
    if won is None:
        try:
            os.unlink(marker)
        except OSError:
            pass
    return won is not False


def abandon_import_phase(journal_dir: Any, key: Any) -> bool:
    """Caller side: True only when `key`'s worker is still inside module import and this call
    won the gate, so its handler will never run and a retry cannot double-execute. False for
    a warm module (no marker), a worker already past import, or an unreadable journal. Never raises."""
    for marker in _read_paths(journal_dir, key, _IMPORTING_SUFFIX):
        try:
            if not os.path.exists(marker):
                continue
        except OSError:
            return False
        gate = marker[: -len(_IMPORTING_SUFFIX)] + _IMPORT_GATE_SUFFIX
        return _create_exclusive(gate) is True
    return False


def prune_journal(
    journal_dir: Any, now: Optional[float] = None, budget: int = PRUNE_ENTRY_BUDGET
) -> bool:
    """One bounded pass: drop every age-bucket dir that ended more than
    JOURNAL_RETAIN_SECS ago, by its name alone (no per-entry stat); until the one-off
    legacy sweep has finished, also sweep the pre-bucket flat entries (past retention
    deleted, the rest moved into their bucket), then write the sweep marker. The pass
    handles at most `budget` entries. Returns False when it stopped on the budget with
    more to do (call again; the next pass resumes there, the sweep being on-disk state),
    else True. Never raises; one pass at a time per process.

    TRAP: not for a hook or boot path. `AckStore.stamp` starts the retention thread
    (`retain_journal`), server process only."""
    if not _prune_lock.acquire(blocking=False):
        return True
    try:
        current = time.time() if now is None else now
        base = os.fspath(journal_dir)
        legacy = not os.path.exists(os.path.join(base, _LEGACY_MARKER))
        cutoff = current - JOURNAL_RETAIN_SECS
        handled = 0
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    name = entry.name
                    end = _bucket_end(name)
                    try:
                        if end is not None:
                            if end <= cutoff:
                                shutil.rmtree(entry.path, ignore_errors=True)
                                handled += 1
                        elif legacy and name.endswith((_JOURNAL_SUFFIX, _LAND_SUFFIX, ".tmp")):
                            # A fresh `.tmp` stays and costs the budget nothing, so a flat dir
                            # holding only those still finishes the sweep and writes its marker.
                            if _sweep_legacy(base, entry, cutoff):
                                handled += 1
                    except OSError:
                        pass
                    if handled >= budget:
                        return False
        except OSError:
            return True
        if legacy:
            try:
                with open(os.path.join(base, _LEGACY_MARKER), "a"):
                    pass
            except OSError:
                pass
        return True
    finally:
        _prune_lock.release()


def _sweep_legacy(base: str, entry: "os.DirEntry[str]", cutoff: float) -> bool:
    """One pre-bucket flat entry: delete it past `cutoff`, else move it under the
    bucket of its mtime (kept when the bucket already holds the key). True when the
    entry left the flat dir, False when a fresh `.tmp` stays."""
    mtime = entry.stat().st_mtime
    if mtime < cutoff:
        os.unlink(entry.path)
        return True
    if entry.name.endswith(".tmp"):
        return False
    bucket = os.path.join(base, _bucket_name(mtime))
    os.makedirs(bucket, exist_ok=True)
    target = os.path.join(bucket, entry.name)
    if os.path.exists(target):
        os.unlink(entry.path)
    else:
        os.replace(entry.path, target)
    return True


class AckStore:

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        *,
        boot_ns: Optional[int] = None,
        executing_bound: Optional[Callable[[Optional[str]], float]] = None,
        clock: Callable[[], float] = time.monotonic,
        journal_dir: Any = None,
    ) -> None:
        """`executing_bound` maps a method to the seconds an admitted record may
        read `executing`; None means unbounded. Passed in, never imported, so
        this module stays stdlib-only. `clock` times failure-text retention.

        `journal_dir`, when given, makes a finished outcome durable: `stamp` journals
        it, and `status` answers `finished` from the journal where memory alone would
        read `unknowable` (worker killed or lost, engine restarted, record evicted)."""
        self._journal_dir = journal_dir
        self._land_resolver: Optional[Callable[[dict], Optional[dict]]] = None
        self._clock = clock
        self._executing_bound = executing_bound
        self._capacity = capacity
        self.boot_ns = time.monotonic_ns() if boot_ns is None else boot_ns
        self.low_water_ns = self.boot_ns
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._results: "OrderedDict[str, tuple]" = OrderedDict()
        self._results_bytes = 0
        self._order: "deque[str]" = deque()
        self._records: dict = {}
        #: Finished keys not yet returned by `status`/`wait`, oldest first. Capacity
        #: and result-LRU eviction skip them; one leaves memory only once its result
        #: is journaled, and `status` then reads it back from the journal.
        self._pinned: "OrderedDict[str, None]" = OrderedDict()

    def admit(self, key: str, method: Optional[str]) -> bool:
        mint_ns = mint_ns_of(key)
        bound_secs: Optional[float] = None
        if self._executing_bound is not None:
            try:
                bound_secs = float(self._executing_bound(method))
            except Exception:  # noqa: BLE001 -- a bad bound is no bound; admit never refuses on it
                bound_secs = None
        with self._lock:
            if key in self._records:
                return False
            if mint_ns is not None and (mint_ns < self.boot_ns or mint_ns <= self.low_water_ns):
                return False
            self._records[key] = {
                "state": _ADMITTED,
                "method": method,
                "outcome": None,
                "error_code": None,
            }
            if bound_secs is not None:
                self._records[key]["admitted_mono"] = time.monotonic()
                self._records[key]["bound_secs"] = bound_secs
            self._order.append(key)
            self._evict_if_needed()
            return True

    def stamp(
        self,
        key: str,
        outcome: str,
        *,
        error_code: Optional[int] = None,
        result: Any = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Terminal stamp. `result` is kept only for a `result` outcome, only if
        JSON-serialisable and within the per-entry cap; it never affects state.
        A failure's reason text (`error_message` for an `error` outcome, the
        result's stderr/error for a nonzero `exit_code`) is kept on the record."""
        size: Optional[int] = None
        dropped: Optional[str] = None
        digest: Optional[dict] = None
        failure: Optional[str] = None
        if outcome == OUTCOME_RESULT and result is not None:
            size, dropped = self._measure(result)
            digest = _outcome_digest(result)
            failure = _failure_text_of(result)
        elif outcome == OUTCOME_ERROR and error_message:
            failure = _clip_text(str(error_message))
        journaled = False
        if self._journal_dir and outcome in _FINISHED_OUTCOMES:
            journaled = record_outcome(
                self._journal_dir, key, outcome,
                result=result, error_code=error_code, error_message=error_message,
            )
            retain_journal(self._journal_dir)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            if record["state"] != _ADMITTED and not (
                record["state"] == _STAMPED
                and (
                    record["outcome"] == OUTCOME_ABANDONED
                    or (record["outcome"] == OUTCOME_KILLED and "is_alive" in record)
                )
                and outcome in _FINISHED_OUTCOMES
            ):
                return
            for stale in ("is_alive", "worker_pid", "worker_dead", "worker_start", "locate"):
                record.pop(stale, None)
            if record.get("restarted") and outcome in _UNKNOWABLE_OUTCOMES:
                return
            record["state"] = _STAMPED
            record["outcome"] = outcome
            record["error_code"] = error_code
            if failure:
                record["failure_text"] = failure
                record["failure_at"] = self._clock()
                record["failure_read"] = False
            if journaled:
                record["journaled"] = True
            if outcome == OUTCOME_RESULT and result is not None:
                record["result_offered"] = True
                record["result_digest"] = digest
                if size is not None:
                    self._retain_result(key, result, size)
                else:
                    record["result_dropped"] = dropped
            if outcome in _FINISHED_OUTCOMES:
                self._pin(key)
            self._changed.notify_all()

    def stamp_killing(self, key: str, worker_pid: Optional[int], is_alive: Callable[[], bool]) -> None:
        """The accept process is terminating `key`'s worker: stamp `killed` and keep
        `worker_pid` and the `is_alive` probe on the record. `status` reads `killed`
        only once the probe reads dead; while it reads alive the record reads
        `executing`/overdue and a late result/error `stamp` still lands. Same
        no-op rules as `stamp` for an unknown, non-admitted or restarted record."""
        with self._lock:
            record = self._records.get(key)
            if record is None or record["state"] != _ADMITTED or record.get("restarted"):
                return
            record["state"] = _STAMPED
            record["outcome"] = OUTCOME_KILLED
            record["error_code"] = None
            record["worker_pid"] = worker_pid
            record["is_alive"] = is_alive
            record["worker_start"] = pid_start_time(worker_pid)
            record.pop("locate", None)
            self._changed.notify_all()

    def bind_worker(self, key: str, locate: Callable[[], Optional[tuple]]) -> None:
        """Register `locate() -> (worker pid, is_alive probe | None) | None` for an admitted `key`:
        the dispatch was handed to the pool and its worker claims it later. `status` and
        `mark_worker_dead` resolve it once, outside the lock; a worker found dead makes the record
        read `unknowable/worker_dead`. A no-op on an unknown or non-admitted key; never raises."""
        with self._lock:
            record = self._records.get(key)
            if record is not None and record["state"] == _ADMITTED:
                record["locate"] = locate

    def _resolve_worker(self, key: str) -> None:
        """Run an admitted, unresolved `key`'s locator and keep its pid, probe and the pid's start time
        on the record. The locator runs without the lock (it reads a claim file)."""
        with self._lock:
            record = self._records.get(key)
            locate = None
            if record is not None and record["state"] == _ADMITTED and not record.get("queued"):
                locate = record.get("locate") if "worker_pid" not in record else None
        if locate is None:
            return
        try:
            from coordinator_core.bounded_io import run_bounded

            found = run_bounded(f"locate-worker:{key}", locate, deadline=_LOCATE_DEADLINE_SECS, default=None)
        except Exception:  # noqa: BLE001 -- a locator that cannot answer leaves the record unresolved
            found = None
        if not found:
            return
        pid, probe = found
        start = pid_start_time(pid)
        with self._lock:
            record = self._records.get(key)
            if record is None or record["state"] != _ADMITTED or "worker_pid" in record:
                return
            record["worker_pid"] = pid
            record["worker_start"] = start
            if probe is not None:
                record["is_alive"] = probe
            record.pop("locate", None)

    def mark_worker_dead(self, pid: int) -> int:
        """Flag every admitted record owned by pool worker `pid` as `worker_dead` and return the count.
        A record whose locator has not resolved yet is resolved first. `status` then answers
        `unknowable/worker_dead` for it and never `executing` again."""
        with self._lock:
            pending = [k for k, r in self._records.items() if r["state"] == _ADMITTED and "locate" in r]
        for key in pending:
            self._resolve_worker(key)
        count = 0
        with self._lock:
            for record in self._records.values():
                if record["state"] == _ADMITTED and record.get("worker_pid") == pid and not record.get("worker_dead"):
                    record["worker_dead"] = True
                    count += 1
            if count:
                self._changed.notify_all()
        return count

    @staticmethod
    def _worker_alive(record: dict) -> bool:
        """Whether the record's worker still runs: its probe reads alive and the pid's start time is
        the one captured when the record was bound (a reused pid is another process). No probe or a
        raising probe reads dead."""
        probe = record.get("is_alive")
        if probe is None:
            return False
        try:
            if not probe():
                return False
        except Exception:  # noqa: BLE001 -- a probe that cannot answer reads dead
            return False
        start = record.get("worker_start")
        if start is None:
            return True
        now = pid_start_time(record.get("worker_pid"))
        return now is not None and abs(now - start) <= _START_TIME_TOLERANCE_SECS

    @classmethod
    def _alive_of(cls, record: dict, alive: Optional[bool]) -> bool:
        return cls._worker_alive(record) if alive is None else alive

    def note_killed(self, key: str, op: Optional[str], dump: Any = None) -> None:
        """Name the op a recycle or overdue kill took from `key`'s caller: `status` then adds `op` and,
        once `dump` resolves, `stall_dump`. `dump` is a path or a dict whose `stall_dump` entry the
        dump writer fills late. A no-op on an unknown key; never raises."""
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record["killed_op"] = op or record.get("method")
            if dump is not None:
                record["killed_dump"] = dump

    @staticmethod
    def _killed_facts(record: dict) -> dict:
        facts: dict = {}
        if record.get("killed_op"):
            facts["op"] = record["killed_op"]
        dump = record.get("killed_dump")
        path = dump.get("stall_dump") if isinstance(dump, dict) else dump
        if path:
            facts["stall_dump"] = str(path)
        return facts

    def reconcile_restarted(self, keys: Optional[Any] = None) -> int:
        """Mark every still-admitted record (or only `keys`) engine-restarted and
        return the count marked. A later result/error stamp still lands; a
        worker-lost/abandoned stamp on a marked record is dropped, so tearing
        down the old pool cannot overwrite the reason. Wakes `wait` callers."""
        wanted = None if keys is None else set(keys)
        count = 0
        with self._lock:
            for key, record in self._records.items():
                if record["state"] != _ADMITTED or record.get("restarted"):
                    continue
                if wanted is not None and key not in wanted:
                    continue
                record["restarted"] = True
                count += 1
            if count:
                self._changed.notify_all()
        return count

    def mark_queued(self, key: str) -> None:
        """The admitted request is waiting for a pool slot: `status` reads `queued`.
        A no-op on an unknown or non-admitted key; never raises."""
        with self._lock:
            record = self._records.get(key)
            if record is not None and record["state"] == _ADMITTED:
                record["queued"] = True

    def mark_dispatched(self, key: str) -> None:
        """A slot was granted: the record reads `executing` again and its bound
        starts now, so queue time never ages it. A no-op on an unknown or
        non-admitted key; never raises."""
        with self._lock:
            record = self._records.get(key)
            if record is not None and record["state"] == _ADMITTED:
                record.pop("queued", None)
                if "admitted_mono" in record:
                    record["admitted_mono"] = time.monotonic()

    def mark_withdrawn(self, key: str) -> None:
        """The caller's wait ended while the handler still runs: `status` reads
        `unknowable/abandoned` at once. The record stays _ADMITTED, so a late
        result/error `stamp` still lands. A no-op on an unknown or non-admitted
        key; never raises."""
        with self._lock:
            record = self._records.get(key)
            if record is not None and record["state"] == _ADMITTED:
                record["withdrawn"] = True
                self._changed.notify_all()

    def wait(self, key: str, timeout_secs: float, expect_engine: Optional[dict] = None) -> dict:
        """Block until `key` leaves `executing` or `timeout_secs` passes, then
        answer `status(key)`. Condition-driven: no polling, no settle interval.
        An unknown or tombstoned key answers at once."""
        deadline = time.monotonic() + max(0.0, timeout_secs)
        with self._changed:
            while True:
                record = self._records.get(key)
                if (
                    record is None
                    or record["state"] != _ADMITTED
                    or record.get("restarted")
                    or record.get("withdrawn")
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
        return self.status(key, expect_engine)

    @staticmethod
    def _measure(result: Any) -> "tuple[Optional[int], Optional[str]]":
        """`(size, None)` when retainable, else `(None, drop cause)`. Runs outside the lock."""
        try:
            size = len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            return None, DROPPED_NOT_SERIALISABLE
        if size <= RESULT_RETAIN_BYTES // 4:
            return size, None
        return None, DROPPED_OVER_CAP

    def _drop_result(self, key: str) -> None:
        dropped = self._results.pop(key, None)
        if dropped is not None:
            self._results_bytes -= dropped[1]
            record = self._records.get(key)
            if record is not None:
                record["result_dropped"] = DROPPED_EVICTED

    def _spilled(self, key: str) -> bool:
        """True when `key`'s result is durable in the journal, so memory may let it go."""
        record = self._records.get(key)
        return bool(record is not None and record.get("journaled"))

    def _pin(self, key: str) -> None:
        """Pin a finished key until it is read. Past RESULT_RETAIN_COUNT pinned, the
        oldest journaled ones leave memory (`status` answers them from the journal);
        an unread key whose result is not durable stays pinned and resident."""
        self._pinned[key] = None
        over = len(self._pinned) - RESULT_RETAIN_COUNT
        if over <= 0:
            return
        for oldest in list(self._pinned):
            if over <= 0:
                break
            if self._spilled(oldest):
                del self._pinned[oldest]
                self._drop_result(oldest)
                over -= 1

    def _retain_result(self, key: str, result: Any, size: int) -> None:
        self._results[key] = (result, size)
        self._results_bytes += size
        while self._results and (
            len(self._results) > RESULT_RETAIN_COUNT or self._results_bytes > RESULT_RETAIN_BYTES
        ):
            victim = next((k for k in self._results if k not in self._pinned and k != key), None)
            if victim is None:
                # Only unread results (and this one) remain: spill the oldest journaled one.
                victim = next(
                    (k for k in self._results if k != key and k in self._pinned and self._spilled(k)),
                    None,
                )
                if victim is None:
                    return
                del self._pinned[victim]
            self._drop_result(victim)

    def _names_other_engine(self, expect_engine: Any) -> bool:
        """True when `expect_engine` (`{"pid", "boot_ns"}`, either optional) names an
        engine other than this one. Malformed or empty claims name nothing."""
        if not isinstance(expect_engine, dict):
            return False
        pid = expect_engine.get("pid")
        boot_ns = expect_engine.get("boot_ns")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid != os.getpid():
            return True
        return isinstance(boot_ns, int) and not isinstance(boot_ns, bool) and boot_ns != self.boot_ns

    def status(self, key: str, expect_engine: Optional[dict] = None) -> dict:
        """The poll answer for `key`: the in-memory answer, except that a journaled
        outcome replaces a non-final one (`unknowable`, or `executing`: a worker that
        landed its op before it was killed, lost or its engine restarted)."""
        self._resolve_worker(key)
        answer = self._status_in_memory(key, expect_engine, self._probe_worker(key))
        if (
            self._journal_dir
            and answer.get("state") == STATE_FINISHED
            and answer.get("result_dropped") == DROPPED_EVICTED
        ):
            journaled = read_outcome(self._journal_dir, key)
            if journaled is not None and "result" in journaled:
                answer = {
                    k: v for k, v in answer.items() if k not in ("result_digest", "result_dropped")
                }
                answer["result"] = journaled["result"]
            return answer
        if self._journal_dir and answer.get("state") in (STATE_UNKNOWABLE, STATE_EXECUTING):
            journaled = read_outcome(self._journal_dir, key)
            if journaled is not None:
                return journaled
            if answer.get("state") == STATE_UNKNOWABLE:
                return self._with_land_record(key, answer)
        return answer

    def set_land_resolver(self, fn: Optional[Callable[[dict], Optional[dict]]]) -> None:
        """Register `fn(land record) -> {"verdict": "committed"|"never-ran", "sha"?}`, the
        accept process's reader of what a dead land worker left. None clears it."""
        self._land_resolver = fn

    def _with_land_record(self, key: str, answer: dict) -> dict:
        """Resolve an unknowable `key` through its land record. No record: `answer` as is.
        A live `executing` answer is never resolved: its worker may be mid-commit."""
        record = read_land_intent(self._journal_dir, key)
        if record is None:
            return answer
        verdict: Optional[dict] = None
        resolver = self._land_resolver
        if resolver is not None:
            try:
                verdict = resolver(record)
            except Exception:  # noqa: BLE001 -- a resolver that cannot answer is unresolved
                verdict = None
        name = verdict.get("verdict") if isinstance(verdict, dict) else None
        if name == LAND_VERDICT_COMMITTED:
            land = {"verdict": name}
            if verdict.get("sha"):
                land["sha"] = verdict["sha"]
            return {"state": STATE_FINISHED, "outcome": OUTCOME_RESULT, "land_record": land}
        if name == LAND_VERDICT_NEVER_RAN:
            return {
                "state": STATE_NOT_RECEIVED,
                "outcome": OUTCOME_NOT_DISPATCHED,
                "land_record": {"verdict": name},
            }
        return {**answer, "land_record": {"verdict": LAND_VERDICT_UNRESOLVED}}

    def _probe_worker(self, key: str) -> Optional[bool]:
        """`key`'s worker liveness read without the store lock (the probe and the pid start time are
        kernel queries a stalled box blocks on); None when the record carries no probe."""
        with self._lock:
            record = self._records.get(key)
            if record is None or "is_alive" not in record:
                return None
            snapshot = {k: record.get(k) for k in ("is_alive", "worker_pid", "worker_start")}
        return self._worker_alive(snapshot)

    def _status_in_memory(
        self, key: str, expect_engine: Optional[dict] = None, alive: Optional[bool] = None
    ) -> dict:
        """The in-memory answer. `alive` is a liveness read taken outside the lock by `_probe_worker`;
        None reads it inline. `expect_engine` is the delivering engine the
        caller was told (`error.data.engine_pid`/`engine_boot_ns`): when it names a
        different engine and this store holds no record of `key`, the answer is
        `unknowable/foreign-engine` and nothing is tombstoned -- this engine cannot
        prove it would have admitted the key."""
        mint_ns = mint_ns_of(key)
        with self._lock:
            if key not in self._records and self._names_other_engine(expect_engine):
                return {"state": STATE_UNKNOWABLE, "reason": REASON_FOREIGN_ENGINE}
            if mint_ns is not None and mint_ns < self.boot_ns:
                return {"state": STATE_UNKNOWABLE, "reason": REASON_ENGINE_RESTARTED}
            if mint_ns is not None and mint_ns <= self.low_water_ns and key not in self._records:
                return {"state": STATE_UNKNOWABLE, "reason": REASON_EXPIRED}

            record = self._records.get(key)
            if record is None:
                self._records[key] = {
                    "state": _TOMBSTONED,
                    "method": None,
                    "outcome": None,
                    "error_code": None,
                }
                self._order.append(key)
                self._evict_if_needed()
                return {"state": STATE_NOT_RECEIVED, "outcome": OUTCOME_NOT_DISPATCHED}

            if record["state"] == _TOMBSTONED:
                return {"state": STATE_NOT_RECEIVED, "outcome": OUTCOME_NOT_DISPATCHED}
            if record["state"] == _ADMITTED:
                if record.get("restarted"):
                    return {
                        "state": STATE_UNKNOWABLE,
                        "reason": REASON_ENGINE_RESTARTED,
                        **self._killed_facts(record),
                    }
                if record.get("worker_dead") or ("is_alive" in record and not self._alive_of(record, alive)):
                    record["worker_dead"] = True
                    record.pop("is_alive", None)
                    return {
                        "state": STATE_UNKNOWABLE,
                        "reason": REASON_WORKER_DEAD,
                        "worker_alive": False,
                        "worker_pid": record.get("worker_pid"),
                        **self._killed_facts(record),
                    }
                if record.get("withdrawn"):
                    return {"state": STATE_UNKNOWABLE, "reason": OUTCOME_ABANDONED}
                if record.get("queued"):
                    return {"state": STATE_QUEUED}
                bound_secs = record.get("bound_secs")
                # TRAP: the pool done-callback may never fire -- a worker blocked
                # in `_run_joining_handler` never resolves its future -- so this
                # read is what ends `executing`. The record stays _ADMITTED so a
                # late `stamp` still lands.
                if bound_secs is not None:
                    age = time.monotonic() - record["admitted_mono"]
                    if age >= bound_secs * OVERDUE_CEILING_FACTOR:
                        return {"state": STATE_UNKNOWABLE, "reason": OUTCOME_ABANDONED}
                    if age >= bound_secs:
                        return {"state": STATE_EXECUTING, "overdue": True}
                return {"state": STATE_EXECUTING}

            outcome = record["outcome"]
            if outcome == OUTCOME_NOT_DISPATCHED:
                return {"state": STATE_NOT_RECEIVED, "outcome": OUTCOME_NOT_DISPATCHED}
            if outcome in _FINISHED_OUTCOMES:
                payload = {"state": STATE_FINISHED, "outcome": outcome}
                if outcome == OUTCOME_ERROR and record.get("error_code") is not None:
                    payload["error_code"] = record["error_code"]
                text = record.get("failure_text")
                if text is not None:
                    payload["failure_text"] = text
                    if record["failure_read"] and (
                        self._clock() - record["failure_at"] >= FAILURE_TEXT_MIN_SECS
                    ):
                        # Read once and old enough: this answer is the last carrier.
                        del record["failure_text"]
                    record["failure_read"] = True
                if record.get("result_offered"):
                    kept = self._results.get(key)
                    if kept is not None:
                        payload["result"] = kept[0]
                    else:
                        payload["result_digest"] = record.get("result_digest") or {}
                        payload["result_dropped"] = record.get("result_dropped", DROPPED_EVICTED)
                self._pinned.pop(key, None)
                return payload
            if outcome == OUTCOME_KILLED and ("is_alive" in record or record.get("worker_dead")):
                pid = record.get("worker_pid")
                if self._alive_of(record, alive):
                    return {
                        "state": STATE_EXECUTING,
                        "overdue": True,
                        "worker_alive": True,
                        "worker_pid": pid,
                        **self._killed_facts(record),
                    }
                record.pop("is_alive", None)
                record["worker_dead"] = True
                return {
                    "state": STATE_UNKNOWABLE,
                    "reason": OUTCOME_KILLED,
                    "worker_alive": False,
                    "worker_pid": pid,
                    **self._killed_facts(record),
                }
            if outcome in _UNKNOWABLE_OUTCOMES:
                # No named reason in § 6 covers a stamped abandoned/worker-lost
                # outcome; the outcome value itself is the honest reason a
                # caller gets no re-run license here.
                return {"state": STATE_UNKNOWABLE, "reason": outcome}
            return {"state": STATE_UNKNOWABLE, "reason": REASON_NO_RESIDENT_ENGINE}

    def _evict_if_needed(self) -> None:
        skipped = 0
        while len(self._order) > self._capacity and skipped < len(self._order):
            evicted_key = self._order.popleft()
            if evicted_key in self._pinned:
                self._order.append(evicted_key)
                skipped += 1
                continue
            self._records.pop(evicted_key, None)
            dropped = self._results.pop(evicted_key, None)
            if dropped is not None:
                self._results_bytes -= dropped[1]
            mint_ns = mint_ns_of(evicted_key)
            if mint_ns is not None and mint_ns > self.low_water_ns:
                self.low_water_ns = mint_ns
