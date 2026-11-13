"""Collector pinning that no claude-klabauter-owned caller spawns `_machine_local.py` without a bound.

A hit is a spawn call (`subprocess.run`/`Popen`/`check_output`/`check_call`/`getoutput`) whose argv
names the machine-local impl or CLI immediately followed by one of its subcommands, and that has no
kill ceiling: no `timeout=` (for `Popen`, no `communicate`/`wait` with one in the same scope), a
`timeout=` that resolves to a number above `ML_SPAWN_TIMEOUT_S`, or `timeout=None`. A second hit
class is a `TimeoutExpired` handler in a scope that spawns machine-local whose body re-spawns or
`continue`s a retry loop.

Shape follows `test_no_machine_local_cli_read_spawn.py` (AST walk, same-scope binding substitution,
Starred handling) and widens it to every subcommand. It differs in two ways that matter: a name
assigned more than once in a scope is followed through every assignment (the `if impl.endswith(".py")
... else ...` shape of the known P0 caller was invisible to the single-assignment rule), and test
files are not scanned (they stub the CLI, they do not storm the box).

The inventory is a site-key set (`path::enclosing.scope`, never line numbers). While the conversions
land it is a two-sided ratchet: live hits must equal it, so a new unbounded site reds this test and a
converted site left listed does too. Target: the empty set.

Out of reach by construction: a spawn whose argv is built by a helper call
(`subprocess.run(_argv("get", key))`) names no literal subcommand in the spawn expression.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files

pytestmark = [pytest.mark.cadence]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

MAX_TIMEOUT_S = 2.0
_TIMEOUT_CONSTANT_NAME = "ML_SPAWN_TIMEOUT_S"

_SCAN_ROOTS = ("coordinator_core", "coordinator", "bin", "scripts")

_SPAWN_FUNCS = {"run", "Popen", "check_output", "check_call", "getoutput"}

_BINARY_MARKERS = (
    "machine-local",
    "machine_local",
    "ml_argv",
    "ml_bin",
    "ml_cli",
    "impl",
    "_resolve_machine_local",
)

_SUBCOMMANDS = frozenset(
    {
        "get", "require", "has", "external-workspace", "store", "relocate-store", "keys", "dump",
        "check", "path", "dir", "set", "unset", "retire-publisher", "array-append", "array-set",
        "migrate-publish-mirrors", "delete",
    }
)
_SUBCOMMAND_WORD = re.compile(r"\b(" + "|".join(sorted(_SUBCOMMANDS, key=len, reverse=True)) + r")\b")

# Sites no conversion row owned. Target is the empty set; the ratchet only shrinks (a converted
# site left here fails `test_inventory_has_no_stale_entries`).
KNOWN_UNBOUNDED_SITES: frozenset[str] = frozenset(
    {
        "coordinator/bin/fan-out-dispatch.py::_machine_local_get",
        "coordinator/bin/lib/unimatrix_box_mux.py::machine_local_value",
        "coordinator/bin/resolve-repo-path.py::_resolve_registry_value",
        "coordinator/bin/unimatrix-install.py::_write_pin",
        "coordinator/bin/unimatrix-install.py::pair_key",
        "coordinator/bin/unimatrix-install.py::recorded_pair_keys",
        "coordinator/bin/unimatrix-passphrase.py::_vault",
        "coordinator/bin/unimatrix-relay.py::_pin_value",
        "coordinator/bin/workday-start-health-probes.py::cmd_working_repo_registration",
        "coordinator/bin/workday-start-step0.py::_slug_self_heal",
        "coordinator/bin/workday-start-step0.py::_slug_self_heal#2",
        "coordinator/bin/workday-start-step0.py::_slug_self_heal#3",
        "coordinator/lib/install/coordinator_install.py::_machine_local_set",
        "coordinator_core/benchmarks/percolate_round_ab.py::_real_machine_local_dump",
        "coordinator_core/install/_shared.py::ml_set",
        "coordinator_core/ops/capture_fan_out_threshold.py::capture",
        "coordinator_core/ops/detect_hardware.py::_ml_set",
        "coordinator_core/ops/new_project_scaffold.py::_register_repo",
        "scripts/setup.py::install_machine_identity",
        "scripts/setup.py::install_machine_identity~retry",
        "scripts/setup.py::offer_warm_opt_in",
        "scripts/setup.py::register_claude_klabauter_root",
    }
)

Hit = tuple[str, int, str]


def _is_test_path(rel_posix: str) -> bool:
    parts = rel_posix.split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _names_marker(text: str) -> bool:
    return any(marker in text for marker in _BINARY_MARKERS)


def _spawn_func_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _SPAWN_FUNCS:
        return func.attr
    if isinstance(func, ast.Name) and func.id in _SPAWN_FUNCS:
        return func.id
    return None


class _Scope:
    """Every same-scope assignment per bare name; control flow is flattened, nested defs are skipped."""

    def __init__(self, stmts: list[ast.stmt], *, in_machine_local_module: bool = False) -> None:
        self.in_machine_local_module = in_machine_local_module
        self.assigns: dict[str, list[ast.expr]] = {}
        self._collect(stmts)

    def _bind(self, name: str, value: ast.expr) -> None:
        self.assigns.setdefault(name, []).append(value)

    def _collect(self, stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        self._bind(target.id, stmt.value)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value:
                self._bind(stmt.target.id, stmt.value)
            elif isinstance(stmt, ast.For) and isinstance(stmt.target, ast.Name):
                self._bind(stmt.target.id, stmt.iter)
            for field in ("body", "orelse", "finalbody"):
                child = getattr(stmt, field, None)
                if isinstance(child, list):
                    self._collect(child)
            if isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    self._collect(handler.body)


class _NodeCollector(ast.NodeVisitor):
    """Calls and Try nodes of one scope, never descending into a nested def/class/lambda."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []
        self.tries: list[ast.Try] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.tries.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _tainted(expr: ast.AST, scope: _Scope, module: _Scope, depth: int = 0) -> bool:
    """True when expr's text names a machine-local marker, directly or through a scope assignment."""
    if _names_marker(ast.unparse(expr)):
        return True
    if module.in_machine_local_module and isinstance(expr, (ast.Name, ast.Call, ast.Subscript)):
        return True
    if depth >= 3:
        return False
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            for table in (scope, module):
                for value in table.assigns.get(node.id, ()):
                    if value is not expr and _tainted(value, scope, module, depth + 1):
                        return True
    return False


def _elements(expr: ast.AST, scope: _Scope, module: _Scope, depth: int = 0) -> list[list[ast.expr]]:
    """Candidate flat element lists for an argv expression: list/tuple literals, `a + b`, bound names."""
    if isinstance(expr, (ast.List, ast.Tuple)):
        return [list(expr.elts)]
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return [
            left + right
            for left in _elements(expr.left, scope, module, depth) or [[expr.left]]
            for right in _elements(expr.right, scope, module, depth) or [[expr.right]]
        ]
    if isinstance(expr, ast.Name) and depth < 2:
        out: list[list[ast.expr]] = []
        for table in (scope, module):
            for value in table.assigns.get(expr.id, ()):
                out.extend(_elements(value, scope, module, depth + 1))
        return out
    return []


def _names_machine_local_subcommand(arg0: ast.expr, scope: _Scope, module: _Scope) -> bool:
    if isinstance(arg0, (ast.Constant, ast.JoinedStr)):
        text = ast.unparse(arg0)
        return _names_marker(text) and bool(_SUBCOMMAND_WORD.search(text))
    for elts in _elements(arg0, scope, module):
        for left, right in zip(elts, elts[1:]):
            literal_subcommand = (
                isinstance(right, ast.Constant) and isinstance(right.value, str) and right.value in _SUBCOMMANDS
            )
            passthrough = isinstance(right, ast.Starred)
            if (literal_subcommand or passthrough) and _tainted(left, scope, module):
                return True
    return False


def _number(expr: ast.expr, scope: _Scope, module: _Scope, depth: int = 0) -> float | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, (int, float)) and not isinstance(expr.value, bool):
        return float(expr.value)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Sub, ast.Mult)):
        left = _number(expr.left, scope, module, depth)
        right = _number(expr.right, scope, module, depth)
        if left is None or right is None:
            return None
        return left + right if isinstance(expr.op, ast.Add) else left - right if isinstance(expr.op, ast.Sub) else left * right
    if isinstance(expr, ast.Name) and depth < 2:
        values = [v for table in (scope, module) for v in table.assigns.get(expr.id, ())]
        numbers = [_number(v, scope, module, depth + 1) for v in values]
        known = [n for n in numbers if n is not None]
        if known and len(known) == len(values):
            return max(known)
    return None


def _timeout_is_bounded(expr: ast.expr, scope: _Scope, module: _Scope) -> bool:
    if isinstance(expr, ast.Constant) and expr.value is None:
        return False
    if isinstance(expr, ast.Name) and expr.id == _TIMEOUT_CONSTANT_NAME:
        return True
    if isinstance(expr, ast.Attribute) and expr.attr == _TIMEOUT_CONSTANT_NAME:
        return True
    number = _number(expr, scope, module)
    if number is not None:
        return number <= MAX_TIMEOUT_S
    return True


def _kw_timeout(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "timeout":
            return kw.value
    return None


def _scope_bounds_popen(calls: list[ast.Call], scope: _Scope, module: _Scope) -> bool:
    """A Popen is bounded when its scope waits on a child with a numeric ceiling."""
    for call in calls:
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr in ("communicate", "wait"):
            timeout = _kw_timeout(call)
            if timeout is None and func.attr == "wait" and call.args:
                timeout = call.args[0]
            if timeout is not None and _timeout_is_bounded(timeout, scope, module):
                return True
    return False


def _is_timeout_expired(handler: ast.ExceptHandler) -> bool:
    def names(node: ast.expr | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id == "TimeoutExpired"
        if isinstance(node, ast.Attribute):
            return node.attr == "TimeoutExpired"
        if isinstance(node, ast.Tuple):
            return any(names(e) for e in node.elts)
        return False

    return names(handler.type)


def _handler_retries(handler: ast.ExceptHandler) -> bool:
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Continue):
                return True
            if isinstance(node, ast.Call) and _spawn_func_name(node) is not None:
                return True
    return False


def _scan_scope(
    stmts: list[ast.stmt],
    scope: _Scope,
    module: _Scope,
    hits: list[Hit],
    rel_posix: str,
    name: str,
) -> None:
    collector = _NodeCollector()
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = stmt.name if name == "<module>" else f"{name}.{stmt.name}"
            _scan_scope(stmt.body, _Scope(stmt.body), module, hits, rel_posix, inner)
            continue
        if isinstance(stmt, ast.ClassDef):
            inner = stmt.name if name == "<module>" else f"{name}.{stmt.name}"
            _scan_scope(stmt.body, _Scope(stmt.body), module, hits, rel_posix, inner)
            continue
        collector.visit(stmt)

    ml_calls = []
    for call in collector.calls:
        fn = _spawn_func_name(call)
        if fn is None or not call.args:
            continue
        if _names_machine_local_subcommand(call.args[0], scope, module):
            ml_calls.append((fn, call))

    popen_bounded = _scope_bounds_popen(collector.calls, scope, module)
    for fn, call in ml_calls:
        if fn == "Popen":
            bounded = popen_bounded
        else:
            timeout = _kw_timeout(call)
            bounded = timeout is not None and _timeout_is_bounded(timeout, scope, module)
        if not bounded:
            hits.append((rel_posix, call.lineno, name))

    if ml_calls:
        for node in collector.tries:
            for handler in node.handlers:
                if _is_timeout_expired(handler) and _handler_retries(handler):
                    hits.append((rel_posix, handler.lineno, f"{name}~retry"))


def scan_file(rel_posix: str, text: str) -> list[Hit]:
    """Unbounded machine-local spawn sites in one source text as (path, lineno, scope)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits: list[Hit] = []
    module = _Scope(tree.body, in_machine_local_module=_names_marker(pathlib.PurePosixPath(rel_posix).name))
    _scan_scope(tree.body, module, module, hits, rel_posix, "<module>")
    return hits


def site_keys(hits: list[Hit]) -> set[str]:
    """`path::enclosing.scope` per hit, `#2`, `#3`... for further hits in one scope; never a line number."""
    seen: dict[str, int] = {}
    keys: set[str] = set()
    for path, _lineno, scope in sorted(hits):
        base = f"{path}::{scope}"
        seen[base] = seen.get(base, 0) + 1
        keys.add(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return keys


def _collect_live_hits() -> list[Hit]:
    hits: list[Hit] = []
    for root_name in _SCAN_ROOTS:
        root = _REPO_ROOT / root_name
        if not root.is_dir():
            continue
        discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for rel_posix, file_path in discovered:
            repo_rel = (root / rel_posix).relative_to(_REPO_ROOT).as_posix()
            if _is_test_path(repo_rel):
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits.extend(scan_file(repo_rel, text))
    return hits


def test_live_unbounded_sites_are_inside_the_inventory() -> None:
    live = site_keys(_collect_live_hits())
    new = live - KNOWN_UNBOUNDED_SITES
    assert not new, (
        "unbounded machine-local spawn(s): route through coordinator_core.machine_local_spawn "
        f"(run_bounded/dump/get) or add `timeout=<=2.0` with a TimeoutExpired fallback: {sorted(new)}"
    )


def test_inventory_has_no_stale_entries() -> None:
    stale = KNOWN_UNBOUNDED_SITES - site_keys(_collect_live_hits())
    assert not stale, f"converted sites still listed; remove them (target: empty set): {sorted(stale)}"


def test_inventory_keys_are_function_qualified_not_line_numbers() -> None:
    for key in KNOWN_UNBOUNDED_SITES:
        path, _, scope = key.partition("::")
        assert path.endswith(".py") and scope, key
        assert not re.search(r":\d+$", key), key


_P0_CALLER = '''
import subprocess

def _machine_local_dump():
    impl = _machine_local_impl()
    python = _resolve_python()
    if impl.endswith(".py"):
        cmd = [python, impl, "dump", "--include-unset"]
    else:
        cmd = [impl, "dump", "--include-unset"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
'''


def _keys(body: str, path: str = "coordinator/bin/_fx.py") -> set[str]:
    return site_keys(scan_file(path, body))


def test_fires_on_the_literal_unbounded_dump_shape() -> None:
    body = '''
import subprocess

def read(python, impl):
    return subprocess.run([python, impl, "dump", "--include-unset"], capture_output=True)
'''
    assert _keys(body) == {"coordinator/bin/_fx.py::read"}


def test_fires_on_the_known_p0_caller_shape_through_a_two_branch_binding() -> None:
    assert _keys(_P0_CALLER) == {"coordinator/bin/_fx.py::_machine_local_dump"}


def test_silent_when_the_same_call_carries_a_ceiling() -> None:
    body = '''
import subprocess

def read(python, impl):
    return subprocess.run([python, impl, "dump", "--include-unset"], capture_output=True, timeout=2.0)
'''
    assert _keys(body) == set()


def test_silent_on_the_bounded_helper() -> None:
    body = '''
from coordinator_core import machine_local_spawn

def read(python, impl):
    a = machine_local_spawn.run_bounded([python, impl, "dump", "--include-unset"])
    return machine_local_spawn.dump(impl)
'''
    assert _keys(body) == set()


def test_fires_on_a_ceiling_above_the_limit_and_on_a_none_ceiling() -> None:
    over = 'import subprocess\ndef r(impl):\n    return subprocess.run([impl, "get", "k"], timeout=5)\n'
    unset = 'import subprocess\ndef r(impl):\n    return subprocess.run([impl, "get", "k"], timeout=None)\n'
    assert _keys(over) == {"coordinator/bin/_fx.py::r"}
    assert _keys(unset) == {"coordinator/bin/_fx.py::r"}


def test_fires_on_a_ceiling_bound_to_an_oversized_constant() -> None:
    body = '''
import subprocess

_T = 10

def r(impl):
    return subprocess.run([impl, "get", "k"], timeout=_T)
'''
    assert _keys(body) == {"coordinator/bin/_fx.py::r"}


def test_silent_on_the_named_ceiling_constant() -> None:
    body = '''
import subprocess

def r(impl):
    return subprocess.run([impl, "get", "k"], timeout=ML_SPAWN_TIMEOUT_S)
'''
    assert _keys(body) == set()


def test_sees_through_starred_unpacking_and_every_subcommand() -> None:
    for sub in ("get", "keys", "dump", "set", "unset", "has", "require"):
        body = f'import subprocess\ndef r(ml_argv, key):\n    subprocess.run([*ml_argv, "{sub}", key])\n'
        assert _keys(body) == {"coordinator/bin/_fx.py::r"}, sub


def test_silent_on_an_unrelated_spawn_with_a_subcommand_word() -> None:
    body = 'import subprocess\ndef r():\n    subprocess.run(["git", "config", "get", "x"])\n    subprocess.run(["npm", "set", "k"])\n'
    assert _keys(body) == set()


def test_popen_is_bounded_only_by_a_waiting_ceiling_in_scope() -> None:
    unbounded = 'import subprocess\ndef r(impl):\n    p = subprocess.Popen([impl, "dump"])\n    return p.communicate()\n'
    bounded = 'import subprocess\ndef r(impl):\n    p = subprocess.Popen([impl, "dump"])\n    return p.communicate(timeout=2.0)\n'
    assert _keys(unbounded) == {"coordinator/bin/_fx.py::r"}
    assert _keys(bounded) == set()


def test_fires_on_a_timeout_handler_that_respawns_or_continues() -> None:
    respawn = '''
import subprocess

def r(impl):
    try:
        return subprocess.run([impl, "get", "k"], timeout=2.0)
    except subprocess.TimeoutExpired:
        return subprocess.run([impl, "get", "k"], timeout=2.0)
'''
    loop = '''
import subprocess

def r(impl):
    for _ in range(3):
        try:
            return subprocess.run([impl, "get", "k"], timeout=2.0)
        except subprocess.TimeoutExpired:
            continue
'''
    fallback = '''
import subprocess

def r(impl):
    try:
        return subprocess.run([impl, "get", "k"], timeout=2.0)
    except subprocess.TimeoutExpired:
        return None
'''
    assert _keys(respawn) == {"coordinator/bin/_fx.py::r~retry"}
    assert _keys(loop) == {"coordinator/bin/_fx.py::r~retry"}
    assert _keys(fallback) == set()


def test_site_keys_do_not_move_with_line_numbers() -> None:
    body = 'import subprocess\n\ndef _get(key):\n    return subprocess.run(["machine-local", "get", key])\n'
    shifted = "# c\n# c\n" + body
    assert _keys(body) == _keys(shifted) == {"coordinator/bin/_fx.py::_get"}


def test_second_hit_in_one_scope_gets_a_numbered_key() -> None:
    body = 'import subprocess\ndef r(impl):\n    subprocess.run([impl, "get", "a"])\n    subprocess.run([impl, "get", "b"])\n'
    assert _keys(body) == {"coordinator/bin/_fx.py::r", "coordinator/bin/_fx.py::r#2"}


def test_fires_on_a_passthrough_wrapper_and_on_a_summed_oversized_ceiling() -> None:
    wrapper = 'import subprocess\ndef _run_machine_local(machine_local_bin, *args):\n    return subprocess.run([str(machine_local_bin), *args])\n'
    summed = 'import subprocess\ndef r(impl):\n    return subprocess.run([impl, "get", "k"], timeout=2.0 + 10.0)\n'
    assert _keys(wrapper) == {"coordinator/bin/_fx.py::_run_machine_local"}
    assert _keys(summed) == {"coordinator/bin/_fx.py::r"}


def test_machine_local_module_names_its_own_opaque_reader_invocation() -> None:
    body = 'import subprocess\ndef r(key):\n    inv = _reader_invocation()\n    return subprocess.run(inv + ["get", key])\n'
    assert _keys(body, "coordinator/templates/bin/claude_machine_local.py") == {
        "coordinator/templates/bin/claude_machine_local.py::r"
    }
    assert _keys(body, "coordinator/bin/other.py") == set()


def test_test_paths_are_not_scanned() -> None:
    assert _is_test_path("coordinator/bin/tests/test_x.py")
    assert _is_test_path("coordinator/bin/lib/test_git_hook_install.py")
    assert not _is_test_path("coordinator/bin/cross-repo-memo.py")
