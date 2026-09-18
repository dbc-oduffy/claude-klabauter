"""coordinator_core.hooks.support.stop_family_runner -- the in-process
runner for the four Stop-family PostToolUse write-path guards, implementing
`stop_family_runner_contract.py` (the GOVERNING SURFACE) verbatim -- see
that module's numbered clauses for the authoritative statement of each
behaviour below.

Ported from DoE-claude `coordinator/hooks/scripts/_stop_family_runner.py`
per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C3.

Deliberately NOT built on top of `guard_runner.py`'s internals (no shared
base class, no imported helper functions from that module) -- see
`stop_family_runner_contract.py`'s own docstring for why this is a SIBLING
mechanism, not an extension: the aggregation problem (concatenate-all N
independent stderr-emitters into one combined stderr write + one exit code)
is different in kind from `guard_runner.py`'s class-aware DENY-vs-
additionalContext combining, not a parametrisation of it. The two-stage
lazy-import shape is structurally similar by necessity -- both runners
solve "N sibling scripts, one process, don't pay every import" -- but is
reimplemented here rather than shared, so the two mechanisms can evolve
independently without one change rippling into an unrelated protocol.

REAL_STOP_FAMILY_REGISTRY lands EMPTY here, for the identical reason
`guard_runner.REAL_GUARD_REGISTRY` does -- see that module's own docstring.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional, Tuple

from coordinator_core.hooks.support.guard_runner_contract import GuardScopeDescriptor


@dataclass(frozen=True)
class RegisteredStopFamilyGuard:
    """One enrolled Stop-family guard: where its `main()` lives, its
    import-free `GuardScopeDescriptor`, and the `sys.modules` key it is
    registered under on import. Mirrors `guard_runner.RegisteredGuard`'s
    shape (same fields, same purpose) without importing that class -- see
    this module's own docstring for why the two runners stay independent."""

    module_key: str
    module_path: str
    descriptor: GuardScopeDescriptor


class _ByteSink:
    """Binary-mode facade for `_BufferedTextCapture.buffer`: writes bytes
    straight through, UNMODIFIED, into the SAME ordered `io.BytesIO` the
    text channel's `write(str)` encodes into -- no decode, no round-trip at
    capture time. This is the byte-exactness half of the fix (the ordering
    half is: one shared sink, not two separately-accumulated buffers -- see
    `_BufferedTextCapture`'s own docstring). Byte-exactness matters here
    specifically because guards routing their emission through
    `message_envelope.emit()` write raw UTF-8 bytes via
    `sys.stderr.buffer.write()` PRECISELY to bypass Python's Windows
    text-mode CRLF translation -- a capture layer that decoded those bytes
    into a `str` mid-flight would still be lossless (UTF-8 decode/encode
    round-trips exactly), but the dispatcher-side RE-EMISSION this capture
    feeds must also avoid a text-mode `sys.__stderr__.write(str)`, or the
    CRLF translation this convention exists to avoid gets reintroduced one
    hop later."""

    def __init__(self, sink: "io.BytesIO") -> None:
        self._sink = sink

    def write(self, data: bytes) -> int:
        return self._sink.write(data)

    def flush(self) -> None:
        pass


class _BufferedTextCapture(io.StringIO):
    """Stand-in for `sys.stderr` under `contextlib.redirect_stderr` that
    also exposes a `.buffer` (a `_ByteSink`) -- see
    `_invoke_stop_guard_main`'s own docstring for why this is required (a
    guard may write via `sys.stderr.buffer.write()`, which a plain
    `io.StringIO` has no attribute for). Both channels land in ONE ordered
    `io.BytesIO` -- `write(str)` (the `print()`/`sys.stderr.write()` path)
    encodes into it, `.buffer.write(bytes)` (the raw-bytes path) writes into
    it unmodified -- so `combined()`/`combined_bytes()` return whatever a
    guard emitted across either channel, in true emission order and
    byte-exact, rather than concatenating two separately-accumulated
    buffers (which would silently reorder mixed-channel output). A folded
    guard that only ever uses one channel per invocation is unaffected
    either way; this fix is what keeps a guard that mixes both channels
    correct too."""

    def __init__(self) -> None:
        super().__init__()
        self._bytes = io.BytesIO()
        self.buffer = _ByteSink(self._bytes)

    def write(self, s: str) -> int:
        self._bytes.write(s.encode("utf-8"))
        return len(s)

    def combined(self) -> str:
        return self.combined_bytes().decode("utf-8", "replace")

    def combined_bytes(self) -> bytes:
        return self._bytes.getvalue()

    def getvalue(self) -> str:
        """Defensive override: the base `io.StringIO.getvalue()` would read
        this instance's OWN internal text buffer, which `write()` above
        deliberately never populates (everything routes through
        `self._bytes` instead, so both channels share one ordered sink).
        Nothing in this module calls `getvalue()` directly today --
        `combined()`/`combined_bytes()` are the contract -- but leaving the
        inherited method unrouted would silently return empty text to any
        future caller that reaches for it out of `io.StringIO` habit."""
        return self.combined()


def _target_path_from_payload(payload: Any) -> Optional[str]:
    """Cheap, import-free extraction of the edited path from a raw
    PostToolUse payload dict. Identical shape to `guard_runner.
    _target_path_from_payload` (Write/Edit/MultiEdit/NotebookEdit all nest
    the path under `tool_input`), reimplemented locally rather than
    imported for the same independence reason as the rest of this module."""
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _import_guard_module(guard: RegisteredStopFamilyGuard):
    """Stage-two import (contract clause 6): only reached once
    `guard.descriptor.matches(target_path)` is already `True`. Same
    `importlib.util.spec_from_file_location` technique as `guard_runner.
    _import_guard_module`, for the same reason (a guard's `module_path`
    need not be a valid dotted-import name)."""
    if guard.module_key in sys.modules:
        return sys.modules[guard.module_key]
    spec = importlib.util.spec_from_file_location(guard.module_key, guard.module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load guard module at {guard.module_path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[guard.module_key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(guard.module_key, None)
        raise
    return module


def _invoke_stop_guard_main(main_fn: Callable[[], int], stdin_text: str) -> Tuple[int, str]:
    """Runs one guard's `main()` with stdin swapped and stderr captured
    (contract clause 3) -- mirrors `guard_runner._invoke_guard_main`'s
    stdio-swap technique, but captures STDERR (this protocol's real
    channel) instead of stdout, and returns the RAW `(exit_code,
    stderr_text)` pair rather than translating into a verdict shape (there
    is only one shape here, so no translation step is needed).

    Captures via `_BufferedTextCapture`, not a plain `io.StringIO()`: a
    guard may route its CHANNEL_STOP emission through
    `message_envelope.emit()`, which deliberately writes via
    `sys.stderr.buffer.write()` (raw UTF-8 bytes, bypassing Python's
    Windows text-mode CRLF translation) rather than `sys.stderr.write()`. A
    plain `io.StringIO` has no `.buffer` attribute and raises
    `AttributeError` the instant such a guard fires.

    Catches `SystemExit` defensively (contract clause 1 says a guard's
    `main()` must not use it for control flow, but the runner never trusts
    that by assumption alone) -- an uncaught `SystemExit` is treated as
    `return 0` with whatever was captured on stderr up to that point, an
    ultra-conservative choice."""
    stdin_buf = io.StringIO(stdin_text)
    stderr_buf = _BufferedTextCapture()
    old_stdin = sys.stdin
    exit_code = 0
    with contextlib.redirect_stderr(stderr_buf):
        sys.stdin = stdin_buf
        try:
            try:
                exit_code = main_fn()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 0
        finally:
            sys.stdin = old_stdin
    return exit_code, stderr_buf.combined()


def build_stop_family_entries(
    registry: Iterable[RegisteredStopFamilyGuard],
    raw_payload_text: str,
    payload: Any,
) -> List[Tuple[str, Callable[[], Tuple[int, str]]]]:
    """Two-stage lazy import (contract clause 6), realised as a list of
    `(name, callable)` entries `run_stop_family_guards` invokes directly. A
    guard whose descriptor does NOT match `payload`'s target path never
    appears here -- its module is never imported."""
    target_path = _target_path_from_payload(payload)
    entries: List[Tuple[str, Callable[[], Tuple[int, str]]]] = []
    for guard in registry:
        if not guard.descriptor.matches(target_path):
            continue

        def _call(_guard: RegisteredStopFamilyGuard = guard) -> Tuple[int, str]:
            module = _import_guard_module(_guard)
            main_fn = getattr(module, "main")
            return _invoke_stop_guard_main(main_fn, raw_payload_text)

        entries.append((guard.module_key, _call))
    return entries


def run_stop_family_guards(
    entries: Iterable[Tuple[str, Callable[[], Tuple[int, str]]]],
    skipped_out: Optional[List[str]] = None,
) -> Tuple[int, str]:
    """The pure aggregation core (contract clauses 4 + 5): CONCATENATE-ALL,
    never first-fires-wins. Every entry that raises `BaseException`
    (clause 5's exception isolation -- deliberately this broad, mirroring
    `guard_runner.run_guards`) has its name appended to `skipped_out` and
    the batch continues; `skipped_out` defaults to a fresh list.

    Returns `(combined_exit_code, combined_stderr_text)`:
      - `combined_exit_code` is `2` if ANY entry returned `2`, else `0`.
      - `combined_stderr_text` is every FIRED entry's captured stderr text
        (exit code `2` only), joined with a blank line between each, in
        entry order -- never just the first."""
    if skipped_out is None:
        skipped_out = []

    fired_texts: List[str] = []
    any_fired = False

    for name, fn in entries:
        try:
            exit_code, text = fn()
        except BaseException:
            skipped_out.append(name)
            continue

        if exit_code == 2:
            any_fired = True
            if text:
                fired_texts.append(text.rstrip("\n"))

    combined_exit = 2 if any_fired else 0
    combined_text = "\n\n".join(fired_texts)
    return combined_exit, combined_text


#: Enrolment registry: populated by whichever later wave lands the four
#: Stop-family guard bodies this registry enrols. Left empty here -- see
#: this module's own docstring, and `guard_runner.REAL_GUARD_REGISTRY`'s
#: identical reasoning, for why an empty registry is the correct landing
#: state for this chunk.
REAL_STOP_FAMILY_REGISTRY: Tuple[RegisteredStopFamilyGuard, ...] = ()


def run_registered_stop_family_guards(
    registry: Iterable[RegisteredStopFamilyGuard],
    raw_payload_text: str,
    payload: Any,
    skipped_out: Optional[List[str]] = None,
) -> Tuple[int, str]:
    """The dispatcher-facing entrypoint: two-stage lazy import
    (`build_stop_family_entries`) feeding the concatenate-all aggregation
    core (`run_stop_family_guards`). A combined dispatcher writes
    `combined_text` to stderr and exits with `combined_exit_code` exactly
    once, regardless of how many of the enrolled guards fired."""
    entries = build_stop_family_entries(registry, raw_payload_text, payload)
    return run_stop_family_guards(entries, skipped_out=skipped_out)
