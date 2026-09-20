"""
coordinator_core.testing.state_write_audit — runtime census instrument (D3).

Purpose: the disposition gate (`coordinator_core/tests/test_raw_writes_have_a_
disposition.py`) proves that every raw-write-holding module has a recorded
decision. It does not, and cannot, prove that a `claims-explicitly` module has
no SECOND, unclaimed write, or that a write reached through an indirection one
call away from its literal target is attributed correctly. This module is the
runtime half of the census (D3 in
`docs/plans/2026-09-11-state-writers-claim-through-one-seam.md`): a single
`sys.addaudithook` that turns "which module writes under `state/`?" into
ground truth for one measured run, over the module boundaries a byte-level
grep cannot see through.

WHERE THE EVENTS COME FROM

`sys.addaudithook` (PEP 578), filtered to exactly two events: `open` and
`os.rename`. CPython raises `open` for every `open()`/`os.open()`/`io.open()`
call (which is also what `os.fdopen`, `Path.write_text`/`write_bytes`, and
`mkstemp`-based atomic-replace helpers bottom out in), and `os.rename` for
`os.rename`/`os.replace`/`Path.rename`/`Path.replace`. The precedent for a
process-wide audit hook filtered to a frozenset of event names, with an
early-return before any per-event work, is
`coordinator_core/telemetry/spawn_counter.py` — read that module first before
touching this one; its negative-spec on audit-hook cost applies here
unchanged (an audit hook fires for EVERY audited event in the process, so the
filter must be the very first thing the callback does).

`classify_event(event, args, frames) -> record | None` is a PURE function: it
takes the raw `(event, args)` pair plus an already-collected sequence of
calling-frame filenames (innermost first) and returns a JSON-serialisable
record, or `None` if the event is not a write, is not under a `state` path
component, or attributes to no `coordinator_core` non-test frame. Frame
collection (walking `sys._getframe`) lives ONLY in the hook callback, never in
`classify_event`, so this module's own tests exercise the classifier over
synthetic tuples and never install the global hook.

OPT-IN, NEVER STANDING

This is a pytest plugin loaded ONLY via `-p
coordinator_core.testing.state_write_audit` on an explicit invocation. It is
never added to `addopts` and never imported from a `conftest.py` — CPython
audit hooks cannot be removed once installed (deliberate, PEP 578), so a
standing install would tax every `open()` call in every future test run for a
question the census asks once. `pytest_configure` here installs the hook
unconditionally when this module IS loaded as a plugin; the opt-in guarantee
is that nothing in this repo loads it except a `-p` invocation naming it by
module path.

OUTPUT

Each worker (or the lone process, when not running under xdist) appends one
JSON object per line to `<--state-write-audit-out>/<worker-id>.jsonl`,
default `.coordinator-local/state-write-audit/` — gitignored; C2's own run
confirmed this with `git check-ignore -v` before writing anything there. The
file descriptor is opened once, BEFORE the hook is installed, and every
record write goes through `os.write(fd, ...)` directly — never a second
`open()` — because a write made through this module's own `open()` call
inside the callback would recurse through the very hook it is running in.

Negative-spec:
    - The hook callback never raises past its own `try`: a broken audit
      instrument must not break the pytest run it is instrumenting, which
      here is the run being measured.
    - `classify_event` does not resolve symlinks, does not know about
      `.gitignore`, and does not decide disposition. It answers exactly one
      question: did a `coordinator_core` non-test frame write under a path
      with a `state` component, during this one audited event?
    - Frames belonging to this module, `coordinator_core/session/claimed_
      write.py`, `coordinator_core/atomic_append.py` and `coordinator_core/
      atomic_replace.py` are never picked as the attributed writer — a claim
      or a delegated primitive is not the module whose disposition is being
      asked about. `test_*.py`/`conftest.py` frames and anything under a
      `tests/`, `testing/` or `benchmarks/` directory are excluded the same
      way census row 2's own population script excludes them.
    - This module does not install anything at import time. Only
      `pytest_configure` does, and only when pytest itself loads this module
      as a plugin.
    - There is no standing gate here. A red test in the one C2 run is
      measurement to record, never a fix to make before the run counts.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md § D3 (C2)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional, Sequence

__all__ = ["classify_event", "pytest_addoption", "pytest_configure", "pytest_unconfigure"]

#: Mode characters that mean "this open call can write". `r`/`rb`/`rt` alone
#: never appear here; `+` covers `r+`/`w+`/`a+`/`x+`, all of which can write.
_WRITE_MODE_CHARS = frozenset({"w", "a", "x", "+"})

#: `os.open`'s write-implying flag bits. `O_RDONLY` is 0 and is deliberately
#: absent — a flags value with none of these bits set is a read.
_WRITE_O_FLAG_BITS = (
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
)

#: Basenames that are never the attributed writer even though they live under
#: `coordinator_core` and hold the raw primitives themselves: this plugin
#: (would otherwise attribute its own record-append `os.write` frame — moot
#: since `os.write` is unaudited, but named for the reader), the claiming
#: seam, and the two primitives it delegates to. See D1/D3 in the plan.
_NEVER_ATTRIBUTED_BASENAMES = frozenset(
    {
        "state_write_audit.py",
        "claimed_write.py",
        "atomic_append.py",
        "atomic_replace.py",
    }
)

_DEFAULT_OUT_DIR = ".coordinator-local/state-write-audit"

_hook_installed = False
_out_fd: Optional[int] = None


def _has_state_component(path: str) -> bool:
    """Whether `path` has a literal `state` path component (D3's filter)."""
    normalized = str(path).replace("\\", "/")
    return "state" in normalized.split("/")


def _is_test_like_frame(normalized: str) -> bool:
    basename = normalized.rsplit("/", 1)[-1]
    if basename.startswith("test_") or basename == "conftest.py":
        return True
    parts = normalized.split("/")[:-1]
    return any(part in ("tests", "testing", "benchmarks") for part in parts)


def _is_attributable_coordinator_core_frame(filename: str) -> bool:
    """Whether `filename` is a `coordinator_core` non-test frame D3 will attribute to."""
    normalized = str(filename).replace("\\", "/")
    if "coordinator_core/" not in (normalized + "/"):
        # Accept both "coordinator_core/..." and ".../coordinator_core/...".
        if not normalized.startswith("coordinator_core/") and "/coordinator_core/" not in normalized:
            return False
    basename = normalized.rsplit("/", 1)[-1]
    if basename in _NEVER_ATTRIBUTED_BASENAMES:
        return False
    if _is_test_like_frame(normalized):
        return False
    return True


def classify_event(
    event: str, args: Sequence[Any], frames: Sequence[str]
) -> Optional[dict]:
    """Classify one audited `open`/`os.rename` event as a state write, or not.

    `args` is the raw audit-hook argument tuple for `event`
    (`(path, mode, flags)` for `open`; `(src, dst)` for `os.rename`, which also
    covers `os.replace`/`Path.rename`/`Path.replace`). `frames` is a sequence
    of calling-frame filenames, innermost first, as the hook callback collects
    them via `sys._getframe` — never collected here, so this function is
    exercised over synthetic tuples with no interpreter frame walking.

    Returns `None` for a read, a non-`state` path, or a write with no
    attributable `coordinator_core` non-test frame anywhere on the stack.
    Otherwise returns a JSON-serialisable record naming the event, the target
    path, and the innermost/outermost attributable frames (which may be the
    same frame, when only one qualifies).
    """
    if event == "open":
        if len(args) < 1:
            return None
        path = args[0]
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        is_write = False
        if isinstance(mode, str) and any(c in _WRITE_MODE_CHARS for c in mode):
            is_write = True
        if isinstance(flags, int) and (flags & _WRITE_O_FLAG_BITS):
            is_write = True
        if not is_write:
            return None
    elif event == "os.rename":
        if len(args) < 2:
            return None
        path = args[1]
        is_write = True
        mode = None
        flags = None
    else:
        return None

    if path is None or not _has_state_component(str(path)):
        return None

    inner_frame = None
    outer_frame = None
    for filename in frames:
        normalized = str(filename).replace("\\", "/")
        if _is_attributable_coordinator_core_frame(normalized):
            if inner_frame is None:
                inner_frame = normalized
            outer_frame = normalized

    if inner_frame is None:
        return None

    return {
        "event": event,
        "path": str(path),
        "mode": mode,
        "flags": flags,
        "inner_frame": inner_frame,
        "outer_frame": outer_frame,
    }


def _collect_frame_filenames() -> Sequence[str]:
    """Walk the caller's stack, innermost first, collecting `co_filename`s.

    Starts at the audit-hook callback's own caller (skipping this function and
    `_audit_hook` itself) so the collected frames begin at whatever Python code
    made the audited call.
    """
    frames = []
    frame = sys._getframe(2) if hasattr(sys, "_getframe") else None
    while frame is not None:
        frames.append(frame.f_code.co_filename)
        frame = frame.f_back
    return frames


def _audit_hook(event: str, args: tuple) -> None:
    """`sys.addaudithook` callback. Hot for EVERY audited event in the process.

    Filters on event name first (an identity/frozenset-membership compare,
    same discipline as `telemetry/spawn_counter.py`) before doing any frame
    walk or classification, and never raises past its own `try` — a broken
    audit instrument must not break the run it is instrumenting.
    """
    if event != "open" and event != "os.rename":
        return
    try:
        frames = _collect_frame_filenames()
        record = classify_event(event, args, frames)
        if record is None:
            return
        line = json.dumps(record, sort_keys=True) + "\n"
        if _out_fd is not None:
            os.write(_out_fd, line.encode("utf-8", errors="replace"))
    except Exception:
        return


def pytest_addoption(parser) -> None:
    group = parser.getgroup("state-write-audit")
    group.addoption(
        "--state-write-audit-out",
        action="store",
        default=_DEFAULT_OUT_DIR,
        help=(
            "Directory the state-write-audit plugin appends one JSONL file "
            "per worker to. Default: " + _DEFAULT_OUT_DIR
        ),
    )


def pytest_configure(config) -> None:
    """Install the one-shot audit hook. Only runs when pytest loads THIS module as a plugin."""
    global _hook_installed, _out_fd
    if _hook_installed:
        return
    out_dir = config.getoption("state_write_audit_out")
    os.makedirs(out_dir, exist_ok=True)
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    out_path = os.path.join(out_dir, f"{worker_id}.jsonl")
    # Opened BEFORE the hook is installed: a record write inside the hook
    # uses this already-open fd via os.write, never a second open() call,
    # which would otherwise recurse through the hook it is running in.
    _out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        sys.addaudithook(_audit_hook)
    except Exception:
        if _out_fd is not None:
            os.close(_out_fd)
            _out_fd = None
        return
    _hook_installed = True


def pytest_unconfigure(config) -> None:
    """Flush and close the output fd. Never removes the audit hook (cannot be removed)."""
    global _out_fd
    if _out_fd is not None:
        try:
            os.close(_out_fd)
        except Exception:
            pass
        _out_fd = None


def audit_hook_installed() -> bool:
    """Whether `pytest_configure` installed the hook in this process."""
    return _hook_installed
