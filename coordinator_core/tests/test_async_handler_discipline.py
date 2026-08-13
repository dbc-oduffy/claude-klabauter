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

Widened again 2026-08-07 (post-mortem: `coordinator_core/ops/ceremony/scoped_git_commit.py`
`_handler` — fixed at commit 3241c7c95573, see that commit message for the full incident):
`_handler` was `async def` with ZERO `await` expressions anywhere in its body, and reached
its blocking call (`run_commit_pipeline()`, itself blocking on git subprocesses and a
cross-process mutex) through a plain function call one level of indirection away from the
handler body — invisible to a scan that only looks at direct `subprocess`/`os`/`shutil`
attribute calls at the handler's own top level. Because the coroutine never yielded,
`asyncio.wait_for`'s timeout (`ipc.dispatch_message`'s async branch) could never fire —
the server-side dispatch timeout was silently unenforceable for that op.

Two new checks close this:

  1. ZERO-AWAIT RULE (the floor — catches the specimen outright, no call-graph
     analysis needed): an `async def` handler with no `await` expression anywhere in
     its own body (counting `await`, `async with`, `async for` — anything that can
     actually yield control back to the event loop) has no reason to be `async`, full
     stop. Nested `def`/`async def` bodies are excluded from the count (an inner
     coroutine's own awaits do not make the OUTER function yield). Async GENERATORS
     (a body containing `yield`/`yield from`) are exempted — `async for x in
     an_async_gen(): yield x` with no bare `await` is a legitimate zero-await async
     generator, not a blocking-handler smell.

  2. ONE-LEVEL INDIRECTION for the existing direct-blocking-call scan: a Call inside
     an async function body whose callee resolves to a plain (non-async) top-level
     `def` in the SAME FILE is now followed one level — if THAT helper's own body
     (not recursing further) contains a direct subprocess/os/shutil blocking call, it
     is reported against the outer async handler, naming the helper. This catches the
     wider class described in the dispatch brief: a handler that awaits something
     elsewhere but reaches its blocking call through one hop of local indirection.

Spec backlink: pln-coordinator-core-global-multip-9ddcf7 § C3
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

# ---------------------------------------------------------------------------
# Zero-await pre-existing-violation allowlist (2026-08-07 widening)
# ---------------------------------------------------------------------------
#
# Generated by walking every `@register_op`-decorated `async def` in
# `coordinator_core/ops/` and `coordinator_core/hooks/` at the moment the
# zero-await rule (`_zero_await_violation`) was added, and recording every
# (rel_path, fn_name) that was ALREADY zero-await at that point — 95 hits.
# This is NOT a broad opt-out: it is an explicit, per-site enumeration (per
# the dispatch brief's own carve-out — "an explicit, commented per-site
# exemption is acceptable; a broad opt-out is not"), machine-generated once
# and then frozen, so any NEW zero-await `@register_op` handler (one not in
# this set) still fails the gate going forward.
#
# Investigated as a CLASS, not individually per-site (95 sites exceeds what a
# single dispatch can hand-audit — see this dispatch's own run-report sidecar
# for the full reasoning). NOTE: this allowlist ONLY suppresses the
# zero-await finding — it does NOT suppress the separate direct/
# one-level-indirect blocking-call scan, which still runs (and still fails)
# against every entry here. In fact a subset of these 95 (13 files, see
# `FIX-async-gate.md`) ALSO fail that independent scan — a real, un-suppressed
# reproduction of the incident's hazard shape, reported separately as findings
# rather than silenced by this allowlist. What the REMAINING majority share is
# a repo-wide convention of declaring every op handler `async def` regardless
# of whether it does anything blocking — real "no reason to be async" hygiene
# debt, not independently re-verified as hazard-free by this allowlist itself.
_ZERO_AWAIT_PRE_EXISTING_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()
# EMPTIED 2026-08-07. All 95 entries discharged rather than suppressed: 87 handlers
# were converted from `async def` to plain `def` (routing them through
# dispatch_message's sync branch, which makes their dispatch timeout actually
# enforceable), and the remaining 8 gained real `await asyncio.to_thread(...)`
# calls, so none of them trips the zero-await rule any more. Keep this frozenset
# EMPTY: a new entry here means someone is re-suppressing the exact defect that
# made DISPATCH_TIMEOUT_SECS unenforceable for ceremony.scoped_git_commit and
# wedged this repo's only sanctioned commit path (see 3241c7c95573).

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


# ---------------------------------------------------------------------------
# Zero-await rule (2026-08-07 widening — see module docstring)
# ---------------------------------------------------------------------------

def _contains_yield(fn: ast.AsyncFunctionDef) -> bool:
    """True if `fn`'s own body (not nested def/async-def) contains a `yield` /
    `yield from` — i.e. `fn` is an async GENERATOR, exempt from the zero-await
    rule (a zero-await async generator driven purely by `async for` delegation
    is a legitimate shape, not a blocking-handler smell)."""
    for stmt in ast.walk(fn):
        if stmt is fn:
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (ast.Yield, ast.YieldFrom)):
            return True
    return False


def _count_own_awaits(fn: ast.AsyncFunctionDef) -> int:
    """Count `await` expressions (including the implicit awaits of `async with`
    / `async for`) that appear directly in `fn`'s own body — NOT inside a nested
    def/async-def, whose own awaits belong to that inner coroutine, not to `fn`
    yielding control back to the event loop."""
    count = 0
    for stmt in ast.walk(fn):
        if stmt is fn:
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (ast.Await, ast.AsyncWith, ast.AsyncFor)):
            count += 1
    return count


def _is_register_op_decorated(fn: ast.AsyncFunctionDef) -> bool:
    """True iff `fn` carries a `@register_op(...)` (or bare `@register_op`)
    decorator — the actual risk surface: `ipc.dispatch_message`'s async branch
    wraps ONLY functions reachable through the op registry in
    `asyncio.wait_for`, so the unenforceable-timeout hazard this gate exists
    to catch is specific to registered op handlers, not every async def in the
    file. An internal async helper that is never registered is never wrapped
    in `wait_for` and carries none of this hazard — scoping the zero-await
    rule to registered handlers keeps it aimed at the real invariant instead
    of flagging every harmless "async def that happens to do nothing blocking"
    helper in the tree (verified against HEAD: an unscoped zero-await rule
    fires on 100+ pre-existing trivial handlers with no blocking call anywhere
    in their body, e.g. `ops/ping.py::_ping` — real "no reason to be async"
    hygiene findings, but not instances of the incident's hazard class, and
    at that volume not triageable as a bounded strengthening of this gate)."""
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id == "register_op":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "register_op":
            return True
    return False


def _zero_await_violation(fn: ast.AsyncFunctionDef) -> bool:
    """True iff `fn` is a `@register_op`-decorated `async def` with no reason
    to be async: zero `await` expressions anywhere in its own body, and it is
    not an async generator. Scoped to registered op handlers — see
    `_is_register_op_decorated` for why."""
    if not _is_register_op_decorated(fn):
        return False
    if _contains_yield(fn):
        return False
    return _count_own_awaits(fn) == 0


# ---------------------------------------------------------------------------
# One-level indirection: async handler -> local sync helper -> blocking call
# ---------------------------------------------------------------------------

def _module_level_sync_funcs(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Map name -> ast.FunctionDef for every top-level, plain (non-async) `def`
    in the module — the resolution set for one-level indirection following."""
    funcs: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            funcs[node.name] = node
    return funcs


def _direct_blocking_calls_in_body(fn: ast.AST) -> list[tuple[int, str]]:
    """Same scan as `_find_blocking_calls_in_async_fn`, generalized to any
    function-shaped node (used for the one-level-deep helper body scan)."""
    violations: list[tuple[int, str]] = []
    for stmt in ast.walk(fn):
        if stmt is fn:
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not isinstance(stmt, ast.Call):
            continue
        if _is_to_thread_call(stmt):
            continue
        is_violation, lineno, call_str = _is_direct_blocking_call(stmt)
        if is_violation:
            violations.append((lineno, call_str))
    return violations


def _find_indirect_blocking_calls_in_async_fn(
    fn: ast.AsyncFunctionDef, sync_funcs: dict[str, ast.FunctionDef]
) -> list[tuple[int, str]]:
    """Follow one level of indirection: for every bare-name Call directly in
    `fn`'s own body that resolves to a module-level plain `def` in the same
    file, scan THAT helper's body (not recursing further) for a direct
    blocking call. Reports at the call site inside `fn`, naming the helper.
    """
    violations: list[tuple[int, str]] = []
    for stmt in ast.walk(fn):
        if stmt is fn:
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not isinstance(stmt, ast.Call):
            continue
        func = stmt.func
        if not isinstance(func, ast.Name):
            continue
        helper = sync_funcs.get(func.id)
        if helper is None:
            continue
        helper_violations = _direct_blocking_calls_in_body(helper)
        for _hln, call_str in helper_violations:
            violations.append(
                (stmt.lineno, f"{func.id}(...) -> {call_str}")
            )
    return violations


def _find_violations_in_file(path: pathlib.Path) -> list[tuple[str, int, str]]:
    """Return (fn_name, lineno, call_str) for every violation in a source file."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []

    sync_funcs = _module_level_sync_funcs(tree)

    violations: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue

        # Scoped to @register_op-decorated handlers (mirrors _is_register_op_decorated's
        # existing zero-await scoping, same rationale): only a registered op handler is
        # ever reached through ipc.dispatch_message's asyncio.wait_for wrapping, so only
        # a registered handler's blocking call can actually make the dispatch timeout
        # unenforceable. An `async def` that is not `@register_op`-decorated -- a test
        # helper's inline mock coroutine (e.g.
        # ops/session/tests/test_boot_sweep_stamp_preservation.py's
        # `_act_then_unstage_dst`), or any other internal async helper -- is never
        # dispatched under that timeout and carries none of this hazard; flagging it is a
        # false positive of the exact kind this scoping already excludes for the
        # zero-await rule. Deliberately NOT a path-based test-file exclusion (that would
        # rot into a one-off suppression) -- this scopes the underlying rule to the real
        # risk surface everywhere, production or test.
        if not _is_register_op_decorated(node):
            continue

        fn_violations = _find_blocking_calls_in_async_fn(node)
        for lineno, call_str in fn_violations:
            violations.append((node.name, lineno, call_str))

        indirect_violations = _find_indirect_blocking_calls_in_async_fn(node, sync_funcs)
        for lineno, call_str in indirect_violations:
            violations.append((node.name, lineno, call_str))

        if _zero_await_violation(node):
            allowlist_key = (_rel(path).replace("\\", "/"), node.name)
            if allowlist_key not in _ZERO_AWAIT_PRE_EXISTING_ALLOWLIST:
                violations.append(
                    (node.name, node.lineno, "async def with zero `await` expressions")
                )
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


# ---------------------------------------------------------------------------
# Permanent regression test: pin the strengthened checker against the
# historical specimen (2026-08-07 widening — see module docstring).
#
# Falsifiability requirement (dispatch brief): a gate proven to pass on the
# FIXED code is not proven to have caught the BROKEN code. This test proves
# both halves against the real historical bodies, not paraphrases of them:
# the BROKEN snippet is a trimmed, semantically-preserved extract of
# `coordinator_core/ops/ceremony/scoped_git_commit.py::_handler` as it stood
# at `3241c7c95573^` (the parent of the fix commit) — `async def`, zero
# `await` expressions, calling the blocking `run_commit_pipeline(...)`
# straight from the handler body. The FIXED snippet mirrors that same
# specimen's post-fix shape (plain `def`, no `async`/`await` at all — the
# real fix, not a synthetic to_thread rewrite of the broken version).
# ---------------------------------------------------------------------------

_HISTORICAL_SPECIMEN_BROKEN = '''
import subprocess
import uuid
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline


@register_op("ceremony.scoped_git_commit")
async def _handler(params: dict, repo_root=None) -> dict:
    worktree_root = params.get("worktree_root")
    paths = params.get("paths")
    message = params.get("message")
    session_id = f"scoped-git-commit-{uuid.uuid4().hex}"
    result = run_commit_pipeline(
        worktree_root,
        session_id=session_id,
        subject=str(message),
        stage_paths=paths,
        caller_paths=set(paths),
    )
    return {"committed": result.committed_sha is not None, "sha": result.committed_sha}
'''

_HISTORICAL_SPECIMEN_FIXED = '''
import subprocess
import uuid
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline


@register_op("ceremony.scoped_git_commit")
def _handler(params: dict, repo_root=None) -> dict:
    worktree_root = params.get("worktree_root")
    paths = params.get("paths")
    message = params.get("message")
    session_id = f"scoped-git-commit-{uuid.uuid4().hex}"
    result = run_commit_pipeline(
        worktree_root,
        session_id=session_id,
        subject=str(message),
        stage_paths=paths,
        caller_paths=set(paths),
    )
    return {"committed": result.committed_sha is not None, "sha": result.committed_sha}
'''


def _violations_in_source(src: str) -> list[tuple[str, int, str]]:
    """Same violation-finding logic as `_find_violations_in_file`, over an
    in-memory source string rather than a file on disk (for pinning the
    checker against a synthetic/historical snippet without writing it into
    the repo tree)."""
    tree = ast.parse(src)
    sync_funcs = _module_level_sync_funcs(tree)
    violations: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for lineno, call_str in _find_blocking_calls_in_async_fn(node):
            violations.append((node.name, lineno, call_str))
        for lineno, call_str in _find_indirect_blocking_calls_in_async_fn(node, sync_funcs):
            violations.append((node.name, lineno, call_str))
        if _zero_await_violation(node):
            violations.append(
                (node.name, node.lineno, "async def with zero `await` expressions")
            )
    return violations


def test_checker_fails_on_historical_broken_specimen() -> None:
    """The strengthened gate MUST flag the pre-fix
    `scoped_git_commit.py::_handler` (as of `3241c7c95573^`) — `async def`
    with zero awaits, calling the blocking `run_commit_pipeline()` directly.
    This is the specimen the whole 2026-08-07 widening exists to catch; a
    gate that cannot fail on this input has not been shown to work."""
    violations = _violations_in_source(_HISTORICAL_SPECIMEN_BROKEN)
    assert violations, (
        "strengthened checker did not flag the historical broken specimen "
        "(scoped_git_commit.py::_handler pre-fix, commit 3241c7c95573^) — "
        "the gate has NOT been demonstrated to catch the incident it exists "
        "to prevent"
    )
    assert any(fn == "_handler" for fn, _ln, _call in violations)


def test_checker_passes_on_historical_fixed_specimen() -> None:
    """The strengthened gate MUST NOT flag the post-fix
    `scoped_git_commit.py::_handler` (a plain `def`, no `async`/`await`
    anywhere) — proves the checker does not over-fire on the actual
    remediation that shipped at commit `3241c7c95573`."""
    violations = _violations_in_source(_HISTORICAL_SPECIMEN_FIXED)
    assert not violations, (
        f"strengthened checker false-positived on the historical FIXED "
        f"specimen (scoped_git_commit.py::_handler post-fix): {violations}"
    )
