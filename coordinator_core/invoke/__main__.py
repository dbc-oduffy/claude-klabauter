"""
coordinator_core.invoke.__main__ — Generic in-process op dispatcher.

Purpose: thin CLI entrypoint for command-type dispatch of any registered
coordinator_core op.  Builds a JSON-RPC 2.0 request envelope and calls
dispatch_message directly — no socket, no service loop.

Invocation:
    coordinator_core.invoke <op> '<json-params>' [--repo <path>]
    coordinator_core.invoke <op> --params-file <path|-> [--repo <path>]

    <op>              — JSON-RPC method name (e.g. "coverage.gate")
    <json-params>     — JSON object string of op params (default: '{}').
                         Mutually exclusive with --params-file.
    --params-file PATH — Read JSON object params from a file instead of argv,
                         or from stdin when PATH is "-". ARG_MAX-safe
                         transport for large params payloads (e.g.
                         ceremony.wsc_commit's resolved_state round-trip can
                         exceed argv limits, notably on Windows/msys), and —
                         via "-" fed by a quoted heredoc — the only transport
                         whose correctness does not depend on the payload's
                         own bytes being shell-safe. Prefer it for any payload
                         carrying free text (a commit message, a memo body):
                         an apostrophe inside a single-quoted argv payload
                         terminates the quoted span in the SHELL, so the
                         payload never reaches this process intact. Both the
                         stdin and file forms decode as explicit UTF-8 (never
                         the platform locale codec), so this transport is
                         quoting-immune AND encoding-stable for arbitrary
                         UTF-8 free text — it does not, on its own, make an
                         op's own params schema or handler logic safe for any
                         payload shape. Blocks on stdin if "-" is passed
                         without a redirect (e.g. run interactively with no
                         heredoc); pair it with a heredoc, never invoke bare.
                         Mutually exclusive with the positional params_json.
    --repo <path>     — Explicit repo root.  If omitted, resolved via
                         coordinator_core.lifecycle.find_repo_root over cwd.
                         Implicit os.getcwd() fallback is PROHIBITED (AC-5).
                         Meaningless — and REFUSED, exit non-zero — on a
                         "none"-scoped op (op_scopes.py): such an op never reads
                         repo_root, so the flag would otherwise silently no-op.
                         DR-279.
    --dump-op-timeouts — Read-only surface: print
                         {"<op>": <float>, ..., "__default__": <live
                         DISPATCH_TIMEOUT_SECS>, "__ceremony_budget__":
                         <ipc.CEREMONY_BUDGET_SECS>} to stdout and exit 0. No
                         <op> required. Lets an external caller (e.g. DoE's
                         cc_invoke, which applies a flat 10s cap) read
                         claude-klabauter's real per-op dispatch-timeout budgets instead
                         of guessing. Every `ceremony.*` op is projected
                         EXPLICITLY at the 2s ceremony budget rather than left
                         to fall through to "__default__" (30s) — a caller
                         sizing its kill ceiling off the default would wait on
                         a request the engine already abandoned at 2s.
                         Source of truth is coordinator_core.ipc.
                         OP_TIMEOUT_OVERRIDES / DISPATCH_TIMEOUT_SECS /
                         CEREMONY_BUDGET_SECS, re-read live at call time (this
                         process is spawn-per-call per DR-215, so there is no
                         stale-value risk, but DISPATCH_TIMEOUT_SECS is
                         env-resolved at import time within THIS process, so
                         reading it here reflects the actual env this process
                         saw). OP_TIMEOUT_OVERRIDES is a value-snapshot of
                         _timeout_for()'s own _OP_TIMEOUT_OVERRIDES (taken once
                         at ipc.py import, not a live view of it) — true today
                         because both are empty, but this dump would not
                         reflect a hypothetical post-import mutation of the
                         private table that _timeout_for() itself would see.
                         Precedence: this flag takes priority over <op> — if
                         both are passed (e.g. "ping --dump-op-timeouts"),
                         <op> is silently ignored and the dump is printed.
                         Spec backlink:
                         cross-repo/inbox/2026-07-18-claude-central-em-cc-invoke-op-timeout-dump-surface.md

Exit codes:
    0 — JSON-RPC success result printed to stdout.
    1 — JSON-RPC error result printed to stdout (generic/transient op-level error —
        may or may not recur on retry), or fatal pre-dispatch error (JSON error
        envelope) printed to stderr.
    2 — JSON-RPC error result printed to stdout, error.code ==
        coordinator_core.ipc.STRUCTURAL_PIN_ERROR — the op handler raised a
        structurally-wedged contract-pin error (e.g. ContractPinError: claude-klabauter's own
        CONTRACT_VERSION disagrees with the vendored cockpit-contract bundle). Distinct
        from exit 1 because this class of failure is NOT transient: it recurs on every
        subsequent invocation until the pin is remediated (re-vendor, or bump
        CONTRACT_VERSION to match) — a caller must not treat it as a one-off/skip-and-
        retry condition the way it may treat a generic exit 1.
        See coordinator_core.ops.emit.validate.ContractPinError.structurally_wedged and
        cross-repo/inbox/2026-07-22-example-cockpit-repo-em-cockpit-contract-version-desync-wedges-emit-cadence.md.

Import safety: module-level imports are stdlib only.  All coordinator_core
imports are deferred inside main() so that ``import coordinator_core.invoke``
has no side effects.

DR backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
Spec backlink: coordinator_core/invoke/__main__.py (this file) — chunk C5 remainder
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple


def _resolved_sid() -> Optional[str]:
    """Session id for a telemetry row, or None.

    A CPU sample that cannot be joined to a session cannot be ranked within one,
    which is what made the sink unable to answer "what does fire #1 of a session
    cost" in process time -- see `ipc._telemetry_sid`, the same resolve at the
    other four dispatch sites. Never raises: a null sid costs a row's
    joinability, a raise would cost the caller's dispatch.
    """
    try:
        from coordinator_core.session.core import resolve_session_id

        return resolve_session_id() or None
    except Exception:
        return None


def _record_dispatch_process_time(msg: dict, t_start: float, process_start: float) -> None:
    """Append one process-time sample for this module's cold dispatch branch.

    The brightline is stated in process time and spawn count, never wall clock,
    so an op whose only entry point is this module cannot be read against that
    budget at all until a sample lands here. Delegates every append-discipline
    concern -- kill switch, route stamping, repo-key resolution, one atomic
    append, never raises -- to `ipc.record_op_process_time`, exactly as the
    other four dispatch sites do; nothing about the sink's shape is re-derived.

    Never raises, and never delays the response: a telemetry failure on this
    path costs one missing row, never the caller's envelope. Imports are
    function-local because this runs only on the cold branch -- a warm hit
    returns before reaching it and must not pay for `ipc`'s import closure.
    """
    try:
        import time as _time

        from coordinator_core.ipc import (
            MEASUREMENT_SCOPE_PROCESS_WIDE,
            record_op_process_time,
            resolve_caller_cwd,
            resolve_request_repo,
        )

        method = msg.get("method") if isinstance(msg, dict) else None
        record_op_process_time(
            op=method if isinstance(method, str) else "<unknown>",
            process_ms=(_time.process_time() - process_start) * 1000.0,
            measurement_scope=MEASUREMENT_SCOPE_PROCESS_WIDE,
            source_path="invoke_cli",
            t_start=t_start,
            repo_root=resolve_request_repo(msg) or resolve_caller_cwd(msg),
            sid=_resolved_sid(),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coordinator_core.invoke",
        description="In-process dispatch of any registered coordinator_core op.",
    )
    p.add_argument(
        "op",
        nargs="?",
        default=None,
        help=(
            "JSON-RPC method name (e.g. coverage.gate, fleet.archive_completed_plans). "
            "Not required when --dump-op-timeouts is passed."
        ),
    )
    p.add_argument(
        "params_json",
        nargs="?",
        default=None,
        help=(
            "JSON object string of op params (default: '{}'). "
            "Mutually exclusive with --params-file."
        ),
    )
    p.add_argument(
        "--params-file",
        metavar="PATH",
        default=None,
        help=(
            "Read JSON object params from a file instead of argv, or from "
            "stdin when PATH is '-' (pair with a quoted heredoc: "
            "--params-file - <<'JSON'; blocks on stdin if invoked without a "
            "redirect). ARG_MAX-safe, quoting-immune (no payload byte can "
            "change how the shell parses the command), and decoded as "
            "explicit UTF-8 on both the stdin and file forms. "
            "Mutually exclusive with the positional params_json."
        ),
    )
    p.add_argument(
        "--repo",
        metavar="PATH",
        default=None,
        help=(
            "Repo root (absolute path). If omitted, resolved via "
            "coordinator_core.lifecycle.find_repo_root from cwd. "
            "Required — implicit os.getcwd() fallback is prohibited."
        ),
    )
    p.add_argument(
        "--dump-op-timeouts",
        action="store_true",
        default=False,
        help=(
            "Print the op-timeout-budget map as JSON to stdout and exit 0. "
            "No <op> required. Shape: {\"<op>\": <float>, ..., "
            "\"__default__\": <live DISPATCH_TIMEOUT_SECS>, "
            "\"__ceremony_budget__\": <ipc.CEREMONY_BUDGET_SECS>}, with every "
            "ceremony.* op projected explicitly at the ceremony budget. Takes priority "
            "over <op>: if both are passed, <op> is ignored."
        ),
    )
    p.add_argument(
        "--bare",
        action="store_true",
        default=False,
        help=(
            "On SUCCESS only, print just the bare `result` object "
            "(json.dumps of response['result'], no jsonrpc/id envelope, no "
            "indentation) instead of the full JSON-RPC 2.0 response envelope. "
            "Lets a single-spawn caller (e.g. cc_invoke) consume stdout "
            "directly without a second process to strip the envelope. The "
            "error path is UNCHANGED — the full envelope is still printed on "
            "error, matching default behavior, since callers detect errors "
            "via exit code / stderr, never by parsing stdout after a nonzero "
            "exit. Default (flag absent) is byte-identical to pre-existing "
            "behavior."
        ),
    )
    p.add_argument(
        "--allow-unstamped-dispatch",
        action="store_true",
        default=False,
        help=(
            "Opt out of the dispatch-axis stamp gate (ipc.dispatch_message) "
            "for THIS invocation only, so an op runs against an unstamped "
            "(live working tree) engine root instead of being refused. For "
            "deliberate manual testing of engine changes ONLY -- typed per "
            "invocation, never inherited by a spawned child. Live ops must "
            "never pass this; it exists so a developer can test against "
            "their own edits before publishing them."
        ),
    )
    return p


# ---------------------------------------------------------------------------
# --dump-op-timeouts read-only surface
# ---------------------------------------------------------------------------

def _dump_op_timeouts() -> dict:
    """Build the op->timeout-budget JSON payload from the live dispatcher source of truth.

    Reads coordinator_core.ipc.OP_TIMEOUT_OVERRIDES and DISPATCH_TIMEOUT_SECS,
    the same module-level constant _timeout_for() falls back to. No second
    source of truth is introduced -- this is a read-only projection of the
    dispatcher's own timeout table, re-read live at call time rather than
    hardcoded, so a process started with an overridden
    COORDINATOR_DISPATCH_TIMEOUT_SECS env var reports the value it actually
    resolved to. OP_TIMEOUT_OVERRIDES itself is a MappingProxyType built once
    at ipc.py import as a value-snapshot of the private _OP_TIMEOUT_OVERRIDES
    dict _timeout_for() reads -- not a live view of it. Harmless while both are
    empty; a future dynamic mutation of the private table post-import would
    not appear in this dump even though _timeout_for() would see it.

    Ceremony ops are projected EXPLICITLY at `ipc.CEREMONY_BUDGET_SECS` rather than
    left to fall through to "__default__". An external caller sizes its own kill
    ceiling off this dump (DoE's cc_invoke: `engine_budget(op) + MARGIN`), so a
    ceremony op reporting the 30s default here would hand that caller a ~40s ceiling
    for an op the engine abandons at 2s -- the caller would sit waiting on a request
    already cancelled. Every op the keying table knows about is emitted by name, and
    "__ceremony_budget__" carries the ceiling itself so a caller can bound a
    ceremony op this table does not list (the budget is prefix-matched in
    `ipc._timeout_for`, so an unlisted `ceremony.*` op is still capped).

    Returns:
        dict -- {"<op>": <float>, ..., "__default__": <float>,
        "__ceremony_budget__": <float>}. "__default__" is the reserved key for the
        global runaway-guard fallback.
    """
    from coordinator_core.ipc import (
        CEREMONY_BUDGET_SECS,
        DISPATCH_TIMEOUT_SECS,
        OP_TIMEOUT_OVERRIDES,
        is_ceremony_method,
    )
    from coordinator_core.op_scopes import OP_KEY_SCOPE
    from coordinator_core import publish_lane

    # A second clamp pass over `payload` (re-applying `min(budget,
    # CEREMONY_BUDGET_SECS)` to every entry) was deliberately dropped here: it
    # would only fire for a ceremony op present in `OP_TIMEOUT_OVERRIDES` but
    # absent from `OP_KEY_SCOPE`, and `test_ceremony_budget_ratchet.py`
    # enforces that no such row can ever land -- it fails the build on any
    # edit that "admits a widening override row for a ceremony op." The same
    # ratchet that makes `_timeout_for`'s clamp unconditional makes this dump
    # loop's projection exact by construction: every ceremony op source is
    # `OP_KEY_SCOPE`, already set to `CEREMONY_BUDGET_SECS` below.
    payload = dict(OP_TIMEOUT_OVERRIDES)
    for op in OP_KEY_SCOPE:
        if is_ceremony_method(op):
            payload[op] = CEREMONY_BUDGET_SECS

    # Publish-lane projection (DR-350). This dump is spawned as a CHILD of the caller
    # sizing its own kill ceiling, so it inherits that caller's environment and can see
    # the lane declaration directly -- no envelope, and no argument to thread through
    # the CLI. Projecting it matters for the same reason ceremony ops are projected
    # explicitly above: a lane op reporting 2s here would hand a publish round a ~4s
    # client ceiling for a commit the engine is willing to spend ten minutes on, and the
    # round would be killed by its own caller long before the engine gave up. Outside a
    # declared round `budget_for` returns None for every op and this loop is a no-op, so
    # the dump an ordinary caller reads is byte-identical to what it was before.
    for op in publish_lane.PUBLISH_LANE_OPS:
        lane_budget = publish_lane.budget_for(op)
        if lane_budget is not None:
            payload[op] = lane_budget

    payload["__default__"] = DISPATCH_TIMEOUT_SECS
    payload["__ceremony_budget__"] = CEREMONY_BUDGET_SECS
    return payload


# ---------------------------------------------------------------------------
# Repo-root resolution
# ---------------------------------------------------------------------------

def _resolve_repo_root(repo_arg: Optional[str], cwd: str) -> Path:
    """Resolve repo_root from --repo arg or git rev-parse --show-toplevel.

    `cwd` is the CALLER's cwd (see `_dispatch_argv`'s docstring) — a relative
    `--repo` value is joined against it (`Path(cwd, repo_arg)`, which for an
    already-absolute `repo_arg` is equivalent to using `repo_arg` alone), and
    `find_repo_root(cwd)` resolves `git rev-parse --show-toplevel` against it
    explicitly, never against this process's own `os.getcwd()` — served warm,
    those two cwds are different processes' cwds entirely.

    Negative-spec: implicit os.getcwd() fallback is PROHIBITED (AC-5 migration).
    If repo_root cannot be resolved, emits a JSON error envelope to stderr and
    exits non-zero — never silently defaults to a fallback path.

    Returns:
        Path — canonical, resolved repo root.
    """
    if repo_arg is not None:
        return Path(cwd, repo_arg).resolve()

    # Explicit find_repo_root(cwd) — NOT a raw os.getcwd() use.
    from coordinator_core.lifecycle import find_repo_root  # deferred: no side effects at import time
    try:
        return find_repo_root(cwd)
    except RuntimeError as exc:
        _fatal_stderr(
            f"repo_root unresolvable: not inside a git working tree and --repo was not "
            f"provided. Run from a git repo or pass --repo <path>. Detail: {exc}"
        )
        raise AssertionError("unreachable — _fatal_stderr always raises")


# ---------------------------------------------------------------------------
# Fatal error helper
# ---------------------------------------------------------------------------

def _fatal_stderr(message: str) -> None:
    """Emit a JSON-RPC 2.0 error envelope to stderr and raise SystemExit(1).

    Used for pre-dispatch failures (bad args, unresolvable repo_root) so that
    callers can distinguish infrastructure errors (stderr, exit 1) from op-level
    errors (stdout, exit 1 with a JSON-RPC error response).

    Raises SystemExit(1) rather than calling os._exit(1) directly: this
    function runs under `_dispatch_argv`'s stdout/stderr redirect (both on
    the real CLI path via `main()` and on the served path via
    `coordinator_core.ops.invoke_from_argv`), so `print(..., file=sys.stderr)`
    above already writes into that capture. `_dispatch_argv` is the sole
    place that turns a SystemExit into a real process exit (only when it IS
    the real process, i.e. from `main()`) or into a returned exit_code
    (when served). Calling os._exit() here would kill the long-lived warm
    server process outright on any served pre-dispatch failure.
    """
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32603, "message": message},
        },
        separators=(",", ":"),
    )
    print(payload, file=sys.stderr)
    sys.stderr.flush()
    raise SystemExit(1)


#: How long `_wait_for_warm_boot` may wait for a just-spawned warm server to
#: start answering, in seconds. `COORDINATOR_WARM_BOOT_WAIT_SECS` overrides it;
#: `0` disables the wait entirely, restoring the pre-wait behaviour (miss ->
#: immediate refusal).
#:
#: READ THE RIGHT CLOCK. This is WALL CLOCK at near-zero process time -- a
#: sleeping poll loop, not work. CLAUDE.md's brightline is measured in process
#: time and spawn count, "never wall clock", so a bounded wait does not breach
#: the 500ms or 2s bars however long it sits: nothing on this box is occupied
#: while it does. An AC or report that charges this number against those bars is
#: reading the wrong clock (claude-klabauter-22, 2026-08-26, sharpening the
#: defence from "it beats four minutes of human guessing").
#:
#: NOT A BUDGET AND NOT A MEASUREMENT. Reaching the engine is budgeted in
#: hundreds of milliseconds (CLAUDE.md's brightline), and this number is an
#: order of magnitude past it by construction: it bounds a FAULT -- the window
#: in which a box that should already have had a resident server is standing
#: one up. It is deliberately not fitted to an observed boot, because no boot
#: on this box has ever been measured: the only intervals on record
#: (2026-08-25/26: +0s, +30s, +4min) are the intervals four operators happened
#: to retry at, which bound nothing. `record_client_boot_wait` exists to
#: replace this guess with the real distribution. Until it has rows, 15s is
#: chosen to be long enough that a genuine interpreter-plus-election boot is
#: not cut off mid-flight, and short enough that nobody can mistake it for
#: normal operation or absorb it as cadence.
#:
#: WHAT THE COLD LOG SAYS, AND WHY IT CANNOT SET THIS NUMBER (doe-claude-cb,
#: 2026-08-26, swept from `client-cold.jsonl`; both readings below
#: reproduced independently here). 2131 recorded misses, 2026-08-20 ->
#: 2026-08-26, clustered into 121 outage windows at a >60s gap.
#:
#: THE FILE HAS TWO DEFENSIBLE READINGS AND THEY DISAGREE BY 9x. A window is
#: measured from its first miss to its last, so it is bounded by when callers
#: happened to call, and 42 of the 121 windows hold a SINGLE miss -- they
#: measure 0s carrying no duration information at all. Read every window and
#: the median is 1s with 28% over 15s. Drop the windows that cannot measure
#: anything and the median is 9s with 43% over 15s (n=79). Neither is the
#: answer: the first is dragged down by windows that measured nothing, and
#: the second over-samples long outages, because a long outage collects more
#: calls and so is likelier to clear the >=2 bar. The honest statement is a
#: bracket -- median somewhere in 1-9s, over-bound share somewhere in 28-43%
#: -- and nothing on disk narrows it.
#:
#: So the premise is COMPATIBLE with this file, not vindicated by it. A wait
#: is worth having if misses are usually a server nearly up; that reading
#: survives, and so does a materially worse one.
#:
#: This is worse than censored: it is censored with the bias direction
#: unknown. Fitting a constant to either reading would be the same defect as
#: quoting an ETA -- a number that looks measured and is not. What the tail
#: does establish (p90 56s, max 234s on the all-windows reading) is that no
#: fixed bound covers it.
#:
#: What the tail DOES establish is that no fixed bound covers it, and that
#: chasing it would be the wrong move: a caller inside the 234s window eats
#: the full failure either way, and a longer bound only adds sleeping to it.
#: The long windows are a separate fault to be found, not a duration to be
#: absorbed. `client-boot-wait.jsonl` is the instrument that can eventually
#: set this number, because it records actual waits with `served` alongside
#: elapsed -- uncensored, and able to separate "waited and got there" from
#: "waited and never did".
#:
#: NEVER REACHABLE FROM A HOOK. This wait is for the op/CLI door, where a
#: caller is already waiting on a result. A hook path must pass
#: `COORDINATOR_WARM_BOOT_WAIT_SECS=0` in the child it spawns: hooks fire on
#: the session and commit hot path where blocking is never acceptable, and
#: `client-cold.jsonl` carries a burst of 1600 misses in 13 seconds
#: (2026-08-25T16:33:28Z, ~123/s), which is many short-lived processes each
#: taking one miss. Whatever produces that burst must never each sleep here.
#: -> state/bug-backlog/2026-08-26-sixteen-hundred-warm-misses-in-thirteen-seconds.yaml
WARM_BOOT_WAIT_SECS = 15.0

#: First poll interval, and the cap it backs off to. Fast at the start because
#: the case this exists for is a server that is nearly up; capped at a second
#: because a tighter tail buys nothing and every poll is a pipe open. Polling
#: cannot storm the spawn path: `warm.client._spawn_once` is one-per-process
#: (`_spawned_this_process`) and cross-process debounced by
#: `breadcrumb.should_spawn`, so every poll after the first re-uses the spawn
#: already in flight rather than triggering another.
#:
#: That claim is about SPAWNS and answers only the spawn question. It says
#: nothing about MISSES, which are recorded per poll and per process -- and a
#: burst of them is a real observed shape on this box, not a hypothetical
#: (see `WARM_BOOT_WAIT_SECS` above). Do not read it as covering both.
_BOOT_POLL_MIN_SECS = 0.1
_BOOT_POLL_MAX_SECS = 1.0
_BOOT_POLL_GROWTH = 1.6


def _warm_boot_wait_deadline() -> float:
    """The configured bound, in seconds. Unset -> `WARM_BOOT_WAIT_SECS`;
    unparseable or negative -> the same default, since a malformed knob must
    not silently turn the wait off -- that is what an explicit `0` is for."""
    raw = os.environ.get("COORDINATOR_WARM_BOOT_WAIT_SECS")
    if raw is None or raw.strip() == "":
        return WARM_BOOT_WAIT_SECS
    try:
        value = float(raw)
    except ValueError:
        return WARM_BOOT_WAIT_SECS
    if value < 0:
        return WARM_BOOT_WAIT_SECS
    return value


def _wait_for_warm_boot(msg: dict) -> Tuple[Optional[dict], float]:
    """Poll `try_warm_dispatch` until a warm server serves `msg` or the bound
    expires. Returns `(response, waited_secs)`; `response` is None when nothing
    served the call before the deadline.

    THIS IS THE OP/CLI DOOR, NOT THE HOOK FAST PATH. `warm/client.py`'s
    negative-spec forbids a poll loop inside that module by contract -- it is
    imported on hook paths where blocking is never acceptable, and the value of
    its cold-signal return is that it is bounded by a single round trip. This
    function is the other side of that line: a human or a script invoked one op
    and is already waiting on its result, so waiting a bounded moment for the
    server that op needs beats handing back a refusal for a fault that is
    already healing. Before this existed, the retry interval was supplied by an
    operator guessing, and the guess was unbounded (four sessions lost an
    evening's memo traffic to it, 2026-08-25/26).

    WHY THIS IS NOT BACKSTOP 2. What the PM retired (2026-08-21) was a SILENT
    degrade to a full cold spawn on every miss, forever. This waits for the
    WARM server, announces itself on stderr before it waits, waits once, and
    still fails hard when the bound expires -- there is no path from here to a
    cold dispatch the caller did not ask for with `--allow-unstamped-dispatch`.

    Aborts early on a PERMANENT reason established mid-wait
    (`last_cold_reason`): those recur identically on every poll, so waiting the
    deadline out would burn the bound to reach a conclusion already in hand.

    Negative-spec:
        - Does NOT retry a served error envelope. Anything well-formed coming
          back is the server answering, which is the condition this waits for.
        - Does NOT print an ETA or a countdown. Boot time is load-dependent
          and, until `record_client_boot_wait` has rows, unknown -- an interval
          an operator can satisfy is one they will draw a wrong conclusion from.
    """
    import time as _time

    from coordinator_core.warm.client import last_cold_reason, try_warm_dispatch

    deadline_secs = _warm_boot_wait_deadline()
    if deadline_secs <= 0:
        return None, 0.0

    started = _time.monotonic()
    print(
        "[warm-client] no warm server answered; a respawn was triggered and this call "
        f"is waiting up to {deadline_secs:g}s for it to start answering.",
        file=sys.stderr,
    )
    sys.stderr.flush()

    interval = _BOOT_POLL_MIN_SECS
    response = None
    while True:
        remaining = deadline_secs - (_time.monotonic() - started)
        if remaining <= 0:
            break
        _time.sleep(min(interval, remaining))
        response = try_warm_dispatch(msg)
        if response is not None:
            break
        if last_cold_reason():
            break
        interval = min(interval * _BOOT_POLL_GROWTH, _BOOT_POLL_MAX_SECS)

    waited = _time.monotonic() - started
    try:
        from coordinator_core.warm.telemetry import record_client_boot_wait

        record_client_boot_wait(
            waited_secs=waited,
            served=response is not None,
            deadline_secs=deadline_secs,
        )
    except Exception:  # noqa: BLE001 -- an instrument may not be why an op fails
        pass
    return response, waited


def _exit_code_for_response(response: dict, structural_pin_error_code: int) -> int:
    """Select the process exit code for a completed JSON-RPC response.

    Pure/testable in-process (unlike main(), which os._exit()s and can only be
    regression-tested via subprocess — see test_invoke_main.py's module docstring).

    0 — no 'error' key (success).
    2 — 'error' present and error.code == structural_pin_error_code (STRUCTURAL_PIN_ERROR)
        — a structurally-wedged contract-pin failure; see the module docstring's
        "Exit codes" section for why this is distinct from the generic exit 1.
    1 — any other 'error' (generic/transient op-level error).
    """
    error = response.get("error")
    if error is None:
        return 0
    if isinstance(error, dict) and error.get("code") == structural_pin_error_code:
        return 2
    return 1


# ---------------------------------------------------------------------------
# Reusable core — argv in, (stdout, stderr, exit_code) out
# ---------------------------------------------------------------------------

def _dispatch_argv(argv: list, cwd: str, *, allow_warm: bool = True) -> Tuple[str, str, int]:
    """Parse `argv`, dispatch the named op in-process, return (stdout, stderr, exit_code).

    The served-callable core behind BOTH `main()` (real CLI process,
    `python -m coordinator_core.invoke <argv...>`) and the warm server's
    `invoke.from_argv` op (`coordinator_core/ops/invoke_from_argv.py`) — the
    native door's server-side entrypoint (docs/reference — see that op
    module's docstring). Extracted so the door relays raw argv to the
    server and the server does the SAME argv-parsing/dispatch/response-
    render this CLI has always done, rather than a second parser silently
    diverging from this one.

    Never touches `sys.argv`, never calls `os.getcwd()` (uses `cwd`
    EXPLICITLY everywhere a cwd is needed — repo_root resolution, a
    relative `--repo`/`--params-file` value, the `_caller_cwd` telemetry
    stamp), never writes to the real stdout/stderr, and never calls
    `os._exit()` or terminates the process. This matters beyond testability:
    served from inside the long-lived warm server process, "the caller's
    cwd" is the DOOR's cwd (the `cwd` argument), never this function's own
    process cwd — getting that wrong corrupts repo-scope resolution and the
    op-latency telemetry denominator for every other request that process
    ever serves. See the `_caller_cwd`/`_origin_worktree` comments below.

    `allow_warm=False` (used by `invoke.from_argv`) skips the warm-pipe
    preamble entirely and goes straight to cold in-process dispatch — a
    request already being served BY the warm server has no business
    dialing a pipe back into itself. This is a divergence in INTERNAL PATH
    only: the warm preamble's own contract is "transparent to the response"
    (any live server serves the identical dispatch_message() result a cold
    call would), so output stays byte-identical either way; `allow_warm`
    exists purely to avoid a pointless (and, single-threaded-accept-loop
    dependent, possibly self-blocking) extra IPC hop. `main()` always calls
    this with the default `allow_warm=True`, preserving the CLI's existing
    warm-then-cold behavior unchanged.

    Every early-exit point below (`_fatal_stderr`, `parser.error`,
    `--dump-op-timeouts`, the final success/error print) raises
    `SystemExit` — argparse's own error paths already do, so catching
    `SystemExit` once here, at this function's boundary, uniformly converts
    every process-exit intent (ours and argparse's) into a returned
    `exit_code` instead of actually exiting.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        try:
            _dispatch_argv_body(argv, cwd, allow_warm=allow_warm)
        except SystemExit as exc:
            code = exc.code
            if code is None:
                code = 0
            elif not isinstance(code, int):
                # argparse's own error() passes a message string here on some
                # paths; treat anything non-int as a generic failure exit,
                # matching the process-level `python` interpreter's own
                # coercion of a non-int SystemExit.code to exit status 1.
                code = 1
            return stdout_buf.getvalue(), stderr_buf.getvalue(), code
    raise AssertionError("_dispatch_argv_body must always raise SystemExit")


def _dispatch_argv_body(argv: list, cwd: str, *, allow_warm: bool) -> None:
    """Parse args, build JSON-RPC request, dispatch in-process, print result.

    Always exits via `raise SystemExit(<code>)` — never returns normally.
    Runs under `_dispatch_argv`'s stdout/stderr redirect; every `print()`
    and `sys.stderr.write()` below lands in that capture, not the real
    streams.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # --allow-unstamped-dispatch: process-local, per-invocation opt-out of
    # ipc.dispatch_message's stamp gate (state/handoffs/2026-08-21_103635_
    # reaching-the-warm-engine.md). Deliberately set here, AFTER argparse,
    # never via an environment variable -- see ipc.allow_unstamped_dispatch's
    # own docstring for why an env var is the wrong shape (inherited by every
    # subprocess this process might itself spawn, silently disarming the
    # gate somewhere nobody intended). Import kept local: this module's own
    # "no eager coordinator_core.ops import" discipline (see the warm
    # preamble's own comment below) still applies, and `ipc` is already
    # imported a few lines down for `dispatch_message` regardless.
    if args.allow_unstamped_dispatch:
        from coordinator_core.ipc import allow_unstamped_dispatch

        allow_unstamped_dispatch()

    # 0. --dump-op-timeouts short-circuit -- read-only, no op dispatch, no
    #    repo_root resolution, no params parsing. Runs BEFORE the op-required
    #    check below since this is the one flag that makes <op> optional.
    if args.dump_op_timeouts:
        # Review: code-reviewer (nit) -- match every other pre-dispatch failure
        # path's _fatal_stderr contract instead of letting an import/build
        # failure surface as a raw Python traceback.
        try:
            payload = _dump_op_timeouts()
        except Exception as exc:
            _fatal_stderr(f"--dump-op-timeouts failed: {exc}")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        sys.stdout.flush()
        raise SystemExit(0)

    if args.op is None:
        parser.error("the following arguments are required: op (unless --dump-op-timeouts is passed)")

    # 1. Resolve params JSON source — --params-file, positional argv, or default '{}'.
    #    Mutually exclusive: a large params payload (e.g. ceremony.wsc_commit's
    #    resolved_state round-trip) overflows argv ARG_MAX on some platforms
    #    (notably Windows/msys, ~32KB); --params-file is the ARG_MAX-immune path.
    if args.params_file is not None and args.params_json is not None:
        _fatal_stderr(
            "--params-file and positional params_json are mutually exclusive"
        )

    if args.params_file is not None:
        # `-` reads stdin: the quoting-immune transport. A params payload
        # carrying an apostrophe (a commit message saying "C1's half") breaks
        # a single-quoted argv payload at the SHELL, before this process is
        # reached — bash ends the quoted span at the apostrophe and the rest
        # of the payload becomes unquoted shell text, so any `(`/`)` in it is
        # a syntax error and anything else is silently re-tokenized. A quoted
        # heredoc (`--params-file - <<'JSON'`) has no interpolation and no
        # quote sensitivity, so no payload byte can change how the shell
        # parses the command. Also ARG_MAX-immune, like the file path form,
        # and needs no temp file to clean up.
        if args.params_file == "-":
            # Review: code-reviewer (P1) — decode the raw stdin bytes as UTF-8
            # explicitly, matching the file branch below, instead of
            # sys.stdin.read() (which decodes via locale.getpreferredencoding()).
            # On Windows, a redirected pipe/heredoc stdin resolves that to the
            # ANSI code page (commonly cp1252), a single-byte codec that maps
            # almost every byte to SOME character — so a UTF-8-encoded
            # multi-byte payload (an em dash, a curly quote, non-Latin text)
            # does not raise a decode error, it silently decodes to the WRONG
            # characters, which is still syntactically valid JSON and
            # dispatches without error. That is worse than the loud
            # shell-quoting failure this transport exists to replace.
            try:
                params_json_str = sys.stdin.buffer.read().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _fatal_stderr(f"Cannot read params JSON from stdin: {exc}")
        else:
            # Relative to `cwd` (the caller's cwd) — NOT this process's own
            # os.getcwd(), which is meaningless when served warm.
            try:
                params_json_str = Path(cwd, args.params_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _fatal_stderr(f"Cannot read --params-file {args.params_file!r}: {exc}")
    elif args.params_json is not None:
        params_json_str = args.params_json
    else:
        params_json_str = "{}"

    # 2. Parse params JSON — must be a JSON object.
    try:
        params = json.loads(params_json_str)
    except json.JSONDecodeError as exc:
        _fatal_stderr(f"Invalid params_json: {exc}")

    if not isinstance(params, dict):
        _fatal_stderr(
            f"params_json must be a JSON object (dict); got {type(params).__name__}"
        )

    # 3. Determine op scope before resolving repo_root — none/central-scoped ops
    #    (e.g. ping, advisory hooks) must work outside any git working tree, so
    #    _resolve_repo_root (which calls git rev-parse) must NOT be called for them.
    from coordinator_core.ipc import WORKTREE_SCOPED_OPS

    # 3a. --repo on a "none"-scoped op fails loud instead of silently no-opping.
    #     Before this check, --repo was accepted and then simply never read — the
    #     branch below only resolves repo_root (and only reads args.repo) for ops
    #     IN WORKTREE_SCOPED_OPS, so a "none"-scoped op ignored the flag entirely
    #     while exiting 0, indistinguishable from --repo actually steering
    #     something. Example-market-data-repo-em ran the survey end-to-end and
    #     reported in good faith that "both --repo forms work — we tested" —
    #     true, and carrying zero information, because neither form does
    #     anything on a "none"-scoped op. DoE nearly planned against that line.
    #     See docs/decisions/DR-279-repo-on-a-none-scoped-op-fails-loud.md.
    #     Scoped to DISPATCHABLE ops only. `OP_KEY_SCOPE.get(op, "none")` is the
    #     right default for a registered-but-unclassified op (see
    #     `ipc.py`'s own absent-means-none rule), but it also answered for ops
    #     that do not exist: an UNDISPATCHABLE name got a refusal naming a
    #     scope nobody had assigned it, and "Omit --repo for this op" routed
    #     the caller from that specific-sounding error to a bare -32601 Method
    #     not found. The two errors contradicted each other and neither said
    #     the true thing, which is that the op is not there. Observed
    #     2026-08-26 on `push.outstanding`, registered in claude-klabauter and absent
    #     from the published engine the forwarder actually dispatches to
    #     (doe-claude-94, state/bug-backlog/2026-08-26-quick-wrap-step-1-has-
    #     no-working-commit-863135e32339.yaml). An unknown op now falls
    #     through to dispatch, which answers Method-not-found honestly.
    if args.repo is not None and args.op not in WORKTREE_SCOPED_OPS:
        from coordinator_core.op_scopes import OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import resolves as _op_resolves

        # Fall through ONLY for an op unknown to BOTH tables. `resolves()` alone
        # was too narrow: its second leg reads `ipc._REGISTRY`, which is empty in
        # this cold CLI process because registration is lazy, so it reduces to
        # "is it in OP_MODULE_MAP" here -- and that module's own docstring admits
        # a dispatchable op legitimately absent from the map. Gating on
        # `resolves()` alone therefore skipped the DR-279 refusal for such an op
        # exactly as for a nonexistent one, restoring the silent --repo no-op
        # DR-279 was built to kill under a new trigger (review: code-reviewer,
        # P1). Requiring BOTH tables to disclaim the op keeps the refusal
        # wherever anything knows about it.
        if _op_resolves(args.op) or args.op in OP_KEY_SCOPE:
            op_scope = OP_KEY_SCOPE.get(args.op, "none")
            _fatal_stderr(
                f"--repo is meaningless for op {args.op!r} (scope={op_scope!r}): this op "
                f"accesses no repo-specific state and never reads repo_root, so --repo "
                f"would silently no-op. Omit --repo for this op. See "
                f"docs/decisions/DR-279-repo-on-a-none-scoped-op-fails-loud.md."
            )

    # 4. Resolve repo_root — ONLY for worktree-scoped ops; fail-loud if unresolvable (AC-5).
    #    For all other scopes (none, central) repo_root stays None — git is never invoked.
    if args.op in WORKTREE_SCOPED_OPS:
        repo_root: Optional[Path] = _resolve_repo_root(args.repo, cwd)
    else:
        repo_root = None

    # 5. Op registration is now LAZY (F6 / claude-klabauter-windows-portability § C4):
    #    dispatch_message's registry-MISS path (ipc.py::_lazy_import_and_lookup)
    #    imports only the dispatched op's owning module on first lookup, instead
    #    of eagerly importing all ~55 op modules here (Windows cold-compile tax
    #    with no __pycache__ warm-up). No eager `import coordinator_core.ops`
    #    call is needed before dispatch.
    from coordinator_core.invoke.dispatch import STRUCTURAL_PIN_ERROR, dispatch_message

    # 6. Build the JSON-RPC 2.0 request envelope.
    msg: dict = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": args.op,
        "params": params,
    }

    # Inject _origin_worktree for working-tree-scoped ops (common_dir + show_top keying).
    # Central and "none"-scoped ops silently ignore this field per the _OP_KEY_SCOPE table.
    # repo_root is a valid Path here — it was resolved unconditionally in the branch above.
    if args.op in WORKTREE_SCOPED_OPS:
        msg["_origin_worktree"] = str(repo_root)

    # Stamp the CALLER's cwd unconditionally — including for "none"-scoped
    # ops, which never get `_origin_worktree` above (C7,
    # 2026-08-20-a-refusal-cannot-exit-zero). Telemetry-only: never read for
    # authz/repo-scope resolution (that stays resolve_request_repo's job via
    # `_origin_worktree`). This is `cwd` (the argument), NOT os.getcwd():
    # warm-served rows would otherwise fall back to
    # `coordinator_core.telemetry.op_latency._write_entry`'s Path.cwd(),
    # which in a warm pool worker is the SERVER's cwd (the klabauter clone),
    # not this caller's — corrupting the very denominator the warmth sweeps
    # measure. See `_CALLER_CWD_FIELD` in coordinator_core.ipc for the reader.
    msg["_caller_cwd"] = cwd

    # 6a. Warm preamble (C15) — try the warm engine's pipe before paying a
    #     cold spawn-per-call. NO CLIENT EVER WAITS FOR A SERVER TO BOOT:
    #     `try_warm_dispatch` returns a JSON-RPC response only when a live
    #     warm server actually served this request (any well-formed
    #     response, including an error envelope, counts — see
    #     coordinator_core.warm.client's module docstring for the full
    #     anti-storm table); it returns None for every other outcome
    #     (warmth disabled, no pipe, busy, someone else's pipe, a stale
    #     ENGINE_SKEW server, or read-deadline expiry), including on the
    #     FileNotFoundError path where it best-effort spawns a warm server
    #     for NEXT time and still returns None for THIS call. `try_warm_
    #     dispatch` itself never raises. BACKSTOP 2 IS RETIRED (2026-08-21,
    #     PM ruling, state/handoffs/2026-08-21_103635_reaching-the-warm-
    #     engine.md): "the cold path is a SUCCESS path" is no longer this
    #     box's rule when warm is enabled -- see the fail-hard block
    #     immediately below `try_warm_dispatch`'s call, which refuses to
    #     fall through to cold on a warm miss unless the caller opted in.
    #     Everything below THAT point is the pre-existing cold dispatch
    #     path, unchanged: it only runs when `response` is not already set,
    #     which now means either a served warm hit, warm disabled entirely,
    #     or the explicit `--allow-unstamped-dispatch` carve-out.
    #
    #     `allow_warm=False` (invoke.from_argv, served FROM the warm server)
    #     skips this block outright — see this function's caller's docstring
    #     for why dialing back into the same server would be pointless.
    #
    #     W11 (2026-08-20-a-refusal-cannot-exit-zero § C8): check
    #     `settings.is_warm_enabled()` BEFORE importing `warm.client` at
    #     all. `warm.client` unconditionally imports `warm.election` +
    #     `warm.settings` at its own module level (its docstring's AC3/AC4
    #     note explains why those two stay eager there), which transitively
    #     pulls 19 coordinator_core modules — 29.2ms measured, against this
    #     __main__'s own ~53ms floor — regardless of whether
    #     `COORDINATOR_WARM=0` is set. `settings.py` imports only
    #     `machine_resolver.registry_get`, so resolving the same
    #     warm/cold answer through it first, and skipping the `warm.client`
    #     import entirely when it says off, avoids paying that tax on the
    #     COORDINATOR_WARM=0 path — the same defect class
    #     claude-klabauter-44 fixed for `detached_spawn`, through a different
    #     door. Behaviourally identical to before: `try_warm_dispatch`
    #     itself would have returned None on this same disabled check, so
    #     `response` still resolves to None either way; only the import
    #     cost changes. `is_warm_enabled` is memoised per-process (W12,
    #     this chunk's settings.py change), so this call is cheap even when
    #     re-derived rather than cached across this single-shot CLI process.
    response = None
    if allow_warm:
        from coordinator_core.warm.settings import is_warm_enabled

        if is_warm_enabled():
            from coordinator_core.warm.client import try_warm_dispatch

            response = try_warm_dispatch(msg)

            # FAIL HARD, NOT FAIL CLOSED (state/handoffs/2026-08-21_103635_
            # reaching-the-warm-engine.md; PM ruling verbatim: "I'd rather
            # have a fail than a silent slow. Much rather."). THIS REVERSES
            # Backstop 2 -- warm.client's own module docstring names it "the
            # cold path is a SUCCESS path", the deliberate design this box
            # ran on until today. See that module's docstring for the
            # retirement notice; this is the enforcement half of it.
            #
            # A warm-enabled box that could not reach a live server for
            # THIS call (skew-evicted, still booting, busy, wedged) no
            # longer silently degrades to a slow cold spawn -- it fails,
            # loudly, on THIS invocation. `try_warm_dispatch` has already
            # kicked off a fresh spawn attempt on its own way out for every
            # miss that can trigger one (see its own module docstring's
            # spawn-trigger table) -- this failure's own remediation is
            # therefore "retry", not "go fix something": the self-heal is
            # already in flight by the time this message is printed.
            #
            # Bypassed by the SAME explicit opt-in as the stamp gate
            # (`ipc.is_unstamped_dispatch_allowed()`) -- one carve-out for
            # "this is a deliberate manual/test invocation", not two
            # independently-toggled ones. A manual test against a live
            # engine build routinely has no warm server for that build at
            # all; demanding one would make the carve-out unusable for the
            # exact case it exists to serve.
            if response is None:
                from coordinator_core.ipc import is_unstamped_dispatch_allowed

                if not is_unstamped_dispatch_allowed():
                    # "Retry in a moment" is the right remediation for a
                    # TRANSIENT miss (server booting, busy, skew-evicted) and
                    # the wrong one when this process can never reach a warm
                    # server at all. In that second case the client has
                    # already established why, and printing the retry advice
                    # over the top of it produced the contradiction an
                    # operator hit on every live op (2026-08-22): "every call
                    # from this tree goes cold", then "cold fallback is
                    # disabled", with no path in either half. Ask the client
                    # for its reason and lead with that instead.
                    from coordinator_core.warm.client import last_cold_reason

                    reason = last_cold_reason()
                    if reason:
                        _fatal_stderr(
                            f"{reason}\n"
                            "Cold fallback is disabled (no live ops without warm); "
                            "retrying will not clear this. For deliberate manual "
                            "testing, pass --allow-unstamped-dispatch."
                        )
                    # WAIT ONCE, BOUNDED, RATHER THAN MAKE A HUMAN GUESS THE
                    # INTERVAL. Every miss reaching this point has already
                    # triggered a respawn on its way out (see the block above),
                    # so the fix for this refusal is in flight while the
                    # refusal is being printed. Refusing here regardless made
                    # the retry interval an operator's guess, and the guess is
                    # unbounded: on 2026-08-25/26 four sessions lost an
                    # evening's memo traffic re-running `cross-repo-memo send`
                    # by hand until one happened to land, the last at +4min.
                    # None of those numbers measured a boot; they measured
                    # patience. `_wait_for_warm_boot` replaces the guess with a
                    # bound, and records what it actually waited so the boot
                    # itself finally gets measured.
                    response, waited = _wait_for_warm_boot(msg)
                    if response is None:
                        # A permanent reason can be established DURING the wait
                        # (the first `try_warm_dispatch` had none, a later poll
                        # did). Re-ask before reaching for the transient
                        # wording, for the same reason the check above exists.
                        reason = last_cold_reason()
                        if reason:
                            _fatal_stderr(
                                f"{reason}\n"
                                "Cold fallback is disabled (no live ops without warm); "
                                "retrying will not clear this. For deliberate manual "
                                "testing, pass --allow-unstamped-dispatch."
                            )

                        # A WAIT THIS LONG IS A DEFECT REPORT, NOT A DIAGNOSIS.
                        # An earlier version of this block said "retry in a
                        # moment"; four sessions read that as seconds, retried
                        # twice, and concluded the fault was permanent
                        # (2026-08-25/26). The first correction merely told the
                        # truth about the delay -- "it takes MINUTES" -- which
                        # is the "the box was busy" answer CLAUDE.md forbids: it
                        # made an over-budget path read as normal operation and
                        # invited the operator to absorb it.
                        #
                        # Both framings were wrong for the same reason. Reaching
                        # the engine is budgeted in HUNDREDS OF MILLISECONDS;
                        # anything that turns into a multi-minute wait is over
                        # the brightline by orders of magnitude and is a P0, not
                        # a cadence. Say that, and do not instruct anyone to wait
                        # it out -- this process has already done the only
                        # waiting anyone should do, and reports the duration it
                        # ACTUALLY waited rather than an interval to aim at.
                        #
                        # Negative-spec: no ETA, no countdown, no "wait N
                        # minutes". Boot time is load-dependent and, until
                        # `record_client_boot_wait` has rows, unmeasured; any
                        # interval printed is one the operator can satisfy and
                        # then draw the same wrong conclusion from. A REPORTED
                        # ELAPSED WAIT IS NOT AN ETA: it says what this call
                        # spent, never what the next one will.
                        waited_clause = (
                            f"this call waited {waited:.1f}s without the warm server "
                            "accepting connections"
                            if waited > 0
                            else "the bounded boot wait is switched off "
                            "(COORDINATOR_WARM_BOOT_WAIT_SECS=0), so this call did not "
                            "wait for it"
                        )
                        _fatal_stderr(
                            "warm dispatch unavailable and cold fallback is disabled "
                            f"(no live ops without warm). A respawn was triggered and "
                            f"{waited_clause}. THIS IS A DEFECT, not a queue: reaching "
                            "the engine is budgeted in hundreds of milliseconds, so an "
                            "unreachable warm server is over budget by orders of "
                            "magnitude however busy the box is. Check for a wedged or "
                            "crash-looping warm server rather than retrying by hand. "
                            "For deliberate manual testing, pass "
                            "--allow-unstamped-dispatch."
                        )

    # 7. Dispatch in-process via dispatch_message (async, no socket, no auth gate).
    #    Manual loop instead of asyncio.run() to avoid executor drain on the timeout
    #    path (P1#5): asyncio.run() calls loop.shutdown_default_executor() which joins
    #    any live threads — a handler that asyncio.to_thread'd and then timed out leaves
    #    a live executor thread that would cause asyncio.run() to block indefinitely.
    #    Deliberately not closed below (see the comment there) so no such join happens.
    #
    #    Gated on `response is None` (C15): the warm preamble above already
    #    supplied a served response, so this whole cold-dispatch block —
    #    unchanged from before C15 — is skipped entirely on a warm hit.
    if response is None:
        # Deferred, not module-level: `asyncio` costs 40.4ms of process time to
        # import (measured, `-X importtime`, 2026-08-21) — more than the whole
        # interpreter start minus `site`, and larger than any other single
        # component of the client-side door cost. It is reachable ONLY here, on
        # the cold branch. Importing it at module scope charged every warm hit
        # for an event loop it never constructs, which is the exact class of
        # defect the 2026-08-19 warm-preamble post-mortem records
        # (`warm/client.py :: spawn_detached`) reappearing at a different site.
        #
        # Negative-spec: this module's docstring permits module-level stdlib
        # imports. That rule is the wrong axis and does not license moving this
        # back — provenance is not cheapness. What belongs at module scope on
        # this path is what a warm hit executes, and nothing else.
        import asyncio
        import time as _time

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Stdout is the JSON-RPC transport this process's caller (cc_invoke and
        # every coordinator/bin/ CLI built on it) parses: exactly one JSON value,
        # written after dispatch completes (step 8 below). redirect_stdout rebinds
        # the sys.stdout name for the duration of the block, so it captures every
        # Python-level write that resolves sys.stdout during dispatch — print(),
        # sys.stdout.write(...), anything else looking the name up inside the
        # block — e.g. a CLI-shaped helper called in-process, such as
        # close_out_and_stamp._stamp_plan_landed's status line. Without this,
        # such a write would interleave onto the transport stream and corrupt it
        # for every caller, not just the offending op. Handler-level stdout is
        # captured here and relayed to stderr (never discarded — it is a genuine
        # diagnostic, just on the wrong stream) so the transport cannot be
        # corrupted by a stray write in handler code the dispatch loop does not
        # control. Nested inside THIS function's own outer redirect_stdout (in
        # `_dispatch_argv`): this inner redirect_stdout still captures correctly
        # — contextlib.redirect_stdout always rebinds relative to whatever
        # `sys.stdout` currently is, so nesting composes rather than fighting.
        #
        # Negative-spec — two known bypass classes, neither observed at a live
        # call site today:
        #   - fd-level: a handler that writes via a raw OS file descriptor
        #     (os.write(1, ...)) or spawns a subprocess inheriting fd 1 bypasses
        #     this capture entirely — this is not an fd-level fix.
        #   - pre-bound reference: a handler holding a reference to the original
        #     stdout object captured before this redirect was entered (e.g. a
        #     module-level `_stdout = sys.stdout` bound at import time) writes to
        #     the real stream — redirect_stdout only rebinds the *name*
        #     `sys.stdout`, not references already taken from it.
        # Loop lifecycle (rewritten 2026-08-21 — the prior comment here was
        # wrong for one of this function's two callers; read this in full
        # before touching it again):
        #
        # `main()` (the CLI) reaches this branch inside a single-shot process
        # that os._exit()s right after `_dispatch_argv` returns — a leaked
        # loop there was always cosmetic, reclaimed by process teardown
        # regardless of what this function did.
        #
        # `invoke.from_argv` (coordinator_core/ops/invoke_from_argv.py) also
        # reaches this branch (via `allow_warm=False`, so it ALWAYS takes the
        # cold path) — but it runs INSIDE the resident warm server, on a
        # worker thread from that server's own event loop's default
        # ThreadPoolExecutor (ipc.py's `_dispatch_message_impl` offloads this
        # op's sync handler via `asyncio.to_thread`, i.e.
        # `loop.run_in_executor(None, ...)` on the SERVER's running loop —
        # that pool is persistent and REUSED across calls, so the same OS
        # thread services many `invoke.from_argv` requests over the server's
        # lifetime, one after another). Every request that reaches this
        # branch created a brand-new loop and NEVER closed it — leaking the
        # loop's own OS-level resources (selector/epoll/kqueue fd, or an
        # IOCP handle on Windows) once per call, for as long as the server
        # stays up and is shared by every session on this box.
        #
        # The fix is `loop.close()` — NOT `asyncio.run()`'s
        # `shutdown_default_executor()`, which are different operations
        # (verified against the installed interpreter's own
        # asyncio.base_events.BaseEventLoop.close source, not assumed):
        # `close()` releases the loop's own resources and, if this loop ever
        # lazily created a default executor of its OWN (only possible if
        # something dispatched THROUGH this loop also called
        # `asyncio.to_thread`/`run_in_executor` on it), shuts that down with
        # `executor.shutdown(wait=False)` — signal-only, never blocks, never
        # joins a live thread. `shutdown_default_executor()` instead awaits
        # `executor.shutdown(wait=True)`, which DOES join every thread in the
        # pool — exactly the P1#5 hang this function has always needed to
        # avoid (a handler that asyncio.to_thread'd and then timed out still
        # has a live thread running when dispatch returns). `loop.close()`
        # carries none of that blocking risk while still releasing the
        # per-call resources the leak was actually made of.
        #
        # `asyncio.set_event_loop(None)` afterward matters specifically
        # because the SERVED path's worker threads are reused: without it, a
        # thread's "current loop" thread-local would keep pointing at a
        # CLOSED loop object between calls — harmless in practice (this
        # function always installs a fresh loop via `set_event_loop` before
        # ever reading "the current loop" again), but leaving stale state
        # findable by some future addition to this function is not free
        # either, and `asyncio.run()` clears it in its own `finally` for the
        # same reason.
        #
        # try/finally (not the previous bare statement): close/reset must run
        # even if `dispatch_message` itself raised past its own documented
        # exception-to-JSON-RPC-error contract — the leak this fixes does not
        # get a pass just because that path is not expected to be reachable.
        _handler_stdout = io.StringIO()
        # The fifth dispatch entry point, and until now the only uninstrumented
        # one: `ipc.py` samples process time at `one_shot_cli` and `hook_batch`,
        # `warm/server.py` at `accept_thread` and `pool_worker`, and this branch
        # at none of them. Measured 2026-08-25 over a 24h window: every
        # `handoff.reconcile_open` and `write_surface.emit_manifest` row in the
        # sink arrives through here, so the ops furthest over the wall-clock bar
        # were the ops with no CPU sample at all — a brightline stated in
        # process time cannot be read against the ops that most need it.
        #
        # PROCESS_WIDE, not PER_OP_PROCESS: this branch is reached BOTH by the
        # standalone CLI process (where the delta would be this op's own CPU)
        # and by `invoke.from_argv` running on a REUSED ThreadPoolExecutor
        # thread inside the resident warm server (see the long comment below),
        # where sibling threads share one `time.process_time()` clock. The label
        # must hold for both callers, and under-claiming a sample's scope costs
        # a reader confidence while over-claiming costs them a wrong conclusion.
        _pt_t_start = _time.time()
        _pt_process_start = _time.process_time()
        try:
            with contextlib.redirect_stdout(_handler_stdout):
                response = loop.run_until_complete(dispatch_message(msg))
        finally:
            _record_dispatch_process_time(msg, _pt_t_start, _pt_process_start)
            loop.close()
            asyncio.set_event_loop(None)
        _captured = _handler_stdout.getvalue()
        if _captured:
            # Best-effort: the envelope below is the thing that must survive.
            # A raised BrokenPipeError from a caller that closed stderr early
            # must not prevent print(output) from running.
            try:
                sys.stderr.write(_captured)
                sys.stderr.flush()
            except OSError:
                pass

    # 8. Print result as indented JSON to stdout.
    #    Review: code-reviewer (nit) — an unguarded json.dumps that raises TypeError/ValueError
    #    (e.g. handler returns a Path, datetime, or other non-serializable object) would crash
    #    BEFORE sys.stdout.flush() + SystemExit, bypassing the flush-then-exit contract that the
    #    rest of this function establishes. Wrap in a try/except and route failures through
    #    _fatal_stderr (which flushes stderr and raises SystemExit(1) cleanly), avoiding silent
    #    partial-output corruption.
    # --bare (success path only): print just response["result"] with no
    # jsonrpc/id envelope and no indentation — the single-spawn transport
    # contract for callers like cc_invoke. The error path is intentionally
    # untouched below: it still prints the full envelope, because callers
    # distinguish success/error via exit code (and stderr), never by
    # inspecting stdout shape after a nonzero exit — so bare-vs-full only
    # needs to differ where the caller actually reads stdout.
    is_bare_success = args.bare and "error" not in response
    try:
        if is_bare_success:
            output = json.dumps(response["result"], ensure_ascii=False)
        else:
            output = json.dumps(response, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        _fatal_stderr(f"Handler returned non-serializable result: {exc}")
    print(output)

    # 9. Exit 0 on success result, 2 on a structural contract-pin error, 1 on any
    #    other JSON-RPC error result — see module docstring's "Exit codes" section
    #    and _exit_code_for_response's docstring.
    #    Explicit flush before SystemExit is mandatory — the real process boundary
    #    (main(), below) uses os._exit(), which does not flush stdio on its own.
    sys.stdout.flush()
    raise SystemExit(_exit_code_for_response(response, STRUCTURAL_PIN_ERROR))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Real-process wrapper: supply sys.argv/os.getcwd(), print, os._exit().

    Thin by design — all argv-parsing/dispatch/response-render logic lives in
    `_dispatch_argv` (and its `allow_warm=False` reuse by the warm server's
    `invoke.from_argv` op), so there is exactly one place that can diverge
    from the plain `python -m coordinator_core.invoke` CLI contract.
    """
    # Lazy op registration (F6 / claude-klabauter-windows-portability § C4) is
    # unconditional as of 2026-08-22 (the import-path-costs-nothing sprint):
    # coordinator_core.ops never eagerly imports its op modules at
    # package-init time, flag or no flag, so this process no longer needs to
    # arm anything before dispatch — the single dispatched op's owning module
    # is imported via ipc.py's registry-miss lazy-import path regardless.
    # (Formerly armed `sys._coordinator_core_lazy_ops` here; retired along
    # with the two-channel flag it fed — see coordinator_core/ops/__init__.py.)

    stdout, stderr, exit_code = _dispatch_argv(sys.argv[1:], os.getcwd())

    if stdout:
        sys.stdout.write(stdout)
    sys.stdout.flush()
    if stderr:
        # Best-effort, matching _dispatch_argv_body's own handler-stdout-relay
        # contract: the stdout envelope above is the thing that must survive.
        # A caller that closed/broke its stderr pipe (test coverage:
        # test_handler_stdout_relay_raising_still_yields_envelope_on_stdout)
        # must not prevent the already-written stdout envelope, or exit_code,
        # from completing this function.
        try:
            sys.stderr.write(stderr)
            sys.stderr.flush()
        except OSError:
            pass
    # os._exit bypasses atexit/finalizers and avoids any residual executor drain
    # (see _dispatch_argv_body step 7's comment on the deliberately-unclosed loop).
    os._exit(exit_code)


if __name__ == "__main__":
    main()
