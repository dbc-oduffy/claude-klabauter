"""`AckStore` -- the accept-process record that a MUTATING dispatch was asked
before it ran, so `warm.request_status` can answer a poll without reading
side effects.

Implements `coordinator_core/contract/dispatch-ack-reconcile-contract.md`
(D1-D5). This module imports only the stdlib -- it is loaded by both the
accept process and, indirectly, by tests that construct a store in
isolation.
"""

from __future__ import annotations

import itertools
import json
import os
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


def _journal_file(journal_dir: Any, key: Any) -> Optional[str]:
    if not journal_dir or not isinstance(key, str) or not key or not set(key) <= _JOURNAL_SAFE_CHARS:
        return None
    return os.path.join(os.fspath(journal_dir), key + _JOURNAL_SUFFIX)


def _land_file(journal_dir: Any, key: Any) -> Optional[str]:
    path = _journal_file(journal_dir, key)
    return None if path is None else path[: -len(_JOURNAL_SUFFIX)] + _LAND_SUFFIX


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
    path = _journal_file(journal_dir, key)
    if path is None or outcome not in _FINISHED_OUTCOMES or os.path.exists(path):
        return path is not None and os.path.exists(path)
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
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.fspath(journal_dir), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(entry, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError, RecursionError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def read_outcome(journal_dir: Any, key: str) -> Optional[dict]:
    """The `finished` poll payload journaled for `key`, or None. Never raises."""
    path = _journal_file(journal_dir, key)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, ValueError, RecursionError):
        return None
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
    path = _land_file(journal_dir, key)
    if path is None or not isinstance(record, dict):
        return False
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.fspath(journal_dir), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError, RecursionError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def read_land_intent(journal_dir: Any, key: str) -> Optional[dict]:
    """The land record written for `key`, or None. Never raises."""
    path = _land_file(journal_dir, key)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def prune_journal(journal_dir: Any, now: Optional[float] = None) -> None:
    """Delete journal entries older than JOURNAL_RETAIN_SECS. Never raises."""
    cutoff = (time.time() if now is None else now) - JOURNAL_RETAIN_SECS
    try:
        with os.scandir(os.fspath(journal_dir)) as entries:
            for entry in entries:
                try:
                    if entry.name.endswith((_JOURNAL_SUFFIX, _LAND_SUFFIX, ".tmp")) and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


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
        if journal_dir:
            prune_journal(journal_dir)
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
        #: eviction and result-LRU eviction skip them while the set is within
        #: RESULT_RETAIN_COUNT; a caller that timed out reads the result later.
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
        if self._journal_dir and outcome in _FINISHED_OUTCOMES:
            record_outcome(
                self._journal_dir, key, outcome,
                result=result, error_code=error_code, error_message=error_message,
            )
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
            for stale in ("is_alive", "worker_pid", "worker_dead"):
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
            self._changed.notify_all()

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

    def _pin(self, key: str) -> None:
        """Pin a finished key until it is read. Past RESULT_RETAIN_COUNT pinned the
        oldest is unpinned and its result dropped (`evicted`), never silently."""
        self._pinned[key] = None
        while len(self._pinned) > RESULT_RETAIN_COUNT:
            oldest, _ = self._pinned.popitem(last=False)
            self._drop_result(oldest)

    def _retain_result(self, key: str, result: Any, size: int) -> None:
        self._results[key] = (result, size)
        self._results_bytes += size
        while self._results and (
            len(self._results) > RESULT_RETAIN_COUNT or self._results_bytes > RESULT_RETAIN_BYTES
        ):
            victim = next((k for k in self._results if k not in self._pinned and k != key), None)
            if victim is None:
                # Only pinned results (and this one) remain: the oldest pinned goes.
                victim = next(iter(self._results))
                self._pinned.pop(victim, None)
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
        answer = self._status_in_memory(key, expect_engine)
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

    def _status_in_memory(self, key: str, expect_engine: Optional[dict] = None) -> dict:
        """The in-memory answer. `expect_engine` is the delivering engine the
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
                    return {"state": STATE_UNKNOWABLE, "reason": REASON_ENGINE_RESTARTED}
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
                alive = False
                probe = record.get("is_alive")
                if probe is not None:
                    try:
                        alive = bool(probe())
                    except Exception:  # noqa: BLE001 -- a probe that cannot answer reads dead
                        alive = False
                if alive:
                    return {
                        "state": STATE_EXECUTING,
                        "overdue": True,
                        "worker_alive": True,
                        "worker_pid": pid,
                    }
                record.pop("is_alive", None)
                record["worker_dead"] = True
                return {
                    "state": STATE_UNKNOWABLE,
                    "reason": OUTCOME_KILLED,
                    "worker_alive": False,
                    "worker_pid": pid,
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
