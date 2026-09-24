"""Every `coordinator/bin/*.py` importing a `lib/` sibling by bare name must
bootstrap `lib` first.

`coordinator/bin/lib/__init__.py` is the ONE place `coordinator/bin/lib` is put
on `sys.path` (see its docstring — 273 scattered preambles were collapsed into
it precisely so no module body mutates interpreter global state inside the warm
server ~50 sessions share). The cost of that centralisation is a prelude every
CLI has to remember:

    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import require_dispatch_engine_on_path

Omitting it is silent at author time and silent at import time. It raises
`ModuleNotFoundError: No module named 'cc_invoke'` only when the branch holding
the import actually executes — so a CLI whose bare-name import sits inside
`main()` past an argument check ships broken and stays broken until someone runs
that exact path.

That is not hypothetical. `write-identity-file.py` carried the defect in both
the authoring tree and the published mirror; it is invisible on any box that
already has `~/.claude/coordinator-identity.yaml`, because the values the step
would write are already there. It bites only a genuinely fresh install — the one
case nobody re-runs. Ten CLIs in this directory were missing the prelude when
this test was written.

A prelude repeated by hand across dozens of peers will be omitted again; this
test is the artifact that makes the omission loud instead of the operator
remembering. Static (AST) rather than import-based on purpose: importing each
module would not execute a function-scoped import, which is exactly where the
original defect lived.

Escape hatch: a module doing its own `sys.path` work is left alone — it has
taken the problem on explicitly, and this test does not adjudicate that choice.

Second check, same failure mode from the other side: no NON-test path-load
of a `coordinator/bin` script may skip `coordinator_core.bin_lib_binding.
ensure_bin_lib_bound`. `coordinator/bin/lib/__init__.py`'s bootstrap only
runs when a script is EXECUTED (its own directory lands on `sys.path[0]`
first). Something that loads a `coordinator/bin` script by file path with
`importlib.util.spec_from_file_location` — a hook, an engine loader, a
relocation forwarder — gets no such thing; the bare `import lib` inside the
loaded script then either raises, or, worse, silently binds an unrelated
`lib` (a pywin32 namespace package is the live case). Static (AST) for the
same reason as the check above: the defect is invisible until the exact
code path that loads the script by file path actually runs.

Scan set: `*.py` under `coordinator_core/`, repo-root `bin/`, `scripts/` and
`coordinator/lib/`, excluding any `tests`/`test` directory and any
`test_*.py`/`conftest.py` file — those are covered by their own collection
path, not by this static scan. A `spec_from_file_location` call is
"bin-targeting" when its enclosing function (or the module, for a
module-scope call) contains a string constant `"bin"`, text containing
`coordinator/bin`, or a `Name`/`Attribute` identifier containing `bin`. The
detector over-approximates on purpose; `_EXEMPT` absorbs the false
positives it catches, by name, with a reason.

A bin-targeting call is bound when a statement that calls
`ensure_bin_lib_bound` (an expression statement, an assignment, or an `if`
whose test contains the call) sits in the same statement list as an
ancestor-or-self statement of the call and precedes the point where that
spec is executed, or when the enclosing scope loads through
`exec_module_bin_bound`. A statement nested in an `if`/`else` body, a loop,
or an `except`/`finally` handler does not dominate. For a file under
repo-root `bin/` only, a dominating assignment to `sys.path[0]` also counts
(the six `distill-*.py` relocation forwarders' shape).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
LIB_DIR = BIN_DIR / "lib"
REPO_ROOT = BIN_DIR.parent.parent

PRELUDE = "import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path"


def _sibling_module_names() -> set[str]:
    """Bare names importable only because `lib/__init__` inserted its own dir."""
    return {p.stem for p in LIB_DIR.glob("*.py")} - {"__init__"}


def _bare_name_importers(source: str, siblings: set[str]) -> list[tuple[str, int]]:
    """`from <lib sibling> import ...` statements, as (module, lineno) pairs.

    Absolute imports only (`level == 0`): a relative import resolves through the
    package machinery and never needs the bare-name path entry.
    """
    tree = ast.parse(source)
    return [
        (node.module, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module in siblings
    ]


def _bootstraps_lib(source: str) -> bool:
    tree = ast.parse(source)
    return any(
        isinstance(node, ast.Import) and any(alias.name == "lib" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_every_bare_name_lib_importer_bootstraps_lib_first():
    siblings = _sibling_module_names()
    assert siblings, f"no lib siblings discovered under {LIB_DIR} — test is inert"

    offenders: list[str] = []
    for path in sorted(BIN_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            importers = _bare_name_importers(source, siblings)
        except SyntaxError:
            continue
        if not importers:
            continue
        if "sys.path" in source or _bootstraps_lib(source):
            continue
        module, lineno = importers[0]
        offenders.append(f"{path.name}:{lineno} imports '{module}' by bare name")

    assert not offenders, (
        "coordinator/bin CLI(s) import a lib/ sibling by bare name without "
        "bootstrapping lib first — each raises ModuleNotFoundError the moment "
        "that import executes:\n  "
        + "\n  ".join(offenders)
        + f"\n\nAdd this line above the bare-name import:\n    {PRELUDE}"
    )


def test_the_check_catches_a_planted_omission():
    """Proof the gate above can fail — a green suite must mean the tree is clean,
    never that the detector stopped detecting."""
    siblings = _sibling_module_names()
    sibling = sorted(siblings)[0]
    planted = f"def main():\n    from {sibling} import something\n"

    assert _bare_name_importers(planted, siblings)
    assert not _bootstraps_lib(planted)

    repaired = f"def main():\n    {PRELUDE}\n    from {sibling} import something\n"
    assert _bootstraps_lib(repaired)


# --- coordinator/bin script path-loads must bind through ensure_bin_lib_bound ---

_SCAN_ROOTS = (
    REPO_ROOT / "coordinator_core",
    REPO_ROOT / "bin",
    REPO_ROOT / "scripts",
    REPO_ROOT / "coordinator" / "lib",
)

#: (relpath-from-repo-root, function-name-or-"<module>") -> one-line reason.
#: Every reason names either "target carries no `import lib`" or "target is
#: not a coordinator/bin script", plus the target it is talking about.
_EXEMPT: dict[tuple[str, str], str] = {
    ("coordinator_core/bash_guards/check_test_suite_invocation.py", "_configured_test_cmds"): (
        "target carries no `import lib` (coordinator/bin/coordinator-resolve-validation-cmd.py)"
    ),
    ("coordinator_core/hooks/project_orientation.py", "_load_tier_last_run_module"): (
        "target carries no `import lib` (coordinator/bin/tier-last-run.py)"
    ),
    ("coordinator_core/install/fleet_env.py", "_load_c1_resolver"): (
        "target carries no `import lib` (coordinator/bin/fleet-env.py)"
    ),
    ("coordinator_core/ops/fleet_machinery_sweep.py", "_load_rename_with_retry"): (
        "target carries no `import lib` (coordinator/bin/publish.py)"
    ),
    ("coordinator_core/install/write_surface_discovery.py", "_load_via_file_location"): (
        "target is not a coordinator/bin script — a dynamically-discovered "
        "write-surface declaration module, resolved at runtime from whatever "
        "the discovery scan finds"
    ),
}


def _add_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def _is_named_call(node: ast.AST, name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == name
    if isinstance(func, ast.Name):
        return func.id == name
    return False


def _is_spec_from_file_location(node: ast.AST) -> bool:
    return _is_named_call(node, "spec_from_file_location")


def _is_ensure_bin_lib_bound(node: ast.AST) -> bool:
    return _is_named_call(node, "ensure_bin_lib_bound")


def _is_exec_module_bin_bound(node: ast.AST) -> bool:
    return _is_named_call(node, "exec_module_bin_bound")


def _enclosing_function(call_node: ast.AST) -> ast.AST | None:
    node = call_node
    while hasattr(node, "parent"):
        node = node.parent  # type: ignore[attr-defined]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _is_bin_targeting(scope_node: ast.AST) -> bool:
    for node in ast.walk(scope_node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "bin" or "coordinator/bin" in node.value:
                return True
        if isinstance(node, ast.Name) and "bin" in node.id:
            return True
        if isinstance(node, ast.Attribute) and "bin" in node.attr:
            return True
    return False


def _stmt_lists_containing(
    body: list[ast.stmt], target: ast.AST
) -> list[tuple[list[ast.stmt], int]] | None:
    """Path of (statement-list, index) pairs, outer to inner, for the
    statement subtree containing `target`. Each pair's statement is an
    ancestor-or-self statement of `target` within `body`."""

    def walk(stmts: list[ast.stmt], path: list[tuple[list[ast.stmt], int]]):
        for i, stmt in enumerate(stmts):
            if any(n is target for n in ast.walk(stmt)):
                new_path = path + [(stmts, i)]
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(stmt, field, None)
                    if sub:
                        deeper = walk(sub, new_path)
                        if deeper is not None:
                            return deeper
                for handler in getattr(stmt, "handlers", ()):
                    deeper = walk(handler.body, new_path)
                    if deeper is not None:
                        return deeper
                return new_path
        return None

    return walk(body, [])


def _is_syspath0_assign(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Assign):
        return False
    for target in stmt.targets:
        if not isinstance(target, ast.Subscript):
            continue
        value = target.value
        if not (isinstance(value, ast.Attribute) and value.attr == "path"):
            continue
        sl: Any = target.slice
        if isinstance(sl, ast.Index):  # py<3.9 grammar
            sl = sl.value
        if isinstance(sl, ast.Constant) and sl.value == 0:
            return True
    return False


def _dominates(stmt: ast.stmt, allow_syspath0: bool) -> bool:
    if isinstance(stmt, ast.Expr) and _is_ensure_bin_lib_bound(stmt.value):
        return True
    if isinstance(stmt, ast.Assign):
        if any(_is_ensure_bin_lib_bound(n) for n in ast.walk(stmt.value)):
            return True
        if allow_syspath0 and _is_syspath0_assign(stmt):
            return True
    if isinstance(stmt, ast.If):
        if any(_is_ensure_bin_lib_bound(n) for n in ast.walk(stmt.test)):
            return True
    return False


def _is_scan_file(path: Path) -> bool:
    parts = path.relative_to(REPO_ROOT).parts
    if any(part in ("tests", "test") for part in parts):
        return False
    if path.name.startswith("test_") or path.name == "conftest.py":
        return False
    return True


def _find_unbound_bin_targeting_calls(source: str, is_repo_root_bin: bool) -> list[int]:
    """Line numbers of bin-targeting `spec_from_file_location` calls in
    `source` that no in-scope statement binds."""
    tree = ast.parse(source)
    _add_parents(tree)
    offending: list[int] = []
    for node in ast.walk(tree):
        if not _is_spec_from_file_location(node):
            continue
        scope = _enclosing_function(node)
        scope_node: ast.AST = scope if scope is not None else tree
        if not _is_bin_targeting(scope_node):
            continue
        body = scope.body if scope is not None else tree.body  # type: ignore[union-attr]
        path = _stmt_lists_containing(body, node)
        bound = False
        if path is not None:
            for level, (stmts, idx) in enumerate(path):
                # The innermost list is scanned in full: a dominating bind may
                # sit between `spec_from_file_location` and the statement that
                # actually executes the loaded spec (the distill forwarders'
                # `sys.path[0] = ...`, or an ensure call ahead of a wrapper
                # exec call) without failing to dominate that execution.
                scan = stmts if level == len(path) - 1 else stmts[:idx]
                if any(_dominates(s, is_repo_root_bin) for s in scan):
                    bound = True
                    break
        if not bound and any(_is_exec_module_bin_bound(n) for n in ast.walk(scope_node)):
            bound = True
        if not bound:
            offending.append(node.lineno)
    return offending


def test_no_bin_script_path_load_skips_the_bind():
    stale = set(_EXEMPT)
    offenders: list[str] = []

    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if not _is_scan_file(path):
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if "spec_from_file_location" not in source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            _add_parents(tree)
            relpath = str(path.relative_to(REPO_ROOT))
            is_repo_root_bin = path.parent == REPO_ROOT / "bin"
            for node in ast.walk(tree):
                if not _is_spec_from_file_location(node):
                    continue
                scope = _enclosing_function(node)
                scope_node: ast.AST = scope if scope is not None else tree
                if not _is_bin_targeting(scope_node):
                    continue
                fname = scope.name if scope is not None else "<module>"  # type: ignore[union-attr]
                key = (relpath, fname)
                body = scope.body if scope is not None else tree.body  # type: ignore[union-attr]
                call_path = _stmt_lists_containing(body, node)
                bound = False
                if call_path is not None:
                    for level, (stmts, idx) in enumerate(call_path):
                        scan = stmts if level == len(call_path) - 1 else stmts[:idx]
                        if any(_dominates(s, is_repo_root_bin) for s in scan):
                            bound = True
                            break
                if not bound and any(
                    _is_exec_module_bin_bound(n) for n in ast.walk(scope_node)
                ):
                    bound = True
                if bound:
                    if key in stale:
                        stale.discard(key)
                        offenders.append(
                            f"{relpath}:{node.lineno} in {fname} — _EXEMPT entry {key} "
                            "names a site that is now bound; remove the entry"
                        )
                    continue
                if key in _EXEMPT:
                    stale.discard(key)
                    continue
                offenders.append(
                    f"{relpath}:{node.lineno} in {fname} loads a coordinator/bin script "
                    "without a dominating bind"
                )

    for key in stale:
        offenders.append(f"_EXEMPT entry {key} names a site that no longer exists")

    assert not offenders, (
        "a non-test path-load of a coordinator/bin script skips "
        "ensure_bin_lib_bound — call coordinator_core.bin_lib_binding."
        "ensure_bin_lib_bound unconditionally before spec_from_file_location, "
        "load through exec_module_bin_bound, or add an _EXEMPT entry naming "
        "why the target needs no lib:\n  " + "\n  ".join(offenders)
    )


def test_the_bin_load_check_catches_a_planted_omission():
    """Proof the check above can fail two different ways — an outright
    missing bind, and a bind that exists but does not dominate (planted
    under an `if` ahead of the spec call)."""
    missing = (
        "import importlib.util\n"
        "def _load(bin_dir):\n"
        "    spec = importlib.util.spec_from_file_location('x', bin_dir / 'x.py')\n"
        "    return spec\n"
    )
    assert _find_unbound_bin_targeting_calls(missing, is_repo_root_bin=False) == [3]

    conditional = (
        "import importlib.util\n"
        "def _load(bin_dir, flag):\n"
        "    if flag:\n"
        "        ensure_bin_lib_bound(str(bin_dir))\n"
        "    spec = importlib.util.spec_from_file_location('x', bin_dir / 'x.py')\n"
        "    return spec\n"
    )
    assert _find_unbound_bin_targeting_calls(conditional, is_repo_root_bin=False) == [5]

    repaired = (
        "import importlib.util\n"
        "def _load(bin_dir):\n"
        "    ensure_bin_lib_bound(str(bin_dir))\n"
        "    spec = importlib.util.spec_from_file_location('x', bin_dir / 'x.py')\n"
        "    return spec\n"
    )
    assert _find_unbound_bin_targeting_calls(repaired, is_repo_root_bin=False) == []
