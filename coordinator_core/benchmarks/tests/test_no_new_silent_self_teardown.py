"""Standing gate: no NEW self-teardown that hides its own failure.

THE DISCRIMINATOR, WHICH IS THIS FILE'S REAL CONTENT. `shutil.rmtree(...,
ignore_errors=True)` appears 58 times in this engine and is correct in most of
them -- removing something another owner created, or a path pytest reaps
anyway. Flagging all 58 would be a gate that misfires, and a gate that
misfires gets disabled.

The subset that is not correct is SELF-TEARDOWN: a function MINTS a directory
(`mkdtemp` / `mkdtemp_for_clone`) and then removes it with `ignore_errors=True`
in the same body. There, and only there, three things are true at once:

  1. nothing else in the system will ever remove that directory -- the minting
     function is its sole owner, so a failed removal is a permanent leak;
  2. `ignore_errors=True` discards the `PermissionError` that a live process
     holding the tree raises on Windows, which is the common failure, not an
     exotic one;
  3. the caller therefore reads a clean teardown from a run that leaked.

That is exactly the 2026-08-27 incident: a benchmark fixture minted a clone,
its detached http-listener supervisor outlived the teardown and held the tree,
`rmtree(ignore_errors=True)` swallowed the error, and every leaking run
reported success. 68 directories and ~14GB accumulated before a human noticed
by reading a drive listing. Measured while writing this gate: 20 of the 58
sites are self-teardown, so the discriminator removes two thirds of the
population without losing the defect it exists for.

WHAT TO DO INSTEAD. `benchmarks.isolated_clone.rmtree_or_raise` removes the
tree and WARNS -- naming the path, the processes still rooted there, and any
pids already reaped -- when something survives. Pair it with
`reap_processes_under` when a spawned process may still hold the tree.

FROZEN INVENTORY, NOT A BURN-DOWN LIST. The pre-existing sites below are
recorded, not scheduled: most are small scratch dirs with no live process to
hold them, they are spread across several owners' files, and converting them
would be churn this incident does not justify. The gate's job is that the
NEXT one does not appear silently. Removing an entry after converting a site
is welcome; adding one requires saying why the failure is not worth seeing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, NamedTuple

_ENGINE_PKG = Path(__file__).resolve().parents[2]

# Calls that MINT a directory the calling function then owns outright.
_MINTS = frozenset({"mkdtemp", "mkdtemp_for_clone"})


class _Site(NamedTuple):
    relpath: str
    func: str
    lineno: int


# (relpath, function) pairs predating this gate. See FROZEN INVENTORY above.
_KNOWN_SELF_TEARDOWN: frozenset[tuple[str, str]] = frozenset({
    ("coordinator_core/bash_guards/_alternative_liveness.py", "_scratch_git_repo"),
    ("coordinator_core/bash_guards/_alternative_liveness.py", "_trigger_guard_branch_set_precedence"),
    ("coordinator_core/bash_guards/_alternative_liveness.py", "probe_command"),
    ("coordinator_core/bash_guards/_firing_shape.py", "_trigger_inprocess_search_still_violates"),
    ("coordinator_core/bash_guards/_guard_coverage.py", "measure_probe_spray"),
    ("coordinator_core/benchmarks/measure_read_events.py", "measure"),
    ("coordinator_core/benchmarks/measure_render_status.py", "measure"),
    ("coordinator_core/benchmarks/tests/test_warm_door_process_time_gate.py", "_short_runtime_base"),
    ("coordinator_core/install/sandbox_check.py", "run_all"),
    ("coordinator_core/ops/new_project_scaffold.py", "main"),
    ("coordinator_core/ops/tests/test_invoke_from_argv.py",
     "test_worktree_scoped_op_resolves_repo_root_from_cwd_param_not_process_cwd"),
    ("coordinator_core/percolate/engine.py", "mktcache_gate_env"),
    ("coordinator_core/tests/_fixtures.py", "isolated_svc_root_impl"),
    ("coordinator_core/tests/test_invoke_main.py", "test_none_scoped_outside_git_tree"),
    ("coordinator_core/warm/tests/test_door_read_deadline_posix.py", "runtime_base"),
})


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _names_minted_in(fn: ast.AST) -> set[str]:
    """Locals bound to the result of a mint call, unwrapping the `Path(...)` /
    `os.path.realpath(...)` wrappers these call sites conventionally use."""
    minted: set[str] = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        for _ in range(3):
            if not (call.args and isinstance(call.args[0], ast.Call)):
                break
            inner = call.args[0]
            call = inner
            if _callee(inner) in _MINTS:
                break
        if _callee(call) in _MINTS:
            minted.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return minted


def _silent_rmtree_of(call: ast.Call, minted: set[str]) -> bool:
    if _callee(call) != "rmtree" or not call.args:
        return False
    if not any(
        kw.arg == "ignore_errors" and getattr(kw.value, "value", None) is True
        for kw in call.keywords
    ):
        return False
    root = call.args[0]
    while True:
        if isinstance(root, ast.Attribute):
            root = root.value
            continue
        # A wrapper call around the mint target, e.g. `rmtree(str(tmp_parent))`
        # -- unwrap its single positional argument the same way the mint side
        # already unwraps `Path(...)` / `os.path.realpath(...)`. Without this,
        # `str()` (or any other single-arg wrapper) defeats the gate whose
        # whole job is catching the next self-teardown site.
        if isinstance(root, ast.Call) and len(root.args) == 1 and not root.keywords:
            root = root.args[0]
            continue
        break
    return isinstance(root, ast.Name) and root.id in minted


def _iter_self_teardown_sites() -> Iterator[_Site]:
    for path in sorted(_ENGINE_PKG.rglob("*.py")):
        relpath = path.relative_to(_ENGINE_PKG.parent).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            minted = _names_minted_in(fn)
            if not minted:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _silent_rmtree_of(node, minted):
                    yield _Site(relpath, fn.name, node.lineno)


def test_no_new_silent_self_teardown() -> None:
    """A function that mints a directory must not then hide its removal failing."""
    new = [
        site
        for site in _iter_self_teardown_sites()
        if (site.relpath, site.func) not in _KNOWN_SELF_TEARDOWN
    ]
    assert not new, (
        "new self-teardown site(s) hiding their own removal failure:\n"
        + "\n".join(f"  {s.relpath}:{s.lineno}  in {s.func}()" for s in new)
        + "\n\nThis function minted the directory, so nothing else will ever "
        "remove it -- an `ignore_errors=True` here reports a clean teardown for "
        "a run that leaked (2026-08-27: 68 directories, ~14GB). Use "
        "`benchmarks.isolated_clone.rmtree_or_raise`, plus `reap_processes_under` "
        "if a spawned process may still hold the tree."
    )


def test_frozen_inventory_has_no_dead_entries() -> None:
    """An inventory entry matching nothing on disk is a licence the next
    function of that name inherits silently -- convert a site, drop its entry."""
    live = {(s.relpath, s.func) for s in _iter_self_teardown_sites()}
    dead = sorted(_KNOWN_SELF_TEARDOWN - live)
    assert not dead, (
        "frozen-inventory entries matching nothing on disk (remove them):\n"
        + "\n".join(f"  {relpath}  {func}()" for relpath, func in dead)
    )
