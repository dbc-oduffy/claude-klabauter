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
import sys
import threading
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


class EntrypointNotWarmLoadableError(ValueError):
    """Raised by `_resolve_entrypoint_script` when a door image names a
    `coordinator/bin` CLI that is not on the committed warm-load allowlist.

    `entrypoint_not_warm_loadable = True` is a duck-type marker consumed by
    `ipc._handler_exception_error`, mirroring `CallerFacingValidationError`'s
    `caller_facing_validation` and `ContractPinError`'s `structurally_wedged`:
    an exception carrying it is answered with
    `ipc.ENTRYPOINT_NOT_WARM_LOADABLE_ERROR` (-32007) and the exception's own
    message, instead of the blanket -32603.

    THE CODE IS THE POINT, NOT THE MESSAGE. -32007 is in
    `door_core.c :: is_provably_undispatched`, so the native door reads this
    refusal as proof that nothing was imported and no `main` was invoked, and
    answers by running that name's CLI cold. -32603 is not, and could not be
    made so without also licensing a cold re-run of an op that may have half
    executed. See `ipc.ENTRYPOINT_NOT_WARM_LOADABLE_ERROR`'s docstring for why
    this distinction is what lets every `coordinator/bin` name share the one
    native door image.

    Subclasses `ValueError` so existing `pytest.raises(ValueError, ...)` call
    sites against this resolver keep passing.
    """

    entrypoint_not_warm_loadable = True


#: `os.chdir` is process-global — every thread in this warm server shares
#: ONE current working directory. A named entrypoint's own `main(argv)` is
#: a subprocess-shaped CLI that resolves its relative-path defaults (five
#: allowlisted CLIs carry argparse `default="."`, per
#: state/audits/2026-08-27-torn-read-hazard-sweep.md) via the process's
#: real OS cwd — there is no way to hand it an "explicit cwd" that a bare
#: `open("relative/path")` or a git subprocess launched with `cwd=None`
#: will actually honor short of making the OS-level chdir true for the
#: whole of that CLI's run. What this lock removes is the RACE named in
#: the audit: two concurrent `invoke.from_argv` calls, each `os.chdir`-ing
#: to a DIFFERENT caller's cwd, interleaving so that a session in the
#: engine repo has `coordinator/bin/**` rewritten under it by a session it
#: never dispatched. Holding this lock for the chdir/call/restore span
#: makes the process-global directory unambiguous for the caller who
#: legitimately owns it at any given instant — the shared process is never
#: read from or written to under a directory it wasn't told about.
_ENTRYPOINT_CWD_LOCK = threading.Lock()


def _resolve_entrypoint_script(entrypoint: str) -> Path:
    """Validates `entrypoint` against the committed allowlist and against
    `coordinator/bin/<entrypoint>.py`'s on-disk existence, returning the
    script path. Does NOT load the module — see `_load_entrypoint_main`,
    which runs inside the op-boundary containment in `_run_entrypoint`.

    FAILS CLOSED on both checks: raises naming the image (`entrypoint`) and,
    respectively, the allowlist path or the missing script — never falls back
    to a different script (this module's own negative-spec above names the
    mis-dispatch that substitution would reproduce). A name failing the
    allowlist check never reaches the filesystem-existence check, and never
    reaches the loader at all — resolving to a real script is necessary but
    not sufficient to load it.

    THE TWO REFUSALS ARE DIFFERENT CLASSES, AND THE DIFFERENCE IS LOAD-BEARING
    FOR THE DOOR (PM ruling 2026-08-29 — one native entrypoint per platform).

      - ALLOWLIST MISS raises `EntrypointNotWarmLoadableError`, which
        `ipc._handler_exception_error` maps to -32007 and `door_core.c ::
        is_provably_undispatched` classifies as safe to re-run cold. Nothing
        was imported and nothing was invoked, so the door answers by running
        that name's own CLI in a cold process. This is what lets EVERY
        `coordinator/bin` name carry the native door image: the allowlist is
        an optimization boundary (may this module body be imported into the
        shared server?), never an entrypoint-existence boundary. Under the
        previous blanket `ValueError`/-32603 the door could not prove the
        request was undispatched, refused with WARM_DISPATCH_INDETERMINATE,
        and a non-allowlisted name was therefore unable to use the door at
        all — which is precisely what kept the `.cmd` interpreter trampolines
        alive for those names.

      - MISSING SCRIPT stays a plain `ValueError` (-32603) and is NOT made
        fall-through-able. A door image whose `coordinator/bin/<name>.py`
        nor its extensionless `coordinator/bin/<name>` sibling exists is a
        broken install, not a warm-loadability question, and spawning a cold
        interpreter to rediscover the same absence buys an interpreter start
        to reach the identical failure.

    THE EXTENSIONLESS FALLBACK EXISTS FOR PARITY WITH THE COLD LEG
    (`door.c`/`door_posix.c` :: `fall_through`), NOT AS A NEW PREFERENCE.
    `<entrypoint>.py` is tried FIRST and always wins when both exist — a name
    that ships both a `.py` and an extensionless script keeps dispatching to
    the `.py`, matching every prior caller's assumption. The extensionless
    candidate is checked only when the `.py` is absent, and only a REGULAR
    FILE at that path counts (a directory or symlink-to-directory at the
    extensionless name is not a script and must not resolve).
    """
    if entrypoint not in _WARM_ENTRYPOINT_ALLOWLIST:
        raise EntrypointNotWarmLoadableError(
            f"invoke.from_argv: entrypoint {entrypoint!r} is not on the committed "
            f"warm-load allowlist ({_ALLOWLIST_PATH}) — refusing to warm-load an "
            f"unvetted CLI's module body into the shared server process; the door "
            f"runs this name cold instead"
        )

    bin_dir = _ENGINE_ROOT / "coordinator" / "bin"
    script = bin_dir / f"{entrypoint}.py"
    if script.is_file():
        return script

    extensionless = bin_dir / entrypoint
    if extensionless.is_file():
        return extensionless

    raise ValueError(
        f"invoke.from_argv: no coordinator/bin CLI for entrypoint {entrypoint!r} "
        f"(expected {script} or {extensionless}) — refusing rather than "
        f"substituting a different CLI's grammar"
    )



def _ensure_bin_dir_importable() -> None:
    """Puts `coordinator/bin` on `sys.path` once, so a loaded entrypoint can
    `from lib.<module> import ...`.

    Why this exists: every bin CLI used to carry its own three-line
    `sys.path.insert(0, <bin>/lib)` preamble, executed at module scope inside
    this shared warm server — 273 entrypoints each mutating interpreter global
    state on import, which is the AC20 impurity and the (b) warm-loadable
    hazard the census exists to exclude. The bootstrap now lives in exactly one
    place (`coordinator/bin/lib/__init__.py`) and this function is what makes
    that package reachable on the warm path. On the CLI path nothing is needed:
    a script's own directory is already `sys.path[0]`.

    Idempotent and cheap — a membership test on every call after the first.
    Negative-spec: this does NOT add `<bin>/lib` itself. That stays the lib
    package's own job, so there is one bootstrap, not two competing ones.
    """
    bin_dir = str(_ENGINE_ROOT / "coordinator" / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

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
    _ensure_bin_dir_importable()
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


#: `(mtime_ns, size) -> shape` per script path. The shape is a property of
#: the file's source, so it is re-derived whenever the file changes on disk
#: and never otherwise — a warm server that outlives a publish must not keep
#: serving the pre-publish shape. Keyed on the stat pair rather than the path
#: alone for exactly that reason.
_ARGV_SHAPE_CACHE: dict[tuple[str, int, int], str] = {}


def _entrypoint_argv_shape(script: Path) -> str:
    """Which `serve_classifier.ARGV_SHAPE_*` this script's own `__main__`
    guard uses, or `ARGV_SHAPE_TAIL` when the file cannot be read or parsed.

    An unreadable/unparseable file is not an error here: `_load_entrypoint_
    main` is about to fail on it anyway, with a better message, and tail is
    the behaviour-preserving default this whole mechanism degrades to.
    """
    # Imported here, not at module scope: this module is on the warm-reach
    # entry path, which carries a 20ms import-CPU ceiling
    # (`test_warm_reach_import_ceiling.py`, AC3), and `ast` alone spends most
    # of it. Both are needed once per distinct script per warm process and
    # are cached by `sys.modules` from then on.
    import ast

    from coordinator_core.warm import serve_classifier

    try:
        stat = script.stat()
        key = (str(script), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return serve_classifier.ARGV_SHAPE_TAIL
    cached = _ARGV_SHAPE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        shape = serve_classifier.ARGV_SHAPE_TAIL
    else:
        shape = serve_classifier.classify_main_argv_shape(tree)
    _ARGV_SHAPE_CACHE[key] = shape
    return shape


def entrypoint_call_args(shape: str, script: Path, argv: list) -> tuple:
    """The positional arguments the warm route hands `main`, given the shape
    its own `__main__` guard uses. THE decision, isolated as a pure function
    so the route-parity suite can assert it per CLI against each file's real
    guard expression without loading 365 module bodies to find out (see
    `coordinator_core/warm/tests/test_entrypoint_argv_route_parity.py`).

    The door relays `argv[1:]` — argv[0] never crosses the wire (`door.c`) —
    so `argv` here is always the bare argument list, and this function's job
    is to re-derive what the file's cold route would have handed `main` from
    the same arguments:

      - FULL (`main(sys.argv)`): the callee slices `[1:]` itself, so it needs
        the program name back in front or it eats its own first real argument
        (the "unknown subcommand" symptom on every ceremony CLI).
      - NONE (`main()`): the callee reads `sys.argv` internally; the caller's
        arguments reach it through `_run_entrypoint`'s `sys.argv` assignment,
        not through a parameter, so passing one here would be inventing an
        argument the cold route never passes.
      - TAIL (everything else): bare arguments, unchanged — what the door
        already did before any of this existed.

    Negative-spec (RAG-bait): this is NOT a per-name table, and NOT a
    forwarder-argv-to-op mapping (the mis-dispatch `invoke.from_argv`'s own
    module docstring forbids). Nothing here is keyed on a CLI's identity —
    only on a shape read off that CLI's own source, which is the same line
    that defines its cold behaviour. A file that changes its `__main__` guard
    changes this answer with it, with no registry to keep in sync.
    """
    from coordinator_core.warm import serve_classifier

    if shape == serve_classifier.ARGV_SHAPE_FULL:
        return ([str(script)] + list(argv),)
    if shape == serve_classifier.ARGV_SHAPE_NONE:
        return ()
    return (list(argv),)


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

    The caller's SESSION IDENTITY is already true in `os.environ` by the time
    this function runs — `coordinator_core.warm.entry_seam.per_request_state`
    (`_environ_identity_borrow`) mirrors it in for the whole isolated dispatch
    this op runs inside, for the same reason cwd and `sys.argv` are borrowed
    here directly: these CLIs read `os.environ` directly, and inside the warm
    server that environment belongs to whoever spawned it. This function does
    not repeat that borrow — doing so here as well would be a second
    implementation of the same rule, at a narrower scope than the seam that
    already makes it true.

    The chdir/call/restore span runs under `_ENTRYPOINT_CWD_LOCK` — see that
    lock's own comment for why: `os.chdir` mutates process-global state in a
    process ~50 sessions share, so two concurrent calls with different
    `cwd`s would otherwise race, each observing (or clobbering) the other's
    directory mid-call. The lock resolves the caller's cwd explicitly for
    the sole duration it is entitled to it, rather than leaving the
    process's actual directory to whichever caller chdir'd last.

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
    # `--help`/`-h` chokepoint (mirrors `entry_point_shim._help_requested`'s
    # semantics: anywhere in argv wins, position never special-cased). This
    # MUST run before `_resolve_entrypoint_script`/`_load_entrypoint_main` —
    # those load the target module and, for an ARGV_SHAPE_NONE entrypoint
    # (e.g. `workday-start-inbox-blitz-assemble`), `entrypoint_call_args`
    # would hand `main_fn` no argv at all, discarding `--help` before the
    # target ever sees it (its own `if __name__ == "__main__":` guard never
    # runs here either, since the module is loaded via `exec_module`, not
    # executed as `__main__`). Uniform for every warm-served entrypoint — no
    # name checks, no per-CLI branch. `main_fn` is never called on this path.
    if any(a in ("--help", "-h") for a in argv):
        return {
            "stdout": f"usage: {entrypoint} [--help]\n",
            "stderr": "",
            "exit_code": 0,
        }

    script = _resolve_entrypoint_script(entrypoint)
    shape = _entrypoint_argv_shape(script)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with _ENTRYPOINT_CWD_LOCK:
        previous_cwd = os.getcwd()
        previous_sys_path = list(sys.path)
        previous_sys_argv = list(sys.argv)
        try:
            os.chdir(cwd)
            # WHY sys.argv IS SET, not just passed as a parameter. Served
            # in-process, `sys.argv` is the warm SERVER's own command line --
            # so any CLI that reads it directly sees the server's arguments
            # instead of the caller's, silently. That is the majority shape in
            # `coordinator/bin`: 218 entrypoints end in `sys.exit(main())` and
            # resolve their own argv internally, and one calls a bare
            # `parser.parse_args()` mid-body after correctly accepting an argv
            # parameter -- which is why `coordinator-queue-append` reached its
            # parser with the caller's arguments missing entirely, and why
            # prepending a dummy token did not shift them back. Restored in the
            # `finally` beside cwd and sys.path, and mutated only under
            # `_ENTRYPOINT_CWD_LOCK` for the same reason chdir is: this process
            # is shared by ~50 sessions, so process-global state may only be
            # borrowed for the span a single caller is entitled to it.
            #
            # This assignment is also what makes the NONE shape's parameter
            # list correct: `entrypoint_call_args` passes those entrypoints
            # nothing, because `sys.argv` is the channel their own cold route
            # uses. It covers what a `__main__` guard cannot state, too --
            # `coordinator-queue-append` accepts an `argv` parameter and then
            # calls a bare `parser.parse_args()` mid-body, and no shape read
            # off its guard would reveal that.
            sys.argv = [str(script)] + list(argv)
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(
                stderr_buf
            ):
                try:
                    main_fn = _load_entrypoint_main(script, entrypoint)
                    exit_code = main_fn(*entrypoint_call_args(shape, script, argv))
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
            sys.path[:] = previous_sys_path
            sys.argv[:] = previous_sys_argv

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
