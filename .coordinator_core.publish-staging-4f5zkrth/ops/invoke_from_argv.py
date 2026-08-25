"""
coordinator_core.ops.invoke_from_argv — JSON-RPC "invoke.from_argv" operation.

Purpose: server-side entrypoint for the native warm-engine door
(coordinator_core/warm/door/, a sibling component). Reaching an already-warm
server today costs a fresh cmd.exe shim plus a fresh Python interpreter on
every call — the "door" is cold even though the engine behind it is warm
(docs/decisions — see the 2026-08-21 "reaching the warm engine" baton). The
native door fixes that by being a thin C client that relays raw argv over the
existing named pipe instead of resolving/interpreting the CLI's own argument
grammar a second time — a second parser would silently diverge from
`coordinator_core.invoke.__main__`'s over time. This op is the "the server
does the translation" half of that contract: it runs the SAME argv-parsing /
op-dispatch / response-render core the `python -m coordinator_core.invoke`
CLI uses (`coordinator_core.invoke.__main__._dispatch_argv`), inside the
already-running warm server process, and returns the CLI's stdout / stderr /
exit_code as ordinary JSON-RPC result data instead of the CLI's
print()-to-real-streams / os._exit() side effects.

Self-registration: importing this module calls register_op("invoke.from_argv", ...)
as a side-effect. coordinator_core.ops.__init__ imports this module (eager
path) and coordinator_core.ops._registry_map maps the op name to it (lazy
path), so the registration fires under either import strategy.

Negative-spec (RAG-bait):
    This handler does NOT reimplement argv parsing, repo-root resolution, or
    JSON-RPC envelope construction — that would be exactly the "second source
    of truth" this op exists to avoid. It is a thin adapter: validate the two
    params, call `_dispatch_argv`, return its three-tuple as a dict. All of
    the CLI's actual behaviour (argparse grammar, --dump-op-timeouts,
    --params-file, --bare, exit-code selection) lives in `__main__.py` and is
    reached unmodified here.

    It does NOT go through the warm-pipe preamble a second time
    (`_dispatch_argv(..., allow_warm=False)`): this op is itself being served
    BY the warm server, so dialing the same server's own pipe from inside a
    request it is already handling would be a pointless (and, depending on
    the pipe accept loop's concurrency model, possibly self-blocking) extra
    hop. See `_dispatch_argv`'s docstring for why this is an internal-path
    divergence only — the returned stdout/stderr/exit_code are byte-identical
    to the cold CLI's either way, because the warm preamble's own contract is
    to be transparent to the dispatched response.

Spec backlink: state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op


@register_op("invoke.from_argv")
def _invoke_from_argv(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "invoke.from_argv" handler — dispatch a CLI-shaped argv, in-process.

    Params:
        argv: list[str] — the argv `python -m coordinator_core.invoke` would
              receive, e.g. ["ping"], ["--bare", "coverage.gate", "{}"],
              ["--repo", "<abs-path>", "handoff.list"], ["--dump-op-timeouts"].
        cwd:  str — the CALLING process's cwd (the door's cwd — NEVER this
              server process's own cwd). Threaded through to
              `_dispatch_argv`, which uses it explicitly for repo_root
              resolution, relative `--repo`/`--params-file` values, and the
              `_caller_cwd` telemetry stamp, precisely so a warm pool
              worker's own cwd never corrupts those.

    Returns:
        {"stdout": str, "stderr": str, "exit_code": int} — byte-identical to
        what `python -m coordinator_core.invoke <argv...>` run from `cwd`
        would print to stdout/stderr and exit with (see this module's own
        docstring for the one deliberate internal-path divergence: skipping
        the warm-pipe preamble to avoid dialing this same server from
        inside itself).

    repo_root is unused — this op is "none"-scoped (op_scopes.py): the argv
    it dispatches may itself name a repo-scoped op, but THAT op's repo_root
    is resolved inside the re-entrant `_dispatch_argv` call from the
    explicit `cwd` param above, never from this op's own repo_root.

    Raises ValueError — surfaces as a standard JSON-RPC error envelope via
    dispatch_message's handler-exception path, the same as every other op's
    params validation — if `argv` is not a list of strings or `cwd` is not a
    non-empty string.
    """
    argv = params.get("argv")
    cwd = params.get("cwd")

    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ValueError("invoke.from_argv requires params.argv to be a list of strings")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("invoke.from_argv requires params.cwd to be a non-empty string")

    from coordinator_core.invoke.__main__ import _dispatch_argv

    stdout, stderr, exit_code = _dispatch_argv(argv, cwd, allow_warm=False)
    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}
