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
                         DISPATCH_TIMEOUT_SECS>} to stdout and exit 0. No <op>
                         required. Lets an external caller (e.g. DoE's
                         cc_invoke, which applies a flat 10s cap) read
                         claude-klabauter's real per-op dispatch-timeout budgets instead
                         of guessing. Source of truth is
                         coordinator_core.ipc.OP_TIMEOUT_OVERRIDES /
                         DISPATCH_TIMEOUT_SECS — the SAME module constants
                         _timeout_for() uses, re-read live at call time (this
                         process is spawn-per-call per DR-215, so there is no
                         stale-value risk, but DISPATCH_TIMEOUT_SECS is
                         env-resolved at import time within THIS process, so
                         reading it here reflects the actual env this process
                         saw).
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
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional


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
            "\"__default__\": <live DISPATCH_TIMEOUT_SECS>}. Takes priority "
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
    return p


# ---------------------------------------------------------------------------
# --dump-op-timeouts read-only surface
# ---------------------------------------------------------------------------

def _dump_op_timeouts() -> dict:
    """Build the op->timeout-budget JSON payload from the live dispatcher source of truth.

    Reads coordinator_core.ipc.OP_TIMEOUT_OVERRIDES (the same MappingProxyType
    _timeout_for() reads for per-op overrides) and DISPATCH_TIMEOUT_SECS (the
    same module-level constant _timeout_for() falls back to). No second source
    of truth is introduced -- this is a read-only projection of the dispatcher's
    own timeout table, re-read live at call time rather than hardcoded, so a
    process started with an overridden COORDINATOR_DISPATCH_TIMEOUT_SECS env
    var reports the value it actually resolved to.

    Returns:
        dict -- {"<op>": <float>, ..., "__default__": <float>}. "__default__"
        is the reserved key for the global runaway-guard fallback.
    """
    from coordinator_core.ipc import DISPATCH_TIMEOUT_SECS, OP_TIMEOUT_OVERRIDES

    payload = dict(OP_TIMEOUT_OVERRIDES)
    payload["__default__"] = DISPATCH_TIMEOUT_SECS
    return payload


# ---------------------------------------------------------------------------
# Repo-root resolution
# ---------------------------------------------------------------------------

def _resolve_repo_root(repo_arg: Optional[str]) -> Path:
    """Resolve repo_root from --repo arg or git rev-parse --show-toplevel.

    Negative-spec: implicit os.getcwd() fallback is PROHIBITED (AC-5 migration).
    If repo_root cannot be resolved, emits a JSON error envelope to stderr and
    exits non-zero — never silently defaults to a fallback path.

    Returns:
        Path — canonical, resolved repo root.
    """
    if repo_arg is not None:
        return Path(repo_arg).resolve()

    # Explicit find_repo_root() over the process cwd — NOT a raw os.getcwd() use.
    from coordinator_core.lifecycle import find_repo_root  # deferred: no side effects at import time
    try:
        return find_repo_root()
    except RuntimeError as exc:
        _fatal_stderr(
            f"repo_root unresolvable: not inside a git working tree and --repo was not "
            f"provided. Run from a git repo or pass --repo <path>. Detail: {exc}"
        )
        raise SystemExit(1)  # unreachable — _fatal_stderr exits; satisfies static checkers


# ---------------------------------------------------------------------------
# Fatal error helper
# ---------------------------------------------------------------------------

def _fatal_stderr(message: str) -> None:
    """Emit a JSON-RPC 2.0 error envelope to stderr and exit non-zero.

    Used for pre-dispatch failures (bad args, unresolvable repo_root) so that
    callers can distinguish infrastructure errors (stderr, exit 1) from op-level
    errors (stdout, exit 1 with a JSON-RPC error response).
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
    os._exit(1)


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
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args, build JSON-RPC request, dispatch in-process, print result."""
    # Lazy op registration (F6 / claude-klabauter-windows-portability § C4): tell
    # coordinator_core.ops to SKIP its eager 55-module import list at package-
    # init time. This process is a one-shot command-type CLI dispatch (DR-215)
    # — nothing else in it depends on the full-eager side-effect — so only the
    # single dispatched op's owning module gets imported (via ipc.py's
    # registry-miss lazy-import path), instead of all ~55. Must be set before
    # anything imports coordinator_core.ops (directly or transitively).
    #
    # On `sys`, not in `os.environ` (2026-07-28): this signal is for THIS
    # process only, and an os.environ write is inherited by every child spawned
    # without an explicit `env=` — including the pytest runs three op modules
    # spawn (cutover_gate, copy_plugin_template, whoami_run_tests), whose
    # collection then fails against the 59 modules that assert the op registry
    # at import time. A `sys` attribute crosses no process boundary.
    # Scoping study: docs/research/2026-07-28-lazy-ops-import-side-effect-scope.md § 6 (c).
    # COORDINATOR_CORE_LAZY_OPS in the environment stays the operator override
    # and, per coordinator_core/ops/__init__.py, outranks this line — so an
    # operator who exports it as "0" gets an eager dispatch process, where this
    # write was previously unconditional.
    setattr(sys, "_coordinator_core_lazy_ops", True)

    parser = _build_arg_parser()
    args = parser.parse_args()

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
            return  # unreachable after _fatal_stderr; pacifies type checkers
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        sys.stdout.flush()
        os._exit(0)

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
        return  # unreachable after _fatal_stderr; pacifies type checkers

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
                return  # unreachable after _fatal_stderr; pacifies type checkers
        else:
            try:
                params_json_str = Path(args.params_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _fatal_stderr(f"Cannot read --params-file {args.params_file!r}: {exc}")
                return  # unreachable after _fatal_stderr; pacifies type checkers
    elif args.params_json is not None:
        params_json_str = args.params_json
    else:
        params_json_str = "{}"

    # 2. Parse params JSON — must be a JSON object.
    try:
        params = json.loads(params_json_str)
    except json.JSONDecodeError as exc:
        _fatal_stderr(f"Invalid params_json: {exc}")
        return  # unreachable after _fatal_stderr; pacifies type checkers

    if not isinstance(params, dict):
        _fatal_stderr(
            f"params_json must be a JSON object (dict); got {type(params).__name__}"
        )
        return

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
    if args.repo is not None and args.op not in WORKTREE_SCOPED_OPS:
        from coordinator_core.op_scopes import OP_KEY_SCOPE
        op_scope = OP_KEY_SCOPE.get(args.op, "none")
        _fatal_stderr(
            f"--repo is meaningless for op {args.op!r} (scope={op_scope!r}): this op "
            f"accesses no repo-specific state and never reads repo_root, so --repo "
            f"would silently no-op. Omit --repo for this op. See "
            f"docs/decisions/DR-279-repo-on-a-none-scoped-op-fails-loud.md."
        )
        return  # unreachable after _fatal_stderr; pacifies type checkers

    # 4. Resolve repo_root — ONLY for worktree-scoped ops; fail-loud if unresolvable (AC-5).
    #    For all other scopes (none, central) repo_root stays None — git is never invoked.
    if args.op in WORKTREE_SCOPED_OPS:
        repo_root: Optional[Path] = _resolve_repo_root(args.repo)
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

    # 7. Dispatch in-process via dispatch_message (async, no socket, no auth gate).
    #    Manual loop instead of asyncio.run() to avoid executor drain on the timeout
    #    path (P1#5): asyncio.run() calls loop.shutdown_default_executor() which joins
    #    any live threads — a handler that asyncio.to_thread'd and then timed out leaves
    #    a live executor thread that would cause asyncio.run() to block indefinitely.
    #    os._exit() below terminates the process before that join happens.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    response = loop.run_until_complete(dispatch_message(msg))
    # Deliberately DO NOT call loop.close() / shutdown_default_executor() here:
    # a handler that internally asyncio.to_thread'd and then timed out leaves a
    # live executor thread the graceful drain would block on (P1#5). os._exit below
    # terminates the process without that join.

    # 8. Print result as indented JSON to stdout.
    #    Review: code-reviewer (nit) — an unguarded json.dumps that raises TypeError/ValueError
    #    (e.g. handler returns a Path, datetime, or other non-serializable object) would crash
    #    BEFORE sys.stdout.flush() + os._exit(), bypassing the flush-then-exit contract that the
    #    rest of main() establishes. Wrap in a try/except and route failures through _fatal_stderr
    #    (which flushes stderr and os._exit(1)s cleanly), avoiding silent partial-output corruption.
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
        return  # unreachable after _fatal_stderr; pacifies type checkers
    print(output)

    # 9. Exit 0 on success result, 2 on a structural contract-pin error, 1 on any
    #    other JSON-RPC error result — see module docstring's "Exit codes" section
    #    and _exit_code_for_response's docstring.
    #    os._exit bypasses atexit/finalizers and avoids any residual executor drain.
    #    Explicit flush before os._exit is mandatory — os._exit does not flush stdio.
    sys.stdout.flush()
    os._exit(_exit_code_for_response(response, STRUCTURAL_PIN_ERROR))


if __name__ == "__main__":
    main()
