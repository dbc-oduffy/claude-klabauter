"""coordinator_core.hooks.support.guard_runner -- the in-process guard
runner that batches doctrine-plane-resident write-path guards inside one
interpreter.

Ported from DoE-claude `coordinator/hooks/scripts/_guard_runner.py` (18
consumers there) per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk
W4-C3. Implements `guard_runner_contract.py` (the GOVERNING SURFACE)
verbatim -- see that module's numbered clauses for the authoritative
statement of each behaviour below.

Two layers:

  1. `run_guards()` -- the pure, class-aware AGGREGATION core (contract
     clause 10) with per-entry EXCEPTION ISOLATION (clause 11). Operates on
     already-produced verdict dicts (`{"channel": ..., "text": ...}`) or
     `(name, callable)` pairs it invokes itself.

  2. `run_registered_guards()` -- the real-guard invocation wrapper:
     TWO-STAGE LAZY IMPORT (clause 12) via `GuardScopeDescriptor.matches()`
     evaluated before any guard body is imported, per-guard stdin/stdout
     /stderr capture (clause 6), translation of a guard's JSON stdout
     envelope into the verdict shape `run_guards()` aggregates, then a call
     into `run_guards()` for the actual aggregation.

REAL_GUARD_REGISTRY lands EMPTY here, deliberately, not as an omission: the
enrolled guard bodies this registry would name (`guard-oss-payload-
locality.py` and its siblings) are a LATER wave's `writes:` (W4-C5/C6), and
they land there under claude-klabauter's own underscore-named, dotted-importable
`coordinator_core/hooks/<name>.py` shape -- not DoE's hyphenated-filename,
`importlib.util.spec_from_file_location`-loaded shape this registry's
`RegisteredGuard.module_path` field assumes. Populating this registry with
entries that name files outside this chunk's footprint, in a shape the
future bodies will not actually take, would be dead and misleading code;
the wave that lands each guard body wires its own `RegisteredGuard` entry
against the shape it actually lands in.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from coordinator_core.hooks.support.guard_runner_contract import (
    CHANNEL_ADDITIONAL_CONTEXT,
    CHANNEL_DENY,
)

GuardVerdict = Dict[str, str]
GuardEntry = Union[GuardVerdict, Tuple[str, Callable[[Any], Optional[GuardVerdict]]]]


def run_guards(
    guards: Iterable[GuardEntry],
    payload: Any,
    skipped_out: Optional[List[str]] = None,
) -> dict:
    """Contract clause 10 (class-aware aggregation) + clause 11 (exception
    isolation), as one pure-ish core: no I/O beyond invoking `callable`
    entries, no sys.path/env mutation.

    At most one DENY reaches the result -- the FIRST `CHANNEL_DENY` verdict
    wins; every `CHANNEL_ADDITIONAL_CONTEXT` text (deny-channel or not)
    concatenates into one `additionalContext` string, so a deny and an
    advisory firing on the same payload both surface.

    A `(name, callable)` entry that raises `BaseException` (clause 11 is
    deliberately this broad -- a stray `SystemExit`/`KeyboardInterrupt`
    escaping guard code must not abort the batch either) has its `name`
    appended to `skipped_out` and the batch continues; `skipped_out`
    defaults to a fresh list when the caller does not supply one."""
    if skipped_out is None:
        skipped_out = []

    deny_text: Optional[str] = None
    context_parts: List[str] = []

    for entry in guards:
        if isinstance(entry, tuple):
            name, fn = entry
            try:
                verdict = fn(payload)
            except BaseException:
                skipped_out.append(name)
                continue
        else:
            verdict = entry

        if not verdict:
            continue

        channel = verdict.get("channel")
        text = verdict.get("text")
        if not text:
            continue

        if channel == CHANNEL_DENY:
            if deny_text is None:
                deny_text = text
        elif channel == CHANNEL_ADDITIONAL_CONTEXT:
            context_parts.append(text)

    result: dict = {}
    if deny_text is not None:
        result["permissionDecision"] = "deny"
        result["permissionDecisionReason"] = deny_text
    result["additionalContext"] = "\n\n".join(context_parts)
    return result


def envelope_to_verdict(out: Optional[dict]) -> Optional[GuardVerdict]:
    if not out or not isinstance(out, dict):
        return None
    hook_output = out.get("hookSpecificOutput")
    if not isinstance(hook_output, dict):
        return None
    if hook_output.get("permissionDecision") == "deny":
        return {"channel": CHANNEL_DENY, "text": hook_output.get("permissionDecisionReason") or ""}
    if "additionalContext" in hook_output:
        return {
            "channel": CHANNEL_ADDITIONAL_CONTEXT,
            "text": hook_output.get("additionalContext") or "",
        }
    return None


def verdict_to_envelope(result: dict) -> Optional[dict]:
    has_deny = result.get("permissionDecision") == "deny"
    has_context = bool(result.get("additionalContext"))
    if not has_deny and not has_context:
        return None
    hook_output: dict = {"hookEventName": "PreToolUse"}
    if has_deny:
        hook_output["permissionDecision"] = "deny"
        hook_output["permissionDecisionReason"] = result["permissionDecisionReason"]
    if has_context:
        hook_output["additionalContext"] = result["additionalContext"]
    return {"hookSpecificOutput": hook_output}


def _target_path_from_payload(payload: Any) -> Optional[str]:
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


@dataclass(frozen=True)
class RegisteredGuard:
    """One enrolled guard: where its `main()` lives (a filesystem path,
    resolved by whichever wave populates `REAL_GUARD_REGISTRY`), its
    import-free `GuardScopeDescriptor`, the `sys.modules` key the runner
    registers it under on import, and its entrypoint attribute name.

    `verdict_attr`: the STDOUT-JSON invocation path (`entry_attr` +
    `_invoke_guard_main`) is what most guards convey their verdict through.
    A guard whose deny/advisory text travels via captured STDERR instead
    sets `verdict_attr` to a module attribute matching the `GuardEntry`
    callable shape directly (`Callable[[Any], Optional[GuardVerdict]]`) --
    `build_registry_entries` calls it with the PARSED payload dict instead
    of going through `_invoke_guard_main`'s stdin/stdout swap. `None` (the
    default) preserves the stdout-JSON path unchanged."""

    module_key: str
    module_path: str
    descriptor: "object"
    entry_attr: str = "main"
    verdict_attr: Optional[str] = None


def _invoke_guard_main(main_fn: Callable[[], int], stdin_text: str) -> GuardVerdict:
    stdin_buf = io.StringIO(stdin_text)
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    old_stdin = sys.stdin
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        sys.stdin = stdin_buf
        try:
            try:
                main_fn()
            except SystemExit:
                pass
        finally:
            sys.stdin = old_stdin

    out_text = stdout_buf.getvalue().strip()
    if not out_text:
        return {}
    try:
        envelope = json.loads(out_text)
    except Exception:
        return {}
    verdict = envelope_to_verdict(envelope)
    return verdict or {}


def _import_guard_module(guard: RegisteredGuard):
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


def build_registry_entries(
    registry: Iterable[RegisteredGuard],
    raw_payload_text: str,
    payload: Any,
) -> List[Tuple[str, Callable[[Any], GuardVerdict]]]:
    target_path = _target_path_from_payload(payload)
    entries: List[Tuple[str, Callable[[Any], GuardVerdict]]] = []
    for guard in registry:
        if not guard.descriptor.matches(target_path):
            continue

        def _call(_payload: Any, _guard: RegisteredGuard = guard) -> GuardVerdict:
            module = _import_guard_module(_guard)
            if _guard.verdict_attr:
                verdict_fn = getattr(module, _guard.verdict_attr)
                return verdict_fn(_payload) or {}
            main_fn = getattr(module, _guard.entry_attr)
            return _invoke_guard_main(main_fn, raw_payload_text)

        entries.append((guard.module_key, _call))
    return entries


REAL_GUARD_REGISTRY: Tuple[RegisteredGuard, ...] = ()


def run_registered_guards(
    registry: Iterable[RegisteredGuard],
    raw_payload_text: str,
    payload: Any,
    skipped_out: Optional[List[str]] = None,
) -> dict:
    entries = build_registry_entries(registry, raw_payload_text, payload)
    return run_guards(entries, payload, skipped_out=skipped_out)
