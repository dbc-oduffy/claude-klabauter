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

`params.entrypoint` (optional, added for the multi-name native-invocation
surface — docs/research/spike-verdicts/2026-08-27-multi-name-native-invocation-
surface.md, chunk C0): when ABSENT, behaviour is completely unchanged from
before this field existed — dispatch runs through `coordinator_core.invoke.
__main__._dispatch_argv` exactly as always, i.e. the `python -m
coordinator_core.invoke <argv>` grammar. When PRESENT, it names a
`coordinator/bin/<entrypoint>.py` CLI (the basename the door itself was
launched/hardlinked as, per `door.c :: get_own_directory`'s
`GetModuleFileNameW` resolution — never a caller-suppliable translation) whose
OWN `main(argv) -> int` is run instead, in-process, under the same
stdout/stderr capture this op has always used. This is additive: the server
treats the field's absence as today's behaviour, so a door installed under its
current single name produces a request this op still handles identically.

Self-registration: importing this module calls register_op("invoke.from_argv", ...)
as a side-effect. coordinator_core.ops.__init__ imports this module (eager
path) and coordinator_core.ops._registry_map maps the op name to it (lazy
path), so the registration fires under either import strategy.

Negative-spec (RAG-bait):
    This handler does NOT reimplement argv parsing, repo-root resolution, or
    JSON-RPC envelope construction — that would be exactly the "second source
    of truth" this op exists to avoid. It is a thin adapter: validate the
    params, call `_dispatch_argv` (or the named entrypoint's own `main`),
    return a three-field dict. All of the CLI's actual behaviour (argparse
    grammar, --dump-op-timeouts, --params-file, --bare, exit-code selection,
    or — for a named entrypoint — that CLI's own verb/flag grammar) lives in
    its own module and is reached unmodified here.

    It does NOT build a mapping from forwarder argv to op argv — not a dict,
    not a prefix table, not a name→op constant. `params.entrypoint` names a
    CLI; that CLI's OWN parser (its `main(argv)`) does 100% of the argument
    interpretation. A translation table here would be the second grammar this
    design forbids (DR-347 Ruling 2).

    It does NOT substitute `coordinator-invoke.py` (or any other CLI) when
    the named entrypoint is not on the committed warm-load allowlist or its
    script is missing — see `_resolve_entrypoint_script` below: either case
    is a `ValueError`, surfaced as a JSON-RPC error envelope, never a silent
    fallback to a different CLI's grammar. That substitution is exactly the
    mis-dispatch this field exists to prevent — see `door.c`'s own
    fall_through()/basename-resolution comment.

    It does NOT warm-load a name absent from the committed
    `warm_entrypoint_allowlist.json` — resolving to a real
    `coordinator/bin/<name>.py` script and being SAFE to run inside the
    shared warm server process are different properties (import side
    effects, module-level I/O, interpreter-global mutation, hard `sys.exit`
    calls); the allowlist is the fail-closed gate on the second property,
    independent of whether the script exists on disk.

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
Spec backlink (entrypoint field): docs/research/spike-verdicts/2026-08-27-
    multi-name-native-invocation-surface.md, chunk C0
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from typing import Callable, Optional, cast

from coordinator_core.ipc import register_op

#: This file lives at `<engine_root>/coordinator_core/ops/invoke_from_argv.py`
#: — `parents[2]` is `<engine_root>` itself, the same root `coordinator/bin/`
#: hangs off in every published/live checkout. This process (the warm server)
#: is already running FROM that root, so no second root-resolution mechanism
#: (env var, sidecar, registry) is introduced here — it is simply this
#: module's own on-disk location, one hop up from `coordinator_core/`.
_ENGINE_ROOT = Path(__file__).resolve().parents[2]

#: Committed warm-load allowlist (chunk C1). Resolution and warm-safety are
#: different properties — resolving to a real `coordinator/bin/<name>.py`
#: script says nothing about whether that script's module body is SAFE to
#: load into the shared warm server process (~50 concurrent sessions), so
#: this file gates loading independently of `_resolve_entrypoint_main`'s own
#: existence check. Seeded here with only C0's proving CLI
#: (`cross-repo-memo`); chunk C2 populates the rest from its
#: `forwarder_door_census.py` "door-eligible" bucket. Read once at import
#: time — a fixed, committed set, not something a caller or a per-call read
#: should be able to influence.
_ALLOWLIST_PATH = Path(__file__).resolve().parent / "warm_entrypoint_allowlist.json"


def _load_allowlist() -> frozenset:
    with open(_ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return frozenset(data["entrypoints"])


_WARM_ENTRYPOINT_ALLOWLIST = _load_allowlist()


def _resolve_entrypoint_script(entrypoint: str) -> Path:
    """Validates `entrypoint` against the committed allowlist and against
    `coordinator/bin/<entrypoint>.py`'s on-disk existence, returning the
    script path. Does NOT load the module — see `_load_entrypoint_main`,
    which runs inside the op-boundary containment in `_run_entrypoint`.

    FAILS CLOSED on both checks: raises `ValueError` naming the image
    (`entrypoint`) and, respectively, the allowlist path or the missing
    script — never falls back to a different script (this module's own
    negative-spec above names the mis-dispatch that substitution would
    reproduce). A name failing the allowlist check never reaches the
    filesystem-existence check, and never reaches the loader at all —
    resolving to a real script is necessary but not sufficient to load it.
    """
    if entrypoint not in _WARM_ENTRYPOINT_ALLOWLIST:
        raise ValueError(
            f"invoke.from_argv: entrypoint {entrypoint!r} is not on the committed "
            f"warm-load allowlist ({_ALLOWLIST_PATH}) — refusing to warm-load an "
            f"unvetted CLI's module body into the shared server process"
        )

    script = _ENGINE_ROOT / "coordinator" / "bin" / f"{entrypoint}.py"
    if not script.is_file():
        raise ValueError(
            f"invoke.from_argv: no coordinator/bin CLI for entrypoint {entrypoint!r} "
            f"(expected {script}) — refusing rather than substituting a different "
            f"CLI's grammar"
        )
    return script


def _load_entrypoint_main(script: Path, entrypoint: str) -> Callable[[Optional[list]], int]:
    """Loads `script` and returns its `main` callable. Must only be called
    from inside `_run_entrypoint`'s op-boundary `try`/`except` — the module
    body itself is untrusted at THIS point (allowlist membership says a
    CLI's owner vetted it as warm-safe as of the last census, not that this
    exact load is guaranteed side-effect-free), so any `SystemExit` or other
    exception `exec_module` raises must be caught by the caller rather than
    propagating out of this function.

    Uses `importlib.util.spec_from_file_location`, not a normal `import`
    statement: every `coordinator/bin/*.py` file is a hyphenated filename,
    which has no ordinary `import x` form (see
    `coordinator_core/install/substrate.py`'s own `_load_setup_template_
    manifest`/`_load_bin_templates_manifest`, the established precedent for
    this exact load shape in this codebase).

    Raises `ValueError` (never caught by this function itself) when the
    module cannot be loaded from `script`, or loads but defines no callable
    `main`.
    """
    module_name = f"_invoke_from_argv_entrypoint_{entrypoint.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise ValueError(
            f"invoke.from_argv: could not load entrypoint {entrypoint!r} from {script}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    main_fn = getattr(module, "main", None)
    if not callable(main_fn):
        raise ValueError(
            f"invoke.from_argv: entrypoint {entrypoint!r} ({script}) defines no "
            f"callable main(argv) -> int"
        )
    return cast(Callable[[Optional[list]], int], main_fn)


def _run_entrypoint(entrypoint: str, argv: list, cwd: str) -> dict:
    """Runs `coordinator/bin/<entrypoint>.py`'s OWN `main(argv)` in-process.

    Chdir's to `cwd` (the door's cwd, never this server process's own cwd)
    for the duration of the call and restores it in a `finally` — every
    `coordinator/bin/*.py` CLI is written to run as a fresh subprocess
    inheriting the caller's cwd via `os.getcwd()`, and served in-process
    inside a long-lived warm pool worker there is no other channel by which
    this call can see the caller's cwd. Stdout/stderr are captured the same
    way `_dispatch_argv` captures the generic dispatcher's own prints, so a
    warm pool worker's real streams are never written to.

    OP-BOUNDARY CONTAINMENT: `_resolve_entrypoint_script` (allowlist +
    existence) runs first and is allowed to raise `ValueError` normally —
    that is fail-closed INPUT validation, surfaced as a standard JSON-RPC
    error envelope, same as `argv`/`cwd`. Loading the module and calling its
    `main` are different: an allowlisted CLI can still call `sys.exit` at
    import time, mutate interpreter globals, or raise mid-body, and none of
    that may be allowed to kill the shared warm server process that ~50
    concurrent sessions share. Both `_load_entrypoint_main` (the module-body
    exec) and `main_fn(argv)` therefore run inside ONE `try` that catches
    `SystemExit` and `Exception`, converting either into a `{"exit_code": N}`
    result the caller sees exactly as an ordinary CLI failure — never a
    JSON-RPC error, never a killed server.
    """
    script = _resolve_entrypoint_script(entrypoint)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    previous_cwd = os.getcwd()
    try:
        os.chdir(cwd)
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            try:
                main_fn = _load_entrypoint_main(script, entrypoint)
                exit_code = main_fn(argv)
            except SystemExit as exc:
                code = exc.code
                exit_code = 0 if code is None else (code if isinstance(code, int) else 1)
            except Exception as exc:
                print(
                    f"invoke.from_argv: entrypoint {entrypoint!r} raised "
                    f"{type(exc).__name__}: {exc}",
                    file=stderr_buf,
                )
                exit_code = 1
            else:
                if not isinstance(exit_code, int):
                    exit_code = 0 if exit_code is None else 1
    finally:
        os.chdir(previous_cwd)

    return {
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "exit_code": exit_code,
    }


@register_op("invoke.from_argv")
def _invoke_from_argv(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "invoke.from_argv" handler — dispatch a CLI-shaped argv, in-process.

    Params:
        argv: list[str] — the argv `python -m coordinator_core.invoke` (or,
              with `entrypoint` set, that named CLI) would receive, e.g.
              ["ping"], ["--bare", "coverage.gate", "{}"],
              ["--repo", "<abs-path>", "handoff.list"], ["--dump-op-timeouts"],
              or (entrypoint="cross-repo-memo") ["list"].
        cwd:  str — the CALLING process's cwd (the door's cwd — NEVER this
              server process's own cwd). Threaded through to
              `_dispatch_argv`/`_run_entrypoint`, which use it explicitly for
              repo_root resolution, relative `--repo`/`--params-file` values,
              the `_caller_cwd` telemetry stamp, or (entrypoint set) an
              explicit chdir, precisely so a warm pool worker's own cwd never
              corrupts those.
        entrypoint: Optional[str] — the door image's OWN basename (see this
              module's docstring). ABSENT: unchanged `_dispatch_argv` behaviour.
              PRESENT: names a `coordinator/bin/<entrypoint>.py` CLI whose own
              `main(argv)` runs instead — see `_run_entrypoint`.

    Returns:
        {"stdout": str, "stderr": str, "exit_code": int} — byte-identical to
        what the resolved CLI, run from `cwd`, would print to stdout/stderr
        and exit with (see this module's own docstring for the one
        deliberate internal-path divergence on the `_dispatch_argv` leg:
        skipping the warm-pipe preamble to avoid dialing this same server
        from inside itself).

    repo_root is unused — this op is "none"-scoped (op_scopes.py): the argv
    it dispatches may itself name a repo-scoped op, but THAT op's repo_root
    is resolved inside the re-entrant `_dispatch_argv` call (or the named
    entrypoint's own CLI) from the explicit `cwd` param above, never from
    this op's own repo_root.

    Raises ValueError — surfaces as a standard JSON-RPC error envelope via
    dispatch_message's handler-exception path, the same as every other op's
    params validation — if `argv` is not a list of strings, `cwd` is not a
    non-empty string, `entrypoint` is present but not a non-empty string, or
    (entrypoint set) the named CLI is not on the committed warm-load
    allowlist or cannot be resolved on disk (see
    `_resolve_entrypoint_script`). It does NOT raise for a failure inside the
    named CLI's own module body or `main` once resolution succeeds — those
    are contained at the op boundary and returned as a nonzero `exit_code`
    (see `_run_entrypoint`).
    """
    argv = params.get("argv")
    cwd = params.get("cwd")
    entrypoint = params.get("entrypoint")

    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ValueError("invoke.from_argv requires params.argv to be a list of strings")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("invoke.from_argv requires params.cwd to be a non-empty string")
    if entrypoint is not None and (not isinstance(entrypoint, str) or not entrypoint):
        raise ValueError(
            "invoke.from_argv requires params.entrypoint to be a non-empty string when present"
        )

    if entrypoint is not None:
        return _run_entrypoint(entrypoint, argv, cwd)

    from coordinator_core.invoke.__main__ import _dispatch_argv

    stdout, stderr, exit_code = _dispatch_argv(argv, cwd, allow_warm=False)
    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}
