"""Standing structural guard (C8, plan
`docs/plans/2026-08-16-one-environment-for-the-fleet.md` § AC7): no new code
resolves FLEET dependencies through a per-repo venv or a hardcoded
environment path instead of C1's `fleet_env.root` registry key
(`coordinator/bin/fleet-env.py::resolve_fleet_env_root`).

Per-repo venvs are banned fleet-wide (this plan's own opening finding); the
whole point of C1/C4/C5/C6 is that there is exactly one documented way to
reach the shared environment. A site that quietly re-derives its own path to
it -- a hardcoded `.fleet-env` literal, a direct re-read of the
`fleet_env.root` machine-local key, or a freshly-minted per-repo venv doing
the job the shared environment already does -- recreates the coupling this
plan exists to retire, once the people who know why have moved on.

Shape (narrowly scoped, ast-based, precedent
`coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py`, whose
allowlist/self-test discipline this file also follows): three independent
sink checks, each proving RESOLUTION rather than mere textual mention.

  1. **Hardcoded environment path.** A `.fleet-env` path fragment (the
     basename C1/C4/C5 use, contracted in `fleet_env_resolve.py`) reaching a
     resolution sink -- a subprocess spawn, a `sys.executable` assignment,
     or a `sys.path`/`site.addsitedir` mutation.
  2. **Direct key read bypassing the resolver.** A call to
     `_machine_local_get("fleet_env.root")` (bare, attribute, or import-
     aliased) anywhere except `coordinator/bin/fleet-env.py` itself, which
     IS the sanctioned resolver `resolve_fleet_env_root` wraps around this
     exact call. Any other site re-deriving the same read is bypassing C1's
     one documented entry point, even though the key it reads is correct.
  3. **Per-repo venv for fleet dependencies.** A venv-creation call
     (`venv.create(...)`, `venv.EnvBuilder(...).create(...)`, or a
     subprocess spawn shaped like `python -m venv <target>` / `uv venv
     <target>`) where the call's own string arguments also reference
     "fleet" -- the conjunctive shape (creation-call AND fleet-labelled
     target) that flags a fresh per-repo venv purpose-built for fleet deps
     without also flagging this repo's many unrelated, legitimate `.venv`
     references (drift.py's per-plugin venvs, whoami_run_tests.py's own
     test venv, exclude-dir markers, etc. -- none of which say "fleet" and
     none of which this guard's remit reaches; a repo-wide "no more `.venv`
     anywhere" sweep is a much larger, undertaken-elsewhere scope than C8's
     fleet-dependency-resolution guard).

Disjoint from the sibling guard, by construction of what each one's sink
looks for: `coordinator_core/install/tests/test_no_venv_dependency_resolution.py`
(C3, `docs/plans/2026-08-14-the-venv-fallback-stops-being-something.md`)
fires on the literal `.coordinator-venv` fragment and `venv_python_path(...)`
-- the settings-home venv that hosts whoami. This guard never looks for that
literal or that resolver call at all; it looks for `.fleet-env`, a direct
`fleet_env.root` key read, and a fleet-labelled venv-creation call. A file
could in principle trip both guards (each is scoped to its own literal), but
neither guard's detector logic references the other's sink, so the two never
double-fire on the SAME resolution -- one always names its own mechanism,
never the sibling's.

Allowlist is enumerated at two granularities -- never a comment, never a
path-prefix heuristic. `_ALLOWLISTED_RELPATHS` exempts a whole file (for a
module that IS the resolver); `_ALLOWLISTED_FUNCTION_SITES` exempts one
named function inside an otherwise-governed file, so a large module carrying
exactly one sanctioned site does not get blanket immunity for everything else
in it. Extending either requires a stated reason in this file, same discipline
as `_ALLOWLISTED_RELPATHS` in the node-shellout precedent.

The function-granular carve-out exists for the WRITER-vs-RESOLVER distinction:
this guard stops consumers re-deriving the fleet-env location, but whatever
SEEDS `fleet_env.root` must construct that path to store it, and cannot ask the
resolver for a value it is itself establishing.

Known gaps (this guard is a literal/AST-shaped detector, not a policy
auditor): dynamic dispatch (`getattr(module, "_machine_local_get")(...)`),
`os.environ` round-trips, and any resolution that never puts a literal
".fleet-env"/"fleet_env.root"/"fleet"-labelled string anywhere in the code
(e.g. a value built entirely from an opaque config object) are not chased --
same proportionality call the sibling C3 guard's own docstring makes for its
analogous gaps.

Spec backlink: docs/plans/2026-08-16-one-environment-for-the-fleet.md C8 (AC7)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Sweep roots: the engine package, plus the two non-package sites the
# sibling C3 guard's own inventory already treats as in-scope for this
# family of "must not reappear" resolution guards.
_SCAN_ROOTS = (
    _REPO_ROOT / "coordinator_core",
    _REPO_ROOT / "scripts",
    _REPO_ROOT / "coordinator" / "bin",
)

_SPAWN_CALL_NAMES = {"run", "Popen", "check_output", "call", "check_call"}
_PATH_MUTATION_CALL_NAMES = {"addsitedir"}  # site.addsitedir(...)
_SYS_PATH_MUTATION_METHOD_NAMES = {"insert", "append", "extend"}

_FLEET_ENV_LITERAL = ".fleet-env"
_MACHINE_LOCAL_GET_NAME = "_machine_local_get"
_FLEET_ENV_ROOT_KEY = "fleet_env.root"
_VENV_CREATE_CALL_NAMES = {"create"}  # venv.create(...) / EnvBuilder(...).create(...)
_FLEET_TOKEN = "fleet"

# Explicit, enumerated allowlist. Each entry names the file and the reason it
# legitimately performs a pattern this guard would otherwise flag; membership
# is never implied by a comment in the allowlisted file itself, and never by
# a path prefix.
_ALLOWLISTED_RELPATHS = {
    # C1: the sole sanctioned site that reads the raw `fleet_env.root`
    # machine-local key directly -- `resolve_fleet_env_root` below IS the
    # resolver every other site is required to call instead of re-deriving
    # this same read.
    "coordinator/bin/fleet-env.py":
        "C1's resolve_fleet_env_root -- the resolver itself, not a bypass",
}

# Function-granular allowlist, keyed (relpath, enclosing function). Whole-file
# membership above is too coarse for a large module that legitimately contains
# exactly one sanctioned site: allowlisting `scripts/setup.py` outright would
# blanket-exempt the entire installer, and this guard's own discipline is that
# a carve-out is never implied by a path prefix.
#
# The distinction that earns an entry here is WRITER vs RESOLVER. This guard
# exists to stop consumers RE-DERIVING the fleet-env location instead of asking
# the resolver. A site that WRITES the `fleet_env.root` key must construct the
# path -- that is what seeding means -- and cannot call the resolver to learn a
# value it is itself establishing.
_ALLOWLISTED_FUNCTION_SITES = {
    ("scripts/setup.py", "_seed_fleet_env_root_from_klabauter"):
        "C4's seeder -- the WRITER of fleet_env.root, deriving the contracted "
        "<klabauter-root>/.fleet-env value to store it. Not a consumer "
        "bypassing the resolver.",
}


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_excluded_source_path(path: Path) -> bool:
    """Test files are out of scope -- this gate governs production code
    that RESOLVES fleet dependencies, not the tests (including this one)
    that assert about it."""
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
    a single string, mirroring the sibling C3 guard's own P2 fix for
    split-literal concatenation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _try_fold_str_concat(node.left)
        right = _try_fold_str_concat(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _string_constants(node: ast.expr) -> list[str]:
    """Collect every literal string constant reachable in `node`'s subtree,
    including `+`-joined literal concatenation, but not through a Name
    reference -- this scanner proves a literal, not a dynamically-built
    value."""
    strings: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            strings.append(sub.value)
    folded = _try_fold_str_concat(node)
    if folded is not None and folded not in strings:
        strings.append(folded)
    return strings


def _collect_import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map each local (possibly aliased) `from X import Y as Z` binding name
    back to its imported original name `Y`, so `visit_Call` can resolve
    `Z(...)` to the canonical `Y` before comparing against a sink name.
    Covers `from cc_invoke import _machine_local_get as get_ml` -> `{"get_ml":
    "_machine_local_get"}` (and the unaliased `import Y` form maps `Y -> Y`,
    a no-op that keeps the lookup total)."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = alias.name
    return aliases


def _collect_name_string_map(tree: ast.AST) -> dict[str, list[str]]:
    """Fixed-point pass over the module's assignments: names bound (directly
    or transitively through another already-known name) to an expression
    that itself contains a literal string constant. Mirrors the sibling C3
    guard's flat, module-wide (not per-scope) taint tracking, so that
    `py = str(Path(home) / '.fleet-env')` followed by
    `subprocess.run([py, ...])` is still seen as reaching the `.fleet-env`
    literal, not just a bare Name the direct-constant scan would miss."""
    mapping: dict[str, list[str]] = {}
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    changed = True
    while changed:
        changed = False
        for node in assigns:
            found = _string_constants_via_map(node.value, mapping)
            if not found:
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                existing = mapping.setdefault(target.id, [])
                for s in found:
                    if s not in existing:
                        existing.append(s)
                        changed = True
    return mapping


def _string_constants_via_map(expr: ast.expr, mapping: dict[str, list[str]]) -> list[str]:
    """`_string_constants`, extended to also pull in the strings recorded
    for any `Name` node whose id is a key in `mapping` (one-hop-per-pass,
    fixed-point over repeated calls during `_collect_name_string_map`)."""
    strings = list(_string_constants(expr))
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Name) and sub.id in mapping:
            for s in mapping[sub.id]:
                if s not in strings:
                    strings.append(s)
    return strings


def _expr_contains_literal(expr: ast.expr, needle: str, mapping: dict[str, list[str]]) -> bool:
    return any(needle in s for s in _string_constants_via_map(expr, mapping))


def _is_sys_path_literal_receiver(receiver: ast.expr) -> bool:
    return (
        isinstance(receiver, ast.Attribute)
        and receiver.attr == "path"
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "sys"
    )


def _is_sys_path_mutation(func: ast.expr, name: str | None) -> bool:
    if name not in _SYS_PATH_MUTATION_METHOD_NAMES:
        return False
    if not isinstance(func, ast.Attribute):
        return False
    return _is_sys_path_literal_receiver(func.value)


def _call_arg_exprs(call: ast.Call) -> list[ast.expr]:
    exprs = list(call.args)
    for kw in call.keywords:
        if kw.value is not None:
            exprs.append(kw.value)
    return exprs


def _is_venv_creation_shape(call: ast.Call, name: str | None, mapping: dict[str, list[str]]) -> bool:
    """True for `venv.create(...)` / `EnvBuilder(...).create(...)` (method
    name `create` on any receiver -- deliberately loose on the receiver,
    tightened instead by the caller's conjunctive "fleet"-labelled-argument
    requirement), or a subprocess spawn whose literal argv strings contain
    both a `venv` module invocation shape (`-m`, `venv`) or `uv venv`."""
    if name in _VENV_CREATE_CALL_NAMES and isinstance(call.func, ast.Attribute):
        return True
    spawn_name = _call_name(call.func)
    if spawn_name in _SPAWN_CALL_NAMES:
        strings: list[str] = []
        for expr in _call_arg_exprs(call):
            strings.extend(_string_constants_via_map(expr, mapping))
        if "venv" in strings and ("-m" in strings or "uv" in strings):
            return True
    return False


class FleetDependencyResolutionVisitor(ast.NodeVisitor):
    """Collects (lineno, description, enclosing_function) for every
    fleet-dependency-resolution violation found in one parsed module: a
    hardcoded `.fleet-env` literal reaching a resolution sink, a direct
    `fleet_env.root` key read, or a fleet-labelled per-repo venv-creation call.

    The enclosing function is carried so `_ALLOWLISTED_FUNCTION_SITES` can
    exempt one sanctioned site without exempting its whole module. It is the
    INNERMOST enclosing def (or None at module scope), so a nested helper never
    inherits its parent's carve-out."""

    def __init__(self, mapping: dict[str, list[str]], import_aliases: dict[str, str]) -> None:
        self._mapping = mapping
        self._import_aliases = import_aliases
        self._func_stack: list[str] = []
        self.violations: list[tuple[int, str, str | None]] = []

    @property
    def _enclosing_function(self) -> str | None:
        return self._func_stack[-1] if self._func_stack else None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        resolved_name = self._import_aliases.get(name, name) if name is not None else name

        # Check 2: direct machine-local key read bypassing the resolver.
        # Compared against `resolved_name` so an import-aliased binding
        # (`from cc_invoke import _machine_local_get as get_ml`) is caught
        # the same as the bare and attribute forms.
        if resolved_name == _MACHINE_LOCAL_GET_NAME:
            for expr in _call_arg_exprs(node):
                if _expr_contains_literal(expr, _FLEET_ENV_ROOT_KEY, self._mapping):
                    self.violations.append((
                        node.lineno,
                        "direct fleet_env.root key read bypassing "
                        "resolve_fleet_env_root",
                        self._enclosing_function,
                    ))
                    break

        # Check 1: hardcoded .fleet-env literal reaching a resolution sink.
        is_path_mutation = (
            name in _PATH_MUTATION_CALL_NAMES
            or _is_sys_path_mutation(node.func, name)
        )
        if name in _SPAWN_CALL_NAMES or is_path_mutation:
            for expr in _call_arg_exprs(node):
                if _expr_contains_literal(expr, _FLEET_ENV_LITERAL, self._mapping):
                    kind = "subprocess spawn" if name in _SPAWN_CALL_NAMES else "sys.path mutation"
                    self.violations.append((
                        node.lineno,
                        f"{kind} ({name}) resolved through a hardcoded "
                        ".fleet-env path",
                        self._enclosing_function,
                    ))
                    break

        # Check 3: fleet-labelled per-repo venv creation.
        if _is_venv_creation_shape(node, name, self._mapping):
            strings: list[str] = []
            for expr in _call_arg_exprs(node):
                strings.extend(_string_constants_via_map(expr, self._mapping))
            if any(_FLEET_TOKEN in s.lower() for s in strings):
                self.violations.append((
                    node.lineno,
                    "per-repo venv creation labelled for fleet dependencies",
                    self._enclosing_function,
                ))

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        is_sys_executable = any(
            isinstance(t, ast.Attribute) and t.attr == "executable"
            and isinstance(t.value, ast.Name) and t.value.id == "sys"
            for t in node.targets
        )
        if is_sys_executable and _expr_contains_literal(node.value, _FLEET_ENV_LITERAL, self._mapping):
            self.violations.append((
                node.lineno,
                "sys.executable repointed at a hardcoded .fleet-env path",
                self._enclosing_function,
            ))
        self.generic_visit(node)


def find_per_repo_fleet_dependency_resolutions(roots: tuple[Path, ...]) -> list[tuple[str, int, str]]:
    """Walk `roots` for .py files (excluding tests/ and test_*.py) and
    return every (relpath, lineno, description) tuple that is a
    per-repo-venv or hardcoded-path fleet dependency resolution, skipping
    allowlisted paths.

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
            mapping = _collect_name_string_map(tree)
            import_aliases = _collect_import_aliases(tree)
            visitor = FleetDependencyResolutionVisitor(mapping, import_aliases)
            visitor.visit(tree)
            for lineno, description, funcname in visitor.violations:
                if (relpath, funcname) in _ALLOWLISTED_FUNCTION_SITES:
                    continue
                violations.append((relpath, lineno, description))
    return violations


def test_no_new_code_resolves_fleet_dependencies_outside_fleet_env_root():
    """Standing gate: zero non-allowlisted sites resolve fleet dependencies
    through a per-repo venv, a hardcoded `.fleet-env` path, or a direct
    `fleet_env.root` key read.

    Per-repo venvs are banned fleet-wide; the shared environment's whole
    point is one documented way to reach it. Sanctioned alternative:
    `coordinator/bin/fleet-env.py::resolve_fleet_env_root()` (C1), or
    `coordinator_core.install.fleet_env_resolve.resolve_fleet_env_fallback_root`
    (C5) for the absent-key ladder -- or add an enumerated allowlist entry
    with a stated reason if the site is the resolver itself.
    """
    violations = find_per_repo_fleet_dependency_resolutions(_SCAN_ROOTS)
    assert violations == [], (
        "Found code resolving fleet dependencies through a per-repo venv or "
        "a hardcoded path instead of C1's fleet_env.root key. Resolve via "
        "coordinator/bin/fleet-env.py::resolve_fleet_env_root() (or C5's "
        "fallback ladder for the absent-key case) instead, or add an "
        f"enumerated allowlist entry with a stated reason: {violations}"
    )


def test_gate_detects_a_planted_hardcoded_fleet_env_literal_spawn(tmp_path):
    """Proves the gate has teeth for check 1: without this, the standing
    assertion above would be passing by absence, not by detection."""
    fixture = tmp_path / "fixture_hardcoded_fleet_env_path.py"
    fixture.write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        "\n"
        "def run_under_fleet_env(home):\n"
        "    py = str(Path(home) / '.fleet-env' / 'bin' / 'python')\n"
        "    return subprocess.run([py, '-c', 'pass'])\n",
        encoding="utf-8",
    )

    violations = find_per_repo_fleet_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_hardcoded_fleet_env_path.py")
    assert "subprocess spawn" in description
    assert ".fleet-env" in description


def test_gate_detects_a_planted_direct_machine_local_key_read(tmp_path):
    """Check 2: a direct `_machine_local_get('fleet_env.root')` call
    anywhere but the sanctioned resolver is a bypass, even though the key
    read is correct."""
    fixture = tmp_path / "fixture_direct_key_read.py"
    fixture.write_text(
        "from cc_invoke import _machine_local_get\n"
        "\n"
        "def rederive_fleet_env_root():\n"
        "    return _machine_local_get('fleet_env.root')\n",
        encoding="utf-8",
    )

    violations = find_per_repo_fleet_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_direct_key_read.py")
    assert "bypassing resolve_fleet_env_root" in description


def test_gate_detects_a_planted_import_aliased_machine_local_key_read(tmp_path):
    """Check 2, import-aliased form: `from cc_invoke import _machine_local_get
    as get_ml` followed by `get_ml('fleet_env.root')` must be caught the same
    as the bare-name call -- the docstring promises "bare, attribute, or
    import-aliased" coverage, and this proves the aliased case actually
    fires rather than merely being claimed."""
    fixture = tmp_path / "fixture_aliased_key_read.py"
    fixture.write_text(
        "from cc_invoke import _machine_local_get as get_ml\n"
        "\n"
        "def rederive_fleet_env_root():\n"
        "    return get_ml('fleet_env.root')\n",
        encoding="utf-8",
    )

    violations = find_per_repo_fleet_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_aliased_key_read.py")
    assert "bypassing resolve_fleet_env_root" in description


def test_gate_detects_a_planted_fleet_labelled_per_repo_venv_creation(tmp_path):
    """Check 3: a fresh per-repo venv purpose-built for fleet deps, via
    `python -m venv <fleet-labelled-target>`."""
    fixture = tmp_path / "fixture_per_repo_fleet_venv.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def provision_repo_fleet_venv(repo_root):\n"
        "    target = str(repo_root / 'fleet-deps-venv')\n"
        "    return subprocess.run(['python', '-m', 'venv', target])\n",
        encoding="utf-8",
    )

    violations = find_per_repo_fleet_dependency_resolutions((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, description = violations[0]
    assert relpath.endswith("fixture_per_repo_fleet_venv.py")
    assert "per-repo venv creation" in description


def test_gate_ignores_unrelated_venv_and_dot_venv_references(tmp_path):
    """Negative control: the repo has many legitimate, unrelated `.venv`
    references (exclude-dir markers, a per-plugin venv, this repo's own
    whoami test venv) -- none say "fleet" and none should trip this
    guard, which is scoped to fleet-dependency resolution only."""
    fixture = tmp_path / "fixture_unrelated_venv.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "_EXCLUDE_DIRS = {'.venv', 'node_modules'}\n"
        "\n"
        "def provision_whoami_venv(root):\n"
        "    venv = root / '.venv'\n"
        "    return subprocess.run(['python', '-m', 'venv', str(venv)])\n",
        encoding="utf-8",
    )

    violations = find_per_repo_fleet_dependency_resolutions((tmp_path,))

    assert violations == []


def test_gate_ignores_docstring_mentions_and_uv_sync_without_fleet_token(tmp_path):
    """Negative control: a docstring citing `.fleet-env`/`fleet_env.root`
    for context only, and the real provisioner's own `uv sync` shape (not
    `uv venv`, no fleet-labelled venv-creation call) must not trip the
    gate."""
    fixture = tmp_path / "fixture_benign.py"
    fixture.write_text(
        '"""References <settings-home>/.fleet-env and the fleet_env.root '
        'key for context only."""\n'
        "import subprocess\n"
        "\n"
        "def provision_shared_env(project_dir, build_dir):\n"
        "    return subprocess.run(\n"
        "        ['uv', 'sync', '--frozen', '--project', str(project_dir)],\n"
        "    )\n",
        encoding="utf-8",
    )

    violations = find_per_repo_fleet_dependency_resolutions((tmp_path,))

    assert violations == []


def test_allowlist_entries_are_enumerated_with_a_reason():
    """A carve-out is membership in the explicit list, never a bare path --
    every entry must carry a non-empty reason string."""
    assert _ALLOWLISTED_RELPATHS, "allowlist must not be empty by construction of the guard"
    for relpath, reason in _ALLOWLISTED_RELPATHS.items():
        assert reason and reason.strip(), f"allowlist entry {relpath!r} has no stated reason"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
