"""CI grep gate: assert no blocking subprocess.run / os / shutil filesystem calls appear
directly inside async def handler functions in coordinator_core/ops/ and
coordinator_core/hooks/.

AC-3 Gap-3 rationale (mcp-async-handler-discipline):
    Blocking I/O inside an `async def` handler stalls the asyncio event loop — no other
    partition can be served until the blocking call returns.  asyncio.wait_for cannot
    interrupt a blocked event loop (the timer never fires).

    Permitted pattern (correct):
        async def _handler(...):
            result = await asyncio.to_thread(blocking_fn, arg1, arg2)

    Forbidden pattern (detected by this gate):
        async def _handler(...):
            result = subprocess.run(...)   # BLOCKS the event loop
            os.rename(src, dest)           # also BLOCKS the event loop
            shutil.copytree(src, dst)      # also BLOCKS the event loop

This gate walks every .py file in coordinator_core/ops/ and coordinator_core/hooks/,
parses the AST, finds every AsyncFunctionDef, and asserts that no direct blocking call
appears in the top-level body of the async function without being wrapped in an
asyncio.to_thread() call at the same level. Two call families are checked:

  - subprocess.run / subprocess.Popen / subprocess.call / subprocess.check_call /
    subprocess.check_output
  - Blocking filesystem calls: os.rename, os.walk, and any shutil.* call (shutil is a
    dedicated blocking-filesystem module — every public function in it does synchronous
    I/O, so no allowlist of individual shutil attrs is maintained; any shutil.<attr>(...)
    call is a violation).

Widened 2026-07-22 (code-review finding, AC-3 Gap-3): the gate originally checked only
subprocess.* calls and missed `research.archive_workdir` /
`research.restructure_for_repeat_topic`'s direct os.rename/os.walk/shutil.copytree/
shutil.rmtree calls inside `async def _handler` bodies — a live blind spot in the same
invariant this gate exists to enforce. See
test_async_handler_discipline_planted_violation.py for a planted-violation self-test
proving the widened detection actually fires.

Implementation note — "direct" means the Call node appears in the function body scope,
NOT inside a lambda or nested def/async-def (those do not block the outer event loop).
The gate checks one level of nesting: a blocking call directly inside an
asyncio.to_thread() argument IS acceptable (to_thread runs it in a thread, so the loop is
not blocked).

Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C3
"""
from __future__ import annotations

import ast
import pathlib
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Production directories to audit
# ---------------------------------------------------------------------------
_HANDLER_DIRS: list[pathlib.Path] = [
    pathlib.Path("coordinator_core/ops"),
    pathlib.Path("coordinator_core/hooks"),
]

# subprocess call names that must NOT appear directly in async def bodies
_BLOCKING_SUBPROCESS_NAMES: frozenset[str] = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
})

# os.* call names that are blocking filesystem I/O and must NOT appear directly
# in async def bodies. Narrow allowlist (not "any os.* call") because os also
# hosts non-blocking/negligible calls (os.environ access, os.getcwd, os.path
# helpers via os.path.* which is a separate module object) that would produce
# false positives if os.* were treated as uniformly blocking.
_BLOCKING_OS_NAMES: frozenset[str] = frozenset({"rename", "walk"})


def _repo_root() -> pathlib.Path:
    """Return the repository root (two levels up from this test file)."""
    return pathlib.Path(__file__).resolve().parent.parent.parent


def _handler_files() -> list[pathlib.Path]:
    """Enumerate all .py files in the handler directories (recursive, absolute paths)."""
    root = _repo_root()
    files: list[pathlib.Path] = []
    for rel_dir in _HANDLER_DIRS:
        abs_dir = root / rel_dir
        if abs_dir.is_dir():
            files.extend(sorted(abs_dir.rglob("*.py")))
    return files


# ---------------------------------------------------------------------------
# AST analysis helpers
# ---------------------------------------------------------------------------

def _is_to_thread_call(node: ast.expr) -> bool:
    """Return True if node is an asyncio.to_thread(...) call expression."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # Match: asyncio.to_thread(...)
    if isinstance(func, ast.Attribute) and func.attr == "to_thread":
        if isinstance(func.value, ast.Name) and func.value.id == "asyncio":
            return True
    # Match: to_thread(...) — if imported directly (less common, still safe)
    if isinstance(func, ast.Name) and func.attr if hasattr(func, "attr") else False:
        return True
    return False


def _is_direct_blocking_call(node: ast.AST) -> tuple[bool, int, str]:
    """Return (is_violation, lineno, call_str) for a direct blocking-I/O call node.

    A "direct" blocking call is one that appears as a statement or value expression
    inside the async function body, NOT inside an asyncio.to_thread() argument.
    The caller is responsible for not passing nodes that are arguments to to_thread.

    Three families are checked: subprocess.{run,Popen,call,check_call,check_output},
    os.{rename,walk}, and any shutil.<attr>(...) call (shutil is a dedicated
    blocking-filesystem module; no per-attr allowlist is maintained for it).
    """
    if not isinstance(node, ast.Call):
        return False, 0, ""
    func = node.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return False, 0, ""
    module_name = func.value.id
    if module_name == "subprocess" and func.attr in _BLOCKING_SUBPROCESS_NAMES:
        return True, node.lineno, f"subprocess.{func.attr}(...)"
    if module_name == "os" and func.attr in _BLOCKING_OS_NAMES:
        return True, node.lineno, f"os.{func.attr}(...)"
    if module_name == "shutil":
        return True, node.lineno, f"shutil.{func.attr}(...)"
    return False, 0, ""


def _find_blocking_calls_in_async_fn(fn: ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """Find direct blocking subprocess calls in the body of an async function.

    Walks the body of `fn` one level deep.  When an asyncio.to_thread() call is
    encountered, its arguments are NOT recursed into (they run in a thread, not on
    the event loop — that is the correct pattern).

    Nested async def / def inside the handler body are also skipped (they do not
    block the outer event loop when called).

    Returns a list of (lineno, call_str) for each violation found.
    """
    violations: list[tuple[int, str]] = []

    for stmt in ast.walk(fn):
        # Skip the outer fn itself (avoid double-visiting)
        if stmt is fn:
            continue

        # Don't recurse into nested function/class definitions
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        if not isinstance(stmt, ast.Call):
            continue

        call = stmt

        # If this call is asyncio.to_thread(...), skip its arguments — they are
        # running in a thread, so blocking I/O there is intentional and correct.
        if _is_to_thread_call(call):
            continue

        is_violation, lineno, call_str = _is_direct_blocking_call(call)
        if is_violation:
            violations.append((lineno, call_str))

    return violations


def _find_violations_in_file(path: pathlib.Path) -> list[tuple[str, int, str]]:
    """Return (fn_name, lineno, call_str) for every violation in a source file."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []

    violations: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        fn_violations = _find_blocking_calls_in_async_fn(node)
        for lineno, call_str in fn_violations:
            violations.append((node.name, lineno, call_str))
    return violations


# ---------------------------------------------------------------------------
# Parametrized test — one file at a time (better failure granularity)
# ---------------------------------------------------------------------------

def _rel(path: pathlib.Path) -> str:
    """Return path relative to repo root for display."""
    try:
        return str(path.relative_to(_repo_root()))
    except ValueError:
        return str(path)


@pytest.mark.parametrize(
    "path",
    _handler_files(),
    ids=lambda p: _rel(p),
)
def test_no_direct_blocking_subprocess_in_async_handlers(path: pathlib.Path) -> None:
    """AC-3 Gap-3: no direct subprocess/os/shutil blocking I/O in async def handler bodies.

    Every async def handler in coordinator_core/ops/ and coordinator_core/hooks/ that
    needs to call blocking subprocess or filesystem I/O MUST wrap it in
    asyncio.to_thread(...). A direct subprocess.run() / os.rename() / shutil.copytree()
    inside an async def stalls the event loop for all other partitions — the per-request
    timeout (DISPATCH_TIMEOUT_SECS) cannot fire while the loop is blocked.

    Correct pattern:
        async def _handler(params, ctx=None, repo_root=None):
            result = await asyncio.to_thread(subprocess.run, cmd, cwd=cwd, ...)

    Violation pattern (caught by this gate):
        async def _handler(params, ctx=None, repo_root=None):
            result = subprocess.run(cmd, cwd=cwd, ...)   # stalls the event loop
            os.rename(src, dest)                         # stalls the event loop
            shutil.copytree(src, dst)                     # stalls the event loop
    """
    violations = _find_violations_in_file(path)
    assert not violations, (
        f"AC-3 async-handler-discipline violation in {_rel(path)}:\n"
        + "\n".join(
            f"  async def {fn}() line {ln}: {call} — wrap in asyncio.to_thread()"
            for fn, ln, call in violations
        )
        + "\n\nBlocking I/O inside async def stalls the event loop. "
        "Use: result = await asyncio.to_thread(blocking_fn, *args, **kwargs)"
    )
