"""coordinator_core.ceremony_common.cli_dispatch — the shared in-process CLI
dispatch primitive for the `workday_complete`/`workstream_complete`/
`workweek_complete` `apply.py` trio's `_load_cli_module`/`_invoke_cli_main`
seam, lifted to the superset of the three private copies (staff-eng review,
accepted — see the C1 dispatch brief this module was built from).

ADDITIVE ONLY. Nothing in the trio is repointed at this module in this
chunk — each of the three keeps its own private `_load_cli_module`/
`_invoke_cli_main` (candidate for a later cut, C5). This module exists so a
later chunk (C2) can converge the three onto ONE shared implementation
rather than three independently-drifting copies, mirroring
`cli_rejection.py`'s own "the ONE shared implementation... not three
patched copies" precedent in the same package.

Source of truth: `workday_complete.apply._invoke_cli_main` (99 lines) is
the richest of the three — 4-tuple return carrying BOTH captured stdout and
stderr, a stdin swap, and the zero-arg-trampoline argv splice — so this
module's `invoke_cli_main` is built as that shape. `workstream_complete.
apply._invoke_cli_main` (142 lines, the largest of the three) was read in
full before finalising this signature; the extra length there is arg-token
substitution machinery layered ABOVE `_invoke_cli_main` in that file (in
`_execute_directives`), not inside the function itself — `_invoke_cli_main`
proper does not carry stdin support, matching neither more nor less than
noted in the Contested Behaviours section below.

ENGINE root, not the target repo root and not process cwd. The scripts
this module loads are ENGINE-PROVISIONED: `coordinator/bin/` ships with
the claude-klabauter install this module is part of, and by construction does not
exist in a consumer repo. `resolve_cli_script_root` therefore computes
`Path(__file__).resolve().parents[2] / "coordinator" / "bin"` from this
module's own location — the same depth every trio `apply.py` uses, and
the same idiom as the `_BIN_DIR` constants in `merge_assemble`'s
package `__init__` and its `apply` module. It takes NO `repo_root`: a caller's
`repo_root` names the repo the CLI OPERATES ON (threaded through `args`
and, for spawning callers, `cwd`), never the tree the CLI SHIPS IN.
Conflating the two is the defect this signature exists to make
unspellable — an earlier `resolve_cli_script_root(repo_root)` joined
`coordinator/bin` onto the consumer root, which aborted the merge
ceremony's `apply` at its first in-process directive in every repo that
is not the engine checkout (three independent consumer repos reported it
on 2026-09-02). `Path.cwd()` remains wrong for the separate reason
below.

CWD IS A LOAD-BEARING GAP THE TRIO NEVER HAD. Verified against the actual
emitted directives (`build_directives(Path('.'), tag_prefix='v',
proposed_tag='v1.2.3')`): `d6 check-no-illegal-paths` and `d1`/`d2
merge-recovery-and-tag-cut` (`d2` is `cut-tag v1.2.3`) all currently rely
on ambient process cwd absent an explicit arg, masked today only because
today's spawn path runs them via `_run_py_script(cwd=repo_root)` — a
subprocess with an explicit working directory. This module never spawns a
subprocess and never calls `os.chdir`; it must not be assumed to establish,
preserve, or repair a cwd invariant for anything it loads or invokes. A
caller merging directives like the three named above onto this primitive
(C2's job, not this chunk's) must resolve that gap itself — by passing an
explicit path argument through `args`, never by relying on this module to
chdir first.

What this module does NOT isolate (say so explicitly, so the next reader
never assumes otherwise): process cwd (see above — never read, never
mutated); `sys.path` beyond `load_cli_module`'s own transient insert of the
loaded script's directory for the duration of `exec_module` (see
`_exec_with_own_dir_on_path` — removed by value before the call returns, and
an invoked script's own top-level imports otherwise run exactly as they
would un-isolated); `os.environ` (read/written by the invoked script
exactly as ambient); any module-level side effect the invoked script's own
top-level code performs at `exec_module` time; and any non-`SystemExit`
exception raised either at import time (`exec_module`) or from `main()`
itself — both propagate straight to this module's caller, uncaught; and
concurrency — `invoke_cli_main` swaps `sys.argv`/`sys.stdin` at
process-global scope for the duration of a call (restored in `finally`),
so this primitive requires single-threaded, non-reentrant callers. Two
concurrent `invoke_cli_main` calls race on both globals.

Contested behaviours across the trio (named per `docs/wiki/record-at-
write-time.md` § Two negative lessons — a lift that silently picks a
winner on a genuine conflict is the failure that doctrine names):

    1. Return shape. `workday_complete`/`workstream_complete` both return a
       4-tuple `(exit_code, stdout, stderr, exit_class)`; `workweek_complete`
       returns a 3-tuple `(exit_code, stderr, exit_class)` and never
       captures stdout at all (its own docstring: "this chunk's directives
       never consume another directive's stdout, so that capture was never
       added here"). GENUINE CONFLICT — this is not a wording difference,
       it is an absent capability in one of the three. The superset sides
       with the 4-tuple (2-of-3, and the richer shape is a strict superset
       of the 3-tuple: a caller that never reads `stdout` pays only an
       unused string).
    2. Stdin wiring. Only `workday_complete` swaps `sys.stdin` for a
       caller-supplied `stdin_text` (its own `stdin_from` directive
       family). Neither `workstream_complete` nor `workweek_complete`
       carries this at the `_invoke_cli_main` level at all — `workstream_
       complete` achieves inter-directive data flow via captured STDOUT
       fed into arg-token substitution one layer up, in
       `_execute_directives`, never via stdin. GENUINE CONFLICT of
       capability, not wording. The superset carries `stdin_text` as an
       optional keyword, defaulting to `None` (untouched `sys.stdin`) so a
       caller with no stdin story pays nothing.
    3. Module loader. `workday_complete._load_cli_module` calls
       `importlib.util.spec_from_file_location(module_name, script_path)`
       with NO explicit loader. `workstream_complete`/`workweek_complete`
       both pass an explicit `importlib.machinery.SourceFileLoader`,
       because `spec_from_file_location` cannot infer a source loader from
       an extensionless path and some consumes-manifest scripts are
       bareword launcher shims with no `.py` suffix. GENUINE CONFLICT — a
       real capability gap in `workday_complete`'s copy, not a stylistic
       difference (2-of-3 have it, and the gap is a real bug against
       extensionless scripts). The superset always passes the explicit
       `SourceFileLoader`, matching the 2-of-3 and covering the strictly
       larger set of script shapes.
    4. `sys.modules` registration order. All three register the freshly
       created module in `sys.modules` BEFORE calling `exec_module` — this
       is NOT a conflict, all three already carry the identical fix
       (dataclass class-body execution resolves `sys.modules[cls.
       __module__]` during `exec_module` and crashes with an unrelated
       AttributeError if the module isn't registered yet). This module
       carries the same order for the same reason; do not re-derive it.

THE TWO-ROOT MODEL (added by
docs/plans/2026-09-07-directive-resolution-reaches-a-plugin-local-cli.md T1;
see docs/reference/plugin-local-cli-dispatch.md for the full route). This
module now resolves TWO distinct producer roots, never conflated:

    1. `resolve_cli_script_root()` (unchanged, AC1) -- the ENGINE root,
       `coordinator/bin` under THIS module's own clone. Always resolvable:
       the engine that runs this code ships that directory by construction.
    2. `resolve_plugin_cli_script_root()` -- a SECOND, DoE-anchored root,
       `<doe_root>/coordinator/bin`, resolved via
       `coordinator_core.ops.coordinator_doe_root.coordinator_doe_root_in_process()`.
       Optional by construction: a box with no DoE-claude clone has no such
       root, and the function returns `None` rather than guessing or raising.

THE RUNG CUT (why `resolve_plugin_cli_script_root()` does not simply call
`coordinator_doe_root()`). The full ladder's rung 3 delegates to
`resolve_coordinator_clone.resolve_clone_root()`, which retains a
`subprocess.run` fallback. `resolve_plugin_cli_script_root()` consults rungs
1, 2, 2.5 and 2.75 ONLY (`coordinator_doe_root_in_process()`) and returns
`None` rather than descending to rung 3 -- so resolving this second root is
zero-spawn on EVERY box, resolvable or not. A box that needs a subprocess to
find DoE is a box where plugin-local dispatch should be off; the sentinel
below already handles `None` without loss.

THE SENTINEL'S CONTRACT. `UNRESOLVED_PLUGIN_CLI_ROOT` is a module-level
literal `Path` whose last path segment is `bin`, at a location guaranteed not
to exist. It exists so a consumer of the optional second root (a
`dict[str, Path]`-typed dispatch table, e.g.) can keep a non-optional `Path`
value without inventing its own DoE-root join -- `<script root> or
UNRESOLVED_PLUGIN_CLI_ROOT` -- and it is owned here, not by any caller,
because the one-definition guard
(`ceremony_common/test_producer_root_has_one_definition.py`) exists precisely
to refuse a second module spelling its own DoE-root join.

`resolve_plugin_cli_script_root()` never returns a path it has not itself
seen on disk: the joined `<doe_root>/coordinator/bin` must be a real
directory (one `is_dir()` inside the resolver), which also covers the stale
or moved DoE clone -- a resolved-but-gone root is treated exactly like an
unresolvable one, never surfaced as a `FileNotFoundError` three calls later.
The join is deliberately NOT layout-aware: a flat OSS/marketplace root (whose
`schemas/` and `bin/` sit directly under it, no `coordinator/` subdirectory)
joins to a `coordinator/bin` that does not exist, so it is never an
admissible plugin-local dispatch source even when `coordinator_doe_root_in_
process()` itself resolves it (at rung 2.75). Admitting a published mirror's
scripts would be a product decision about consumer boxes this module does
not make.

`sys.path` FINDING, FOLDED INTO THE EXISTING NEGATIVE SPEC BELOW: a
plugin-local script resolved through this second root is loaded through the
SAME `load_cli_module()` as any engine-local one, so it gets the same
transient `sys.path` insert of its own directory for the duration of
`exec_module` -- and nothing more. A DoE-side script that itself mutates
`sys.path` at import (several do -- see the plan's own census) is NOT
isolated by this module; that hazard belongs to the loaded script, not to
this dispatch primitive.

TWO CACHE LAYERS, NOT ONE (post-C5 note — the trio is now repointed at
this module; the paragraph above describing "ADDITIVE ONLY... nothing in
the trio is repointed" is C1-era history, not current state). Each trio
member (`workday_complete`/`workstream_complete`/`workweek_complete`)
still keeps its OWN private `_LOADED_MODULES` dict, keyed by `cli_name`
(the caller-chosen manifest name) — that per-module cache is checked
FIRST and short-circuits before this module is ever called, so this
module's own `_LOADED_MODULES` (below) is only consulted on a per-module
cache MISS. The two layers are keyed on DIFFERENT axes: the trio's
per-module caches key by `cli_name`/`module_name` (a caller-chosen
string); this module's cache keys by the RESOLVED ABSOLUTE `script_path`.
Neither cache is ever cleared, and clearing one does NOT clear or
invalidate the other — a script edited on disk mid-process can be stale
in one layer, both, or (in principle, if a future caller varies
`module_name` for the same path) neither in sync with the other. Do not
"fix" one layer's staleness without accounting for the other; they are
independent, not a single logical cache split across two dicts.

Spec backlink:
docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md, chunk C1

Negative-spec:
    - Does NOT repoint any of the three trio members at this module —
      ADDITIVE ONLY in this chunk. The trio's own private
      `_load_cli_module`/`_invoke_cli_main` are untouched.
    - Does NOT spawn a subprocess anywhere — every load and invocation is
      in-process, exactly like the three copies it is lifted from.
    - Does NOT read or mutate process cwd, call `os.chdir`, or otherwise
      establish a working-directory invariant for anything it loads or
      invokes — see "CWD IS A LOAD-BEARING GAP" above.
    - Does NOT isolate `os.environ`, an invoked script's own module-level
      side effects, or a non-`SystemExit` exception raised at import time or
      from `main()` — all of these propagate or apply exactly as if the
      call were un-isolated (see the enumeration above). `sys.path` is the
      one exception: `load_cli_module` inserts the loaded script's own
      directory for the duration of `exec_module` and removes it by value
      before returning (see "What this module does NOT isolate" above and
      `_exec_with_own_dir_on_path`) — everything else about `sys.path` is
      left exactly as ambient. One further durable, idempotent insert:
      `load_cli_module` calls `ensure_bin_lib_bound` unconditionally before
      building the spec, which binds `coordinator/bin` on `sys.path` when
      (and only when) `script_path` resolves under this engine's own bin —
      a no-op for every tmp-dir script `test_cli_dispatch.py` loads.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import inspect
import io
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

from coordinator_core.bin_lib_binding import ensure_bin_lib_bound
from coordinator_core.ceremony_common.cli_rejection import (
    CliExitClass,
    classify_cli_exit,
)
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root_in_process

#: on any real box -- see module docstring, "THE SENTINEL'S CONTRACT". Exists
UNRESOLVED_PLUGIN_CLI_ROOT = Path("/__coordinator_unresolved_plugin_cli_root__/coordinator/bin")

#: apply._BY_PATH_LOAD_LOCK`'s discipline, kept as a private module-level
_BY_PATH_LOAD_LOCK = threading.Lock()


def _exec_with_own_dir_on_path(loader: Any, module: ModuleType, script_dir: str) -> None:
    """Runs `loader.exec_module(module)` with `script_dir` on `sys.path[0]`,
    then restores `sys.path` to what it was.

    A script loaded BY PATH (as every caller of `load_cli_module` loads one)
    never gets the one thing a direct `python script.py` invocation gives it
    for free: its own directory as `sys.path[0]`. `exec_module` on a
    file-backed spec contributes nothing to `sys.path`, so a bare `import
    lib` at the top of a loaded consumes-manifest script instead binds
    whatever PEP-420 namespace package named `lib` sits earliest on the
    process's ambient `sys.path` -- the import succeeds, no error is raised,
    and the script proceeds against a partially-populated module.

    Removal is BY VALUE under `_BY_PATH_LOAD_LOCK`, never a positional pop --
    this dispatch primitive is used from a warm, multi-session engine process
    where an unrelated concurrent `sys.path` insert landing between this
    function's own insert and its restore would otherwise make an
    index-based pop remove someone else's entry and leak this one for the
    life of the process."""
    with _BY_PATH_LOAD_LOCK:
        added = script_dir not in sys.path
        if added:
            sys.path.insert(0, script_dir)
        try:
            loader.exec_module(module)
        finally:
            if added and script_dir in sys.path:
                sys.path.remove(script_dir)


#: Per-process cache of already-loaded CLI modules, keyed by the RESOLVED
#: ABSOLUTE `script_path` (`str(Path.resolve())`) — never by the caller-
_LOADED_MODULES: dict[str, ModuleType] = {}


def resolve_cli_script_root() -> Path:
    """The `coordinator/bin` directory holding every consumes-manifest
    script, resolved from THIS module's own location — the engine clone
    that ships both this module and those scripts.

    Takes no `repo_root` BY DESIGN (see module docstring): `coordinator/
    bin/` is engine-provisioned and absent from every consumer repo, so a
    caller's target-repo root is never a valid script root. Never
    `Path.cwd()` either (module docstring, "CWD IS A LOAD-BEARING GAP").

    NEGATIVE SPEC — DO NOT DELETE THIS FUNCTION AGAIN. It was deleted once,
    on 2026-09-01, and replaced with a gravestone arguing that this module
    "lives at a DIFFERENT depth than any of the three callers" and therefore
    could not compute a shared root at all. That premise is false, and it is
    checkable in one grep: this module, `merge_assemble/apply.py`,
    `merge_assemble/__init__.py`, and the `workday_complete`,
    `workstream_complete` and `workweek_complete` apply modules are each one
    directory under `coordinator_core/`, so `parents[2]` is the engine root
    for all six. The deletion bought nothing and left five copies of one
    expression, each free to drift back toward a repo-root join. The defect
    the gravestone described was real — `resolve_cli_script_root(repo_root)`
    joined `coordinator/bin` onto the TARGET repo and aborted `merge-assemble
    apply` at its first in-process directive in every consumer repo — but the
    correction is the no-argument signature above, which makes that call a
    TypeError rather than a runtime miss, not the removal of the seam."""
    return Path(__file__).resolve().parents[2] / "coordinator" / "bin"


def resolve_plugin_cli_script_root() -> Optional[Path]:
    """The SECOND, DoE-anchored `coordinator/bin` directory -- see module
    docstring, "THE TWO-ROOT MODEL" and "THE RUNG CUT". Zero parameters, for
    the same reason `resolve_cli_script_root()` takes none: a caller's
    `repo_root` names the repo a CLI operates on, never the tree it ships in.

    Resolves `<doe_root>/coordinator/bin` from
    `coordinator_doe_root_in_process()`, which covers rungs 1, 2, 2.5 and
    2.75 ONLY -- this function never calls `coordinator_doe_root()` and
    never reaches `resolve_coordinator_clone.resolve_clone_root()`'s
    `subprocess.run`. Returns `None`, never raises, when the ladder cannot
    resolve a root AND, equally, when it resolves a root whose joined
    `coordinator/bin` is not a directory (a stale/moved clone, or a
    flat-layout root admissible only up to rung 2.75 itself -- the join is
    deliberately not layout-aware, see module docstring)."""
    root, _rung = coordinator_doe_root_in_process()
    if root is None:
        return None
    candidate = Path(root) / "coordinator" / "bin"
    return candidate if candidate.is_dir() else None


def load_cli_module(module_name: str, script_path: Path) -> ModuleType:
    """Loads (once, cached under `script_path`'s resolved absolute path —
    see `_LOADED_MODULES`, never under `module_name`) the script at
    `script_path` via `importlib.util.spec_from_file_location`, in-process
    — never a subprocess. Uses an explicit `importlib.machinery.SourceFileLoader`
    (superset behaviour — see module docstring, Contested behaviour 3) so
    an extensionless bareword launcher shim loads exactly as a `.py`
    script would; this is a strict superset of `spec_from_file_location`'s
    own extension-sniffing, never a narrower path.

    Raises `ValueError` if no loadable spec can be built from
    `script_path`; propagates whatever `exec_module` itself raises
    (including a non-`SystemExit` exception from the script's own
    module-level code) uncaught — this module does not isolate import-time
    failures, see the module docstring's negative-spec.

    Registers the fresh module in `sys.modules` BEFORE calling
    `exec_module` (all three trio copies already carry this fix — see
    module docstring, Contested behaviour 4 — a dataclass's class-body
    execution resolves `sys.modules[cls.__module__]` during `exec_module`
    and crashes with an unrelated `AttributeError` if the module is not
    registered yet).

    `exec_module` runs via `_exec_with_own_dir_on_path`, which puts
    `script_path.parent` on `sys.path[0]` for the duration of the call and
    restores it after — every consumes-manifest script this function loads
    ships alongside its own `coordinator/bin/lib` package, and a bare
    `import lib` at that script's top level resolves correctly only when its
    own directory precedes whatever namespace package named `lib` the
    caller's ambient `sys.path` would otherwise bind first."""
    cache_key = str(script_path.resolve())
    if cache_key in _LOADED_MODULES:
        return _LOADED_MODULES[cache_key]
    ensure_bin_lib_bound(str(script_path.resolve().parent))
    loader = importlib.machinery.SourceFileLoader(module_name, str(script_path))
    spec = importlib.util.spec_from_file_location(module_name, script_path, loader=loader)
    if spec is None or spec.loader is None:
        raise ValueError(
            f"cli_dispatch.load_cli_module: could not load {module_name!r} from {script_path}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        _exec_with_own_dir_on_path(spec.loader, module, str(script_path.resolve().parent))
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    _LOADED_MODULES[cache_key] = module
    return module


def invoke_cli_main(
    module: ModuleType, args: list[str], *, stdin_text: Optional[str] = None
) -> tuple[int, str, str, CliExitClass]:
    """Invokes `module.main` in-process (never a subprocess) — accepts
    either an `argv`-taking `main(argv)` or a zero-arg `main()` (some
    consumes-manifest scripts across the trio expose the latter, calling
    `sys.exit` internally). Returns `(exit_code, stdout, stderr,
    exit_class)`: the resolved integer exit code (`main`'s own return value
    when it returns one, else the code a `SystemExit` it raises carries,
    else `0` on a clean fallthrough) paired with everything the call
    printed to stdout and, separately, to stderr (superset return shape —
    see module docstring, Contested behaviour 1). A `SystemExit` carrying a
    non-int, non-`None` code (e.g. `sys.exit("some message")`) collapses to
    exit code `1`; the message itself is not captured into stderr or
    otherwise surfaced.

    Zero-arg `main()` trampolines are `def main() -> None` wrappers whose
    body still calls `sys.exit(op_main(sys.argv[1:]))` — they parse
    `sys.argv` themselves rather than accepting a caller-supplied argv.
    Left alone, calling `main_fn()` bare hands them this PROCESS's own
    `sys.argv`, silently discarding `args`. This splices `args` into
    `sys.argv` (with a dummy `argv[0]` taken from `module.__name__`,
    restored in `finally`) for the duration of the call so a caller-
    supplied `args` reaches the op the same way it would for an
    `argv`-taking `main(argv)`.

    Stdin wiring (superset behaviour — see module docstring, Contested
    behaviour 2): when `stdin_text` is not `None`, `sys.stdin` is swapped
    for a fresh `io.StringIO(stdin_text)` for the duration of the call and
    restored in `finally`. When `stdin_text` IS `None` (the default),
    `sys.stdin` is left completely untouched — this function never opens
    or redirects a stdin stream for a caller that didn't ask for one.

    Stdout and stderr are each captured via `contextlib.redirect_stdout`/
    `contextlib.redirect_stderr` rather than left to print straight
    through, so a caller can thread either back into its own output or
    into another directive's input; this function does not re-emit either
    stream itself — that is the calling assembler's job (each trio
    member's own `_dispatch_directive` re-emits both onto its own
    stdout/stderr today; this module does not reproduce that re-emission).

    The fourth return value is `ceremony_common.cli_rejection.
    classify_cli_exit`'s verdict over this invocation: `CliExitClass.
    ARGV_REJECTED` when the callee raised `SystemExit(2)` with
    argparse-shaped stderr (the argv itself was rejected before any
    op-level code ran), else `CliExitClass.RETURNED` — including every
    zero-arg trampoline's own raised, semantic exit. This does not change
    `exit_code` itself or what a caller reports upward; it only names,
    for the caller, whether the exit code above actually means something
    the callee decided.

    Raises `ValueError` if `module` exposes no `main` attribute at all.
    Propagates any non-`SystemExit` exception `main()` itself raises
    uncaught — this function does not isolate a callee's own exceptions,
    see the module docstring's negative-spec."""
    main_fn: Optional[Callable[..., Any]] = getattr(module, "main", None)
    if main_fn is None:
        raise ValueError(
            f"cli_dispatch.invoke_cli_main: {module.__name__} exposes no main() entrypoint"
        )
    try:
        params = inspect.signature(main_fn).parameters
    except (TypeError, ValueError):
        params = {}

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    saved_stdin = sys.stdin
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    raised = False
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            try:
                if params:
                    result = main_fn(list(args))
                else:
                    saved_argv = sys.argv
                    sys.argv = [module.__name__, *args]
                    try:
                        result = main_fn()
                    finally:
                        sys.argv = saved_argv
            except SystemExit as exc:
                raised = True
                code = exc.code
                result = int(code) if isinstance(code, int) else (0 if code is None else 1)
    finally:
        sys.stdin = saved_stdin

    exit_code = int(result) if isinstance(result, int) else 0
    stderr_text = stderr_buf.getvalue()
    exit_class = classify_cli_exit(raised=raised, code=exit_code, stderr_text=stderr_text)
    return exit_code, stdout_buf.getvalue(), stderr_text, exit_class
