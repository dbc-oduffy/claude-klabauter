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

Repo root, not `Path(__file__)` or process cwd (staff-eng finding,
accepted, critical): each trio member computes its own script root as
`Path(__file__).resolve().parents[2] / "coordinator" / "bin"` — safe there
because each `apply.py` lives at a fixed depth under the repo root. This
module lives in `ceremony_common/`, at a DIFFERENT depth than any of the
three callers, so the same `Path(__file__)` computation would resolve to
the wrong directory here. `resolve_cli_script_root` therefore takes
`repo_root` as an explicit parameter and computes the join from it —
never from this module's own file location, never from `Path.cwd()`.

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
mutated); `sys.path` (an invoked script's own top-level imports run exactly
as they would un-isolated); `os.environ` (read/written by the invoked
script exactly as ambient); any module-level side effect the invoked
script's own top-level code performs at `exec_module` time; and any
non-`SystemExit` exception raised either at import time (`exec_module`) or
from `main()` itself — both propagate straight to this module's caller,
uncaught.

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
    - Does NOT isolate `sys.path`, `os.environ`, an invoked script's own
      module-level side effects, or a non-`SystemExit` exception raised at
      import time or from `main()` — all of these propagate or apply
      exactly as if the call were un-isolated (see the enumeration above).
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import inspect
import io
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

from coordinator_core.ceremony_common.cli_rejection import (
    CliExitClass,
    classify_cli_exit,
)

#: Per-process cache of already-loaded CLI modules, keyed by the caller-
#: chosen `module_name` (each trio member already namespaces this per-CLI,
#: e.g. `_workday_complete_cli_<name>` — this module does not impose its
#: own naming scheme, it only caches whatever key the caller passes).
_LOADED_MODULES: dict[str, ModuleType] = {}


def resolve_cli_script_root(repo_root: Path) -> Path:
    """The `coordinator/bin` directory holding every consumes-manifest
    script, joined from an explicit `repo_root` — never from `Path(
    __file__)` (this module's own location does not sit at the same depth
    under the repo root as any of the three trio callers) and never from
    `Path.cwd()` (see module docstring, "CWD IS A LOAD-BEARING GAP")."""
    return repo_root / "coordinator" / "bin"


def load_cli_module(module_name: str, script_path: Path) -> ModuleType:
    """Loads (once, cached under `module_name`) the script at `script_path`
    via `importlib.util.spec_from_file_location`, in-process — never a
    subprocess. Uses an explicit `importlib.machinery.SourceFileLoader`
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
    registered yet)."""
    if module_name in _LOADED_MODULES:
        return _LOADED_MODULES[module_name]
    loader = importlib.machinery.SourceFileLoader(module_name, str(script_path))
    spec = importlib.util.spec_from_file_location(module_name, script_path, loader=loader)
    if spec is None or spec.loader is None:
        raise ValueError(
            f"cli_dispatch.load_cli_module: could not load {module_name!r} from {script_path}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    _LOADED_MODULES[module_name] = module
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
    see module docstring, Contested behaviour 1).

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
