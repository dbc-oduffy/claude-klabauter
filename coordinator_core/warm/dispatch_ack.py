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

DROPPED_OVER_CAP = "over-cap"
DROPPED_NOT_SERIALISABLE = "not-serialisable"
DROPPED_EVICTED = "evicted"

_ADMITTED = "admitted"
_STAMPED = "stamped"
_TOMBSTONED = "tombstoned"

STATE_NOT_RECEIVED = "not_received"
STATE_EXECUTING = "executing"
STATE_FINISHED = "finished"
STATE_UNKNOWABLE = "unknowable"

REASON_ENGINE_RESTARTED = "engine-restarted"
REASON_EXPIRED = "expired"
REASON_NO_RESIDENT_ENGINE = "no-resident-engine"

OUTCOME_RESULT = "result"
OUTCOME_ERROR = "error"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_WORKER_LOST = "worker-lost"
OUTCOME_NOT_DISPATCHED = "not-dispatched"

_FINISHED_OUTCOMES = (OUTCOME_RESULT, OUTCOME_ERROR)

#: Answers "how long past its executing bound may a still-unstamped record read
#: `executing` (overdue) before a poll reads it abandoned?" A multiple of the
#: record's own bound, never a raised op budget. 10x the 30s bound is 300s, the
#: inline executor join (`asyncio.run`), so a handler that finishes inside that
#: join is read as executing; past it the supersede rule in `stamp`, not this
#: ceiling, carries a late result.
OVERDUE_CEILING_FACTOR = 10
_UNKNOWABLE_OUTCOMES = (OUTCOME_ABANDONED, OUTCOME_WORKER_LOST)


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


class AckStore:

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        *,
        boot_ns: Optional[int] = None,
        executing_bound: Optional[Callable[[Optional[str]], float]] = None,
    ) -> None:
        """`executing_bound` maps a method to the seconds an admitted record may
        read `executing`; None means unbounded. Passed in, never imported, so
        this module stays stdlib-only."""
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
    ) -> None:
        """Terminal stamp. `result` is kept only for a `result` outcome, only if
        JSON-serialisable and within the per-entry cap; it never affects state."""
        size: Optional[int] = None
        dropped: Optional[str] = None
        digest: Optional[dict] = None
        if outcome == OUTCOME_RESULT and result is not None:
            size, dropped = self._measure(result)
            digest = _outcome_digest(result)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            if record["state"] != _ADMITTED and not (
                record["state"] == _STAMPED
                and record["outcome"] == OUTCOME_ABANDONED
                and outcome in _FINISHED_OUTCOMES
            ):
                return
            record["state"] = _STAMPED
            record["outcome"] = outcome
            record["error_code"] = error_code
            if outcome == OUTCOME_RESULT and result is not None:
                record["result_offered"] = True
                record["result_digest"] = digest
                if size is not None:
                    self._retain_result(key, result, size)
                else:
                    record["result_dropped"] = dropped
            self._changed.notify_all()

    def wait(self, key: str, timeout_secs: float) -> dict:
        """Block until `key` leaves `executing` or `timeout_secs` passes, then
        answer `status(key)`. Condition-driven: no polling, no settle interval.
        An unknown or tombstoned key answers at once."""
        deadline = time.monotonic() + max(0.0, timeout_secs)
        with self._changed:
            while True:
                record = self._records.get(key)
                if record is None or record["state"] != _ADMITTED:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
        return self.status(key)

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

    def _retain_result(self, key: str, result: Any, size: int) -> None:
        self._results[key] = (result, size)
        self._results_bytes += size
        while self._results and (
            len(self._results) > RESULT_RETAIN_COUNT or self._results_bytes > RESULT_RETAIN_BYTES
        ):
            old_key, (_, old_size) = self._results.popitem(last=False)
            self._results_bytes -= old_size
            old_record = self._records.get(old_key)
            if old_record is not None:
                old_record["result_dropped"] = DROPPED_EVICTED

    def status(self, key: str) -> dict:
        mint_ns = mint_ns_of(key)
        with self._lock:
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
                if record.get("result_offered"):
                    kept = self._results.get(key)
                    if kept is not None:
                        payload["result"] = kept[0]
                    else:
                        payload["result_digest"] = record.get("result_digest") or {}
                        payload["result_dropped"] = record.get("result_dropped", DROPPED_EVICTED)
                return payload
            if outcome in _UNKNOWABLE_OUTCOMES:
                # No named reason in § 6 covers a stamped abandoned/worker-lost
                # outcome; the outcome value itself is the honest reason a
                # caller gets no re-run license here.
                return {"state": STATE_UNKNOWABLE, "reason": outcome}
            return {"state": STATE_UNKNOWABLE, "reason": REASON_NO_RESIDENT_ENGINE}

    def _evict_if_needed(self) -> None:
        while len(self._order) > self._capacity:
            evicted_key = self._order.popleft()
            self._records.pop(evicted_key, None)
            dropped = self._results.pop(evicted_key, None)
            if dropped is not None:
                self._results_bytes -= dropped[1]
            mint_ns = mint_ns_of(evicted_key)
            if mint_ns is not None and mint_ns > self.low_water_ns:
                self.low_water_ns = mint_ns
