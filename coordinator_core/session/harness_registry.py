"""
coordinator_core.session.harness_registry — pure parser over the harness's
own `<claude-config>/sessions/*.json` registry.

Spec backlink: docs/plans/2026-08-08-harness-session-registry-as-liveness-source.md § C1

Public surface (pinned contract — do not change without updating consumers):

    def registry_dir() -> Path | None
    def snapshot() -> dict[str, RegistryRecord]
    def lookup(session_id: str) -> RegistryRecord | None
    def self_record() -> tuple[str, RegistryRecord] | None
    class RegistryRecord: pid: int; start_epoch: float; cwd: str | None

`self_record()` (added `docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md`
§ C1, spike-verified anchor; cite `example-doctrine-repo@642195ba` / follow-up
`example-doctrine-repo@88929bea` — the wrongful-takeover shape this module's
negative-spec defends against) is the O(1) leg over the pid-keyed
`<registry_dir>/<CLAUDE_PID>.json` file: it resolves `CLAUDE_PID` via
`coordinator_core.session.core._resolve_claude_pid_from_env` (the one
shared resolver, never a bare `os.environ.get`) and parses that single
file via `_parse_one` — ONE `read_text`, no `glob`, no `snapshot()` call.
It returns `_parse_one`'s own `tuple[str, RegistryRecord] | None` shape
(the `str` is `sessionId`) so a caller has a `sessionId` to trust-check
against, not just a bare record. Like every other function in this
module, `self_record()` NEVER calls `core.stable_pid_alive` — it is a
pure parser with no verdict arm; the trust check belongs to the caller
(`pickup_assemble.compute_repo_identity_gate`), never here. This module's
central invariant and negative-spec (below) apply to `self_record()`
identically to `snapshot()`/`lookup()`.

`registry_dir()` resolves `<claude-config>/sessions` by routing through
`coordinator_core._settings_home.claude_config_dir()` — NEVER hand-roll
`Path.home() / ".claude"`; that duplicate computation is exactly the
split-brain defect `claude_config_dir()`'s own divergence check exists to
close.

`snapshot()` performs the ONE directory scan: it parses `sessions/*.json`
once and returns a `sessionId`-keyed dict of parsed records. `lookup(sid)`
is defined as `snapshot().get(sid)` — a convenience for the O(1)
`session_live` call site, NOT a second scan implementation. Called per-id
inside a verdicts loop, a per-call scan would be hundreds of parses per
invocation on a loaded box and would give a torn view across ids within one
pass.

THE CENTRAL INVARIANT. `RegistryRecord` is a parsed `(pid, start_epoch)`
pair, NOT a liveness verdict. `snapshot()` and `lookup()` do NOT call
`core.stable_pid_alive` — they are pure parsers. The tolerant birth-instant
compare happens at `liveness.py`'s existing single `core.stable_pid_alive`
seam (a later chunk's job, not this module's), keeping
`session/liveness.py`'s D5 single-liveness-key invariant literally true.

Negative-spec:
    - There is NO DEAD arm and no code path in this module that constructs
      one. A registry miss, a malformed file, an absent directory, a
      `sessionId` mismatch, or a `procStart` failing the sanity band below
      each yield `None` for that record — never a verdict. The caller's
      existing fallback stack decides what `None` means. An executor
      "simplifying" a `None` return into a dead verdict reintroduces a
      wrongful-takeover failure this fleet has already suffered twice
      (example-doctrine-repo 642195ba, follow-up 88929bea).
    - `updatedAt`, `statusUpdatedAt`, and `status` are read NOWHERE in this
      module. They track busy/idle transitions, not process liveness: a
      continuously-working session was measured carrying a 1465-second-old
      `updatedAt` — worse than the existing 30-minute recency window, not
      better. A grep for those three names inside `coordinator_core/session/`
      must return hits only in this block.
    - This module never calls `core.stable_pid_alive`, `psutil.pid_exists`,
      or any raw `pid`-only liveness check — see the RAW-PID-LIVENESS floor
      in `liveness.py`. The registry's `pid` is admissible only paired with
      `start_epoch` through `stable_pid_alive`, at that module's seam.
    - No caching/memoization across `snapshot()` calls — each call is a
      fresh, single scan; staleness policy belongs to the caller.

`procStart` is a Windows FILETIME — 100ns ticks since 1601-01-01 — and
converts to a Unix epoch via `int(procStart) / 1e7 - 11644473600`. A
converted epoch outside `[now - 90d, now + 1h]` is REJECTED (record yields
`None`) before it is ever returned: this turns a unit mismatch (e.g. an
unobserved POSIX record shape — see plan Anti-scope, this conversion is
verified only on Windows) into an explicit `None` rather than a coincidence
risk. Verified this session against all 22 live rows on this box:
sub-millisecond agreement with `psutil.Process(pid).create_time()` on every
one.

Defensive parsing: `OSError`, `json.JSONDecodeError`, a missing `pid` or
`procStart` key, a non-`int` `pid`, a non-numeric `procStart`, and an
out-of-band `procStart` each yield `None` for that record and skip to the
next record — these are the common, expected cases. ADDITIONALLY, both
`snapshot()` and `lookup()` catch an outermost `except Exception` at the
public boundary and degrade to `{}` / `None` respectively (AC11) — the
enumerated list above is NOT the contract's actual boundary and provably
omits real raisers reachable on this path: `registry_dir()` routes through
`_require_rooted` (raises `ValueError` on a bad/relative `CLAUDE_CONFIG_DIR`
override), `int(procStart)` can raise `OverflowError`, and `read_text` can
raise `UnicodeDecodeError`. Nothing in this module may raise to its caller
— it sits on the claim hot path, and the module it feeds
(`session/liveness.py`) already documents fail-open-never-fail-dead as its
established bias.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from coordinator_core._settings_home import claude_config_dir

_FILETIME_EPOCH_OFFSET_SEC = 11644473600.0
_FILETIME_TICKS_PER_SEC = 1e7

_SANITY_BAND_PAST_SEC = 90 * 24 * 3600
_SANITY_BAND_FUTURE_SEC = 3600


@dataclass(frozen=True)
class RegistryRecord:
    """A parsed `(pid, start_epoch, cwd)` triple — NOT a liveness verdict.

    `start_epoch` is a Unix epoch (seconds, float) already converted from the
    registry's raw Windows-FILETIME `procStart` and already validated inside
    the sanity band. Consumers pair this with `pid` through
    `core.stable_pid_alive`'s tolerant birth-instant compare — never a bare
    `pid_exists`/`kill -0` check.

    `cwd` is the session's CURRENT harness-level working directory as last
    recorded by the harness — the value `/cd` moves, not a launch-immutable
    fact — or `None` if the raw record omits it. Like `pid`/`start_epoch`,
    this is a parsed value only; it carries no repo-identity verdict of its
    own. Consumers compose a verdict over it (e.g.
    `pickup_assemble.compute_repo_identity_gate`'s containment + plausibility
    check) — this module stays a pure parser.
    """

    pid: int
    start_epoch: float
    cwd: str | None = None


def _filetime_to_epoch(raw) -> float | None:
    """Convert a raw `procStart` field to a Unix epoch, or None if invalid.

    Raises nothing — every failure mode (non-numeric, overflow, out of the
    `[now - 90d, now + 1h]` sanity band) returns None so the caller skips the
    record rather than propagating.
    """
    try:
        ticks = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        epoch = ticks / _FILETIME_TICKS_PER_SEC - _FILETIME_EPOCH_OFFSET_SEC
    except OverflowError:
        return None
    now = time.time()
    if epoch < now - _SANITY_BAND_PAST_SEC or epoch > now + _SANITY_BAND_FUTURE_SEC:
        return None
    return epoch


def registry_dir() -> Path | None:
    """Resolve `<claude-config>/sessions`, or None if it cannot be resolved.

    Routes through `_settings_home.claude_config_dir()` — never hand-rolls
    `Path.home() / ".claude"`. Never raises: a bad/relative
    `CLAUDE_CONFIG_DIR` override surfaces as `ValueError` from
    `claude_config_dir()`'s internal `_require_rooted`, caught here and
    turned into None, matching this module's fail-open contract.
    """
    try:
        return claude_config_dir() / "sessions"
    except Exception:
        return None


def _parse_one(path: Path) -> tuple[str, RegistryRecord] | None:
    """Parse a single `sessions/<x>.json` file into (sessionId, record), or None."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    session_id = data.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None

    raw_pid = data.get("pid")
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int):
        # Review: coordinator:code-reviewer — a non-int pid (e.g. a JSON
        # float) must yield None per the module docstring's "non-`int` pid"
        # negative-spec case, not silently truncate via int().
        return None
    pid = raw_pid
    if pid <= 0:
        # Review: coordinator:code-reviewer — no OS ever assigns a
        # non-positive pid; same defensive class as the FILETIME sanity band.
        return None

    start_epoch = _filetime_to_epoch(data.get("procStart"))
    if start_epoch is None:
        return None

    raw_cwd = data.get("cwd")
    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None

    return session_id, RegistryRecord(pid=pid, start_epoch=start_epoch, cwd=cwd)


def snapshot() -> dict[str, RegistryRecord]:
    """Parse every `sessions/*.json` once; return a `sessionId`-keyed dict.

    The ONE directory scan this module performs — `lookup()` is defined over
    this, never a second scan implementation. Never raises: any unexpected
    internal exception is caught at this boundary and degrades to `{}`.

    If two files in the registry directory carry the same `sessionId`, the
    later one in `Path.glob`'s OS-dependent iteration order wins (last
    writer wins, order unspecified) — acceptable given the registry is
    self-pruning. Review: coordinator:code-reviewer.
    """
    try:
        directory = registry_dir()
        if directory is None or not directory.is_dir():
            return {}

        result: dict[str, RegistryRecord] = {}
        for entry in directory.glob("*.json"):
            parsed = _parse_one(entry)
            if parsed is None:
                continue
            session_id, record = parsed
            result[session_id] = record
        return result
    except Exception:
        return {}


def lookup(session_id: str) -> RegistryRecord | None:
    """Return the registry record for `session_id`, or None.

    Defined as `snapshot().get(session_id)` — a convenience over the single
    scan, not a second scan. Never raises: any unexpected internal exception
    is caught at this boundary and degrades to None.
    """
    try:
        return snapshot().get(session_id)
    except Exception:
        return None


def self_record() -> tuple[str, RegistryRecord] | None:
    """Return this session's own registry record, or None.

    The O(1) leg: reads `<registry_dir>/<CLAUDE_PID>.json` directly, via
    `_parse_one` — ONE `read_text`, no `glob`, no `snapshot()` scan. This is
    what keeps a caller's zero-spawn claim true (a single Windows FILETIME
    file read only). `CLAUDE_PID` is resolved via the ONE shared resolver,
    `coordinator_core.session.core._resolve_claude_pid_from_env` — never a
    bare `os.environ.get('CLAUDE_PID')` — so an absent, non-integer, or
    non-positive value is rejected by that resolver's own established
    validation rather than growing a second, divergent parse here.

    Returns `_parse_one`'s own `tuple[str, RegistryRecord] | None` shape
    (the `str` is `sessionId`) — NOT a bare `RegistryRecord` — so a caller
    performing a trust check (e.g. `pickup_assemble.compute_repo_identity_
    gate`'s AC10 cross-check) has a `sessionId` to compare against on this
    pid-keyed leg. Routing through `_parse_one` also guarantees this leg's
    FILETIME sanity band and non-int-pid/pid<=0 rejections cannot diverge
    from `snapshot()`'s fallback leg — the split-brain this module's
    docstring forbids.

    Absent `CLAUDE_PID`, an unresolvable registry dir, an unreadable file,
    or a malformed record each yield None — this module's established
    fail-open-never-fail-dead bias. Like every other function here, this
    NEVER calls `core.stable_pid_alive` — no verdict arm, pure parser only.
    Never raises: any unexpected internal exception is caught at this
    boundary and degrades to None (AC11-shaped boundary, matching
    `snapshot()`/`lookup()`).
    """
    try:
        from coordinator_core.session.core import _resolve_claude_pid_from_env

        match, _reason = _resolve_claude_pid_from_env()
        if match is None:
            return None
        pid, _create_time = match

        directory = registry_dir()
        if directory is None:
            return None

        return _parse_one(directory / f"{pid}.json")
    except Exception:
        return None
