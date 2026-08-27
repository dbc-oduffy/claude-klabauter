"""Standing structural guard (C3, plan
`docs/plans/2026-08-14-the-venv-fallback-stops-being-something.md` § AC4/AC5):
no engine code resolves ITS OWN dependencies (interpreter, importable
package, `sys.path`/`sys.executable`) through
`<settings-home>/.coordinator-venv` except the explicit, enumerated sites
that legitimately own, build, probe, or reclaim it.

`<settings-home>/.coordinator-venv` is a shared, mutable tree, rebuilt by
rename-swap while 50-70 concurrent LLM sessions run on this machine (see
CLAUDE.md § Load norm). A site that quietly resolves its own dependencies
through it re-creates exactly the coupling this plan's C1/C2 chunks remove
from `scripts/setup.py` and `substrate.py::resolve_hook_python_bin` --
this test is what stops that coupling from silently reappearing once the
people who know why have moved on.

Shape (narrowly scoped, ast-based, precedent
`coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py`): a
violation is a subprocess spawn call, a `sys.executable` assignment, or a
`sys.path`/`site.addsitedir` mutation whose argument expression is
"venv-derived" -- a literal `.coordinator-venv` path fragment, an inline
`venv_python_path(...)` call, or a local variable assigned from one. This
proves RESOLUTION (running/importing through the venv interpreter), not
mere textual mention -- a docstring citation, a dir-exclusion marker, or a
health-probe path build (health-checking existence/importability is not
"resolving a dependency through it") does not trip the detector, by
construction of what counts as a sink.

Allowlist is enumerated by relpath only (AC5) -- never a comment, never a
path-prefix heuristic. Extending it requires a one-line reason in this
file, same discipline as `_ALLOWLISTED_RELPATHS` in the node-shellout
precedent.

Known gaps (this guard is a literal/AST-shaped detector, not a policy
auditor -- see DR-307's Negative-spec). Do not read a green run of this
test as stronger evidence than these gaps allow:

- **Dynamic dispatch.** `getattr(module, "venv_python_path")(...)` -- the
  outer call's `.func` is itself a `Call`, not an `Attribute`/`Name`, so
  `_call_name` can't resolve it and the sink check never fires. Not
  chased: defeating dynamic dispatch statically is a losing game for an
  AST-shaped guard and over-broadening it costs false positives.
- **`os.environ` round-trips.** `os.environ["X"] = str(venv_python_path(...))`
  followed by `subprocess.run([os.environ["X"], ...])` is untracked --
  taint propagation only follows `ast.Assign` targets that are plain
  `ast.Name` nodes, not subscript targets/reads. Out of proportion to
  chase here.
- **Pin-mediated policy preference with no venv string in the code at
  all** -- the original defect's own shape. Not this guard's job by
  design; held instead by
  `test_resolve_hook_python_bin_ignores_the_venv_pin`
  (`coordinator_core/install/test_substrate.py`). See DR-307 § "The known
  gap".

Covered, not gaps: `sys.path.insert/append/extend` (receiver-checked
against literal `sys.path` or a module-level name directly assigned from
`sys.path`, e.g. `p = sys.path; p.insert(...)`), import-aliased resolver
calls (`from ... import venv_python_path as vpp`), and split string-literal
concatenation (`".coordinator" + "-venv"`, folded via bounded
constant-folding of `+`-joined `ast.Constant` string nodes).

- **`sys.path`-alias chains beyond one hop.** `p = sys.path; q = p;
  q.insert(...)` -- `_collect_sys_path_aliases` only recognizes a name
  assigned directly from the `sys.path` literal chain, not transitively
  through another already-collected alias. Not chased: the same
  proportionality call as the resolver-alias taint above, and a two-hop
  indirection through an unused local is a narrow evasion shape for a
  structural guard to chase further.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Sweep roots: the engine package itself, plus the two named non-package
# sites the plan's own inventory (state/audits/2026-08-14-coordinator-venv-
# dependant-inventory.md) calls out as living outside coordinator_core/.
_SCAN_ROOTS = (
    _REPO_ROOT / "coordinator_core",
    _REPO_ROOT / "scripts",
    _REPO_ROOT / "coordinator" / "bin",
)

_SPAWN_CALL_NAMES = {"run", "Popen", "check_output", "call", "check_call"}
_PATH_MUTATION_CALL_NAMES = {"addsitedir"}  # site.addsitedir(...)
# sys.path.insert/append/extend(...) -- method name alone is far too common
# (any list) to gate on; _is_sys_path_mutation additionally confirms the
# receiver expression is literally `sys.path` before these count as a sink.
_SYS_PATH_MUTATION_METHOD_NAMES = {"insert", "append", "extend"}

_VENV_LITERAL = ".coordinator-venv"
_VENV_RESOLVER_CALL_NAME = "venv_python_path"

# Explicit, enumerated allowlist (AC5). Each entry names the file and the
# reason it legitimately touches the venv; membership is never implied by a
# comment in the allowlisted file itself, and never by a path prefix.
_ALLOWLISTED_RELPATHS = {
    # Owns and builds the venv -- every legitimate venv resolution/spawn
    # bottoms out here by construction; this IS the resolver.
    "coordinator_core/install/ensure_venv.py":
        "owns and builds the venv",
    # Purpose (c), consented-to by PM ruling 2026-08-14 (DR-307) and narrowed
    # 2026-08-17 (DR-317, machine-first-install-surface plan, C2): the opt-in
    # `--allow-venv-fallback` branch of `provision_deps`, now break-glass-only
    # and PEP-668-excluded -- reaching the venv here requires an explicit
    # install-time flag on every run, never an automatic degradation and
    # never reached for a PEP-668 refusal.
    "scripts/setup.py":
        "provision_deps' opt-in --allow-venv-fallback branch (purpose c, C2)",
}


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_excluded_source_path(path: Path) -> bool:
    """Test files are out of scope -- this gate governs production code
    that RUNS via the venv, not the tests (including this one) that assert
    about it."""
    if path.name.startswith("test_"):
        return True
    return "tests" in path.parts


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _try_fold_str_concat(node: ast.expr) -> str | None:
    """Statically fold a `BinOp` chain of `+`-joined string `Constant`s into
    a single string (P2 fix for split-literal concatenation, e.g.
    `".coordinator" + "-venv"`) -- bounded to the literal-concat shape only,
    never crossing a variable or a non-Add operator."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _try_fold_str_concat(node.left)
        right = _try_fold_str_concat(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _collect_resolver_aliases(tree: ast.AST) -> set[str]:
    """Module-level `from ... import venv_python_path as vpp`-shaped import
    aliases, treated as equivalent resolver call names alongside the bare
    `venv_python_path` -- an aliased import is still the same resolver, just
    under a local name (P2 fix)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name == _VENV_RESOLVER_CALL_NAME and alias.asname:
                aliases.add(alias.asname)
    return aliases


def _value_is_venv_derived_seed(value: ast.expr, tainted: set[str], resolver_names: frozenset[str]) -> bool:
    """True if `value`'s subtree already resolves the venv, without
    consulting anything beyond a literal `.coordinator-venv` fragment, an
    inline `venv_python_path(...)` call (or a known import alias of it), or
    a currently-known tainted name -- the per-iteration seed
    `_collect_venv_tainted_names` grows to a fixed point over
    (`py = str(other_tainted_var)`-shaped chains)."""
    for node in ast.walk(value):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _VENV_LITERAL in node.value:
            return True
        if isinstance(node, ast.BinOp):
            folded = _try_fold_str_concat(node)
            if folded is not None and _VENV_LITERAL in folded:
                return True
        if isinstance(node, ast.Call) and _call_name(node.func) in resolver_names:
            return True
        if isinstance(node, ast.Name) and node.id in tainted:
            return True
    return False


def _collect_venv_tainted_names(tree: ast.AST) -> set[str]:
    """Names whose assigned value resolves to the venv -- directly (a
    literal `.coordinator-venv` fragment or an inline `venv_python_path(...)`
    call, including through an import alias of it) or transitively
    (`py = str(already_tainted_var)`). Fixed-point over the module's
    assignments. Flat (not per-scope) by deliberate design: a false
    positive from name reuse across two unrelated functions is a safe
    direction for a structural guard -- the allowlist is the escape valve,
    never a narrower taint scope that could miss a real resolution."""
    resolver_names = frozenset({_VENV_RESOLVER_CALL_NAME}) | _collect_resolver_aliases(tree)
    assigns = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in assigns:
            if not _value_is_venv_derived_seed(node.value, tainted, resolver_names):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in tainted:
                    tainted.add(target.id)
                    changed = True
    return tainted


def _is_venv_derived(expr: ast.expr, tainted: set[str], resolver_names: frozenset[str]) -> bool:
    """True if `expr`'s subtree resolves the venv: a literal
    `.coordinator-venv` path fragment, an inline `venv_python_path(...)`
    call (or a known import alias of it), or a reference to a locally
    venv_python_path-tainted name."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _VENV_LITERAL in node.value:
            return True
        if isinstance(node, ast.BinOp):
            folded = _try_fold_str_concat(node)
            if folded is not None and _VENV_LITERAL in folded:
                return True
        if isinstance(node, ast.Call) and _call_name(node.func) in resolver_names:
            return True
        if isinstance(node, ast.Name) and node.id in tainted:
            return True
    return False


def _is_sys_path_literal_receiver(receiver: ast.expr) -> bool:
    """True for the literal attribute chain `sys.path` itself."""
    return (
        isinstance(receiver, ast.Attribute)
        and receiver.attr == "path"
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "sys"
    )


def _collect_sys_path_aliases(tree: ast.AST) -> set[str]:
    """Module-level names assigned directly from `sys.path`
    (`p = sys.path`) -- P2 fix for the `p.insert(...)` evasion shape, where
    the mutating call's receiver is a plain `ast.Name` rather than the
    literal `sys.path` attribute chain. Direct assignment only (not
    transitive through another alias) -- deliberately narrow, mirroring the
    other alias-collection helpers in this module."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not _is_sys_path_literal_receiver(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _is_sys_path_mutation(func: ast.expr, name: str | None, sys_path_aliases: frozenset[str]) -> bool:
    """True for `sys.path.insert/append/extend(...)` -- not any object with
    one of these extremely common list-method names. Requires the receiver
    expression to be either the literal attribute chain `sys.path` (an
    `ast.Attribute` with `.attr == "path"` on an `ast.Name` `sys`), or a
    plain `ast.Name` directly assigned from `sys.path` (`p = sys.path;
    p.insert(...)`, P2 fix) -- not merely a method-name match."""
    if name not in _SYS_PATH_MUTATION_METHOD_NAMES:
        return False
    if not isinstance(func, ast.Attribute):
        return False
    receiver = func.value
    if _is_sys_path_literal_receiver(receiver):
        return True
    return isinstance(receiver, ast.Name) and receiver.id in sys_path_aliases


def _call_arg_exprs(call: ast.Call) -> list[ast.expr]:
    exprs = list(call.args)
    for kw in call.keywords:
        if kw.value is not None:
            exprs.append(kw.value)
    return exprs


class VenvResolutionVisitor(ast.NodeVisitor):
    """Collects (lineno, description) for every venv-derived resolution
    sink found in one parsed module."""

    def __init__(self, tainted: set[str], resolver_names: frozenset[str], sys_path_aliases: frozenset[str]) -> None:
        self._tainted = tainted
        self._resolver_names = resolver_names
        self._sys_path_aliases = sys_path_aliases
        self.violations: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        is_path_mutation = name in _PATH_MUTATION_CALL_NAMES or _is_sys_path_mutation(node.func, name, self._sys_path_aliases)
        if name in _SPAWN_CALL_NAMES or is_path_mutation:
            for expr in _call_arg_exprs(node):
                if _is_venv_derived(expr, self._tainted, self._resolver_names):
                    kind = "subprocess spawn" if name in _SPAWN_CALL_NAMES else "sys.path mutation"
                    self.violations.append((node.lineno, f"{kind} ({name}) resolved through the venv"))
                    break
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        is_sys_executable = any(
            isinstance(t, ast.Attribute) and t.attr == "executable"
            and isinstance(t.value, ast.Name) and t.value.id == "sys"
            for t in node.targets
        )
        if is_sys_executable and _is_venv_derived(node.value, self._tainted, self._resolver_names):
            self.violations.append((node.lineno, "sys.executable repointed at the venv interpreter"))
        self.generic_visit(node)


def find_venv_dependency_resolutions(roots: tuple[Path, ...]) -> list[tuple[str, int, str]]:
    """Walk `roots` for .py files (excluding tests/ and test_*.py) and
    return every (relpath, lineno, description) tuple that is a venv-derived
    dependency resolution, skipping allowlisted paths.

    Used both against the real tree (the standing gate) and against an
    isolated tmp_path fixture (this gate's own self-test, proving it
    actually detects rather than merely passing by absence).
    """
    violations: list[tuple[str, int, str]] = []
    seen_paths: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            if _is_excluded_source_path(path):
                continue
            relpath = _relpath(path, root)
            if relpath in _ALLOWLISTED_RELPATHS:
                continue
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError:
                continue
            resolver_names = frozenset({_VENV_RESOLVER_CALL_NAME}) | _collect_resolver_aliases(tree)
            tainted = _collect_venv_tainted_names(tree)
            sys_path_aliases = frozenset(_collect_sys_path_aliases(tree))
            visitor = VenvResolutionVisitor(tainted, resolver_names, sys_path_aliases)
            visitor.visit(tree)
            for lineno, description in visitor.violations:
                violations.append((relpath, lineno, description))
    return violations


def test_no_engine_code_resolves_dependencies_through_the_venv():
    """Standing gate: zero non-allowlisted sites resolve their own
    dependencies (interpreter spawn, sys.executable, sys.path/site
    mutation) through `<settings-home>/.coordinator-venv`.

    `<settings-home>/.coordinator-venv` is a shared, mutable tree rebuilt
    by rename-swap while 50-70 concurrent sessions run on this machine --
    a site that resolves through it recreates the exact coupling this plan
    removes. Sanctioned alternative: resolve through
    `coordinator_core.pyresolve.resolve_python_bin()` (the machine
    interpreter this plan makes the default), or add an enumerated
    allowlist entry with a stated reason if the site is a genuine owner/
    probe/reclaim/opt-in-fallback role.
    """
    violations = find_venv_dependency_resolutions(_SCAN_ROOTS)
    assert violations == [], (
        "Found engine code resolving its own dependencies through the "
        "shared, mutable .coordinator-venv tree (rebuilt live under "
        "concurrent sessions). Resolve via coordinator_core.pyresolve."
        f"resolve_python_bin() instead, or add an enumerated allowlist "
        f"entry with a stated reason: {violations}"
    )


def test_gate_detects_a_planted_subprocess_spawn_through_venv_python_path(tmp_path):
    """Proves the gate has teeth: without this, the standing assertion
    above would be passing by absence, not by detection."""
    fixture = tmp_path / "fixture_reintroduced_venv_resolution.py"
    fixture.write_text(
        "import subprocess\n"
        "from coordinator_core.install.ensure_venv import venv_python_path\n"
        "from coordinator_core._settings_home import settings_home\n"
        "\n"
        "def run_under_venv():\n"
        "    venv_py = venv_python_path(settings_home() / '.coordinator-venv')\n"
        "    return subprocess.run([str(venv_py), '-c', 'import coordinator_whoami'])\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_reintroduced_venv_resolution.py")
    assert lineno == 7
    assert "subprocess spawn" in description


def test_gate_detects_a_planted_inline_venv_literal_spawn(tmp_path):
    """A second planted shape: no intermediate variable, a bare literal
    `.coordinator-venv` path fragment built inline into the spawn call."""
    fixture = tmp_path / "fixture_inline_literal_spawn.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def run_hook(settings_home_path):\n"
        "    py = str(settings_home_path / '.coordinator-venv' / 'bin' / 'python')\n"
        "    return subprocess.run([py, '-c', 'pass'])\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_inline_literal_spawn.py")
    assert "subprocess spawn" in description


def test_gate_detects_a_planted_sys_executable_repoint(tmp_path):
    """Third planted shape: `sys.executable` itself repointed at the venv,
    the mechanism `_hook_venv_inject.py`-style ambient injection uses."""
    fixture = tmp_path / "fixture_sys_executable_repoint.py"
    fixture.write_text(
        "import sys\n"
        "from coordinator_core.install.ensure_venv import venv_python_path\n"
        "\n"
        "def repoint(venv_dir):\n"
        "    sys.executable = str(venv_python_path(venv_dir))\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_sys_executable_repoint.py")
    assert "sys.executable" in description


def test_gate_detects_a_planted_sys_path_insert(tmp_path):
    """P1 fix: `sys.path.insert(0, ...)` is the most idiomatic way to add an
    import path -- arguably more common than `site.addsitedir` -- and must
    be caught alongside it, per the module's own docstring claim."""
    fixture = tmp_path / "fixture_sys_path_insert.py"
    fixture.write_text(
        "import sys\n"
        "from coordinator_core.install.ensure_venv import venv_python_path\n"
        "\n"
        "def inject(venv_dir):\n"
        "    sys.path.insert(0, str(venv_python_path(venv_dir).parent / 'lib'))\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_sys_path_insert.py")
    assert "sys.path mutation" in description


def test_gate_detects_a_planted_sys_path_alias_insert(tmp_path):
    """P2 fix: `p = sys.path; p.insert(...)` -- the receiver of the
    mutating call is a plain `ast.Name` bound to `sys.path`, not the
    literal attribute chain. Must be caught the same as the direct-chain
    shape."""
    fixture = tmp_path / "fixture_sys_path_alias_insert.py"
    fixture.write_text(
        "import sys\n"
        "from coordinator_core.install.ensure_venv import venv_python_path\n"
        "\n"
        "def inject(venv_dir):\n"
        "    p = sys.path\n"
        "    p.insert(0, str(venv_python_path(venv_dir)))\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_sys_path_alias_insert.py")
    assert "sys.path mutation" in description


def test_gate_ignores_a_plain_list_alias_unrelated_to_sys_path(tmp_path):
    """Negative control for the P2 alias fix: `p = some_list; p.insert(...)`
    must not trip the detector -- `p` is not assigned from `sys.path`, so it
    must not be treated as a `sys.path` alias."""
    fixture = tmp_path / "fixture_unrelated_list_alias.py"
    fixture.write_text(
        "def build_exclude_args():\n"
        "    some_list = ['.coordinator-venv']\n"
        "    p = some_list\n"
        "    p.insert(0, '.coordinator-venv')\n"
        "    return p\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert violations == []


def test_gate_ignores_insert_append_extend_on_a_plain_list(tmp_path):
    """Negative control for the P1 fix: `insert`/`append`/`extend` are
    extremely common list-method names -- the guard must confirm the
    receiver is literally `sys.path`, not merely match the method name."""
    fixture = tmp_path / "fixture_plain_list_methods.py"
    fixture.write_text(
        "def build_exclude_args():\n"
        "    some_list = []\n"
        "    some_list.append('.coordinator-venv')\n"
        "    some_list.insert(0, '.coordinator-venv')\n"
        "    some_list.extend(['.coordinator-venv'])\n"
        "    return some_list\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert violations == []


def test_gate_detects_a_planted_aliased_resolver_import(tmp_path):
    """P2 fix: `from ... import venv_python_path as vpp` is still the same
    resolver under a local alias -- module-level import aliases are
    collected and treated as equivalent resolver call names."""
    fixture = tmp_path / "fixture_aliased_resolver.py"
    fixture.write_text(
        "import subprocess\n"
        "from coordinator_core.install.ensure_venv import venv_python_path as vpp\n"
        "\n"
        "def run_under_venv():\n"
        "    return subprocess.run([str(vpp(None)), '-c', 'pass'])\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_aliased_resolver.py")
    assert "subprocess spawn" in description


def test_gate_detects_a_planted_split_literal_concat(tmp_path):
    """P2 fix: `".coordinator" + "-venv"` (two separate string `Constant`
    nodes joined by `ast.BinOp`) is folded and checked the same as a
    single-Constant literal."""
    fixture = tmp_path / "fixture_split_literal_concat.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def run_hook(settings_home_path):\n"
        "    marker = '.coordinator' + '-venv'\n"
        "    py = str(settings_home_path / marker / 'bin' / 'python')\n"
        "    return subprocess.run([py, '-c', 'pass'])\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_split_literal_concat.py")
    assert "subprocess spawn" in description


def test_gate_ignores_docstring_mentions_and_health_probes(tmp_path):
    """Negative control: the exact shape of noise this gate must NOT flag
    -- a docstring citing `.coordinator-venv`, a dir-exclusion marker, and a
    health-probe call that merely checks importability without spawning."""
    fixture = tmp_path / "fixture_benign.py"
    fixture.write_text(
        '"""References <settings-home>/.coordinator-venv/bin/python for '
        'context only."""\n'
        "import subprocess\n"
        "\n"
        "_EXCLUDE_DIR_MARKERS = ('/node_modules/', '/.coordinator-venv/')\n"
        "\n"
        "def probe_something_unrelated():\n"
        "    return subprocess.run(['git', 'status'], capture_output=True)\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert violations == []


def test_gate_flags_a_venv_literal_variable_reused_for_an_unrelated_spawn(tmp_path):
    """Pins the module's own documented tradeoff (flat, not per-scope,
    taint -- see `_collect_venv_tainted_names`'s docstring): a name once
    tainted by a legitimate non-resolving use (e.g. an `rsync --exclude`
    marker) is treated as tainted everywhere that name is referenced,
    including an unrelated later reuse of the same identifier that reaches
    a spawn call for a reason that has nothing to do with the venv.

    This IS a false positive on the strict "does this call actually
    resolve through the venv" question -- and it is the guard's stated,
    deliberate direction (a missed real resolution is worse than an
    over-flagged unrelated one). If this starts failing, that is a design
    change to `_collect_venv_tainted_names`'s scoping, not a regression in
    this test."""
    fixture = tmp_path / "fixture_reused_name_false_positive.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def build_rsync_exclude_args():\n"
        "    exclude_marker = '.coordinator-venv'\n"
        "    return ['--exclude', exclude_marker]\n"
        "\n"
        "def run_unrelated_tool():\n"
        "    exclude_marker = 'totally-unrelated-value'\n"
        "    return subprocess.run(['some-tool', exclude_marker])\n",
        encoding="utf-8",
    )

    violations = find_venv_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_reused_name_false_positive.py")
    assert "subprocess spawn" in description


def test_allowlist_entries_are_enumerated_with_a_reason():
    """AC5: a carve-out is membership in the explicit list, never a bare
    path -- every entry must carry a non-empty reason string."""
    assert _ALLOWLISTED_RELPATHS, "allowlist must not be empty by construction of the guard"
    for relpath, reason in _ALLOWLISTED_RELPATHS.items():
        assert reason and reason.strip(), f"allowlist entry {relpath!r} has no stated reason"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
