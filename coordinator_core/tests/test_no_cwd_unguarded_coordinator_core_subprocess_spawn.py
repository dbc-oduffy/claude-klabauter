"""Standing tripwire (test-only): every test-suite subprocess spawn whose child
must import `coordinator_core` pins its resolution -- via `cwd=`, an `env=`
that carries `PYTHONPATH`, or a `sys.path.insert` inside its own `-c` payload
-- rather than relying on the invoking shell's cwd.

Context: commits 69f20f52 and 75b51038 fixed 28 tests across 10 files that
shared one defect. A test spawns a real subprocess whose argv either runs
`-m coordinator_core...` or a `-c "import coordinator_core..."` payload. That
child inherits the parent's cwd but NOT pytest's rootdir `sys.path`
insertion, so it resolves the `coordinator_core` package only when cwd is (or
is under) the repo root. Run pytest by absolute path from anywhere else --
exactly what a sibling repo's session does -- and the child dies with
`ModuleNotFoundError: No module named 'coordinator_core'` before writing
anything to stdout. The test then verifies the author's working directory
instead of the property under test, and it is invisible to anyone running
pytest from the repo root, which is where the class was written and where it
stayed hidden. See `git show 69f20f52` / `git show 75b51038` for the fix
shape this gate enforces:

    _REPO_ROOT = Path(__file__).resolve().parents[N]
    ...
    subprocess.run([...], ..., cwd=_REPO_ROOT)

That class was swept, not prevented. This gate is the prevention: it fails
the fast tier the moment a new spawn reintroduces the same unpinned shape.

Modeled directly on `coordinator_core/frontmatter/tests/test_no_node_schema_
shellout.py` (structure, ast-over-regex rationale, named exemption set kept
empty by construction, self-tests proving the gate actually detects rather
than passing by absence). Homed in `coordinator_core/tests/` rather than
alongside that precedent because the defect this gate polices is not
frontmatter-specific -- it spans all three collectable test trees
(`coordinator_core`, `coordinator/tests`, `coordinator/bin`), and
`coordinator_core/tests/` is this repo's existing home for other
cross-cutting, non-domain-scoped standing checks (e.g.
`test_pickup_assemble_import_perf.py`, `test_hooks_bookkeeping.py`).

Detection, implemented via `ast` (not regex) for the same reason the
precedent gives -- a regex balance-scan over nested parens on this class
produced ~15 candidates of which only 9 were real when this gate was built;
`ast` gets exact keyword-argument shape and never miscounts:

    For every subprocess spawn call (`subprocess.run`/`Popen`/`check_output`/
    `check_call`, attribute or bare-imported form) inside a `test_*.py` file
    under one of the three scan roots, if the call's argv would cause the
    child to import `coordinator_core` as a package (a literal `-m` argv
    pair targeting it, or a `-c`/shell string payload containing
    `import coordinator_core` / `from coordinator_core` / `-m
    coordinator_core`), the call MUST also either:
      (a) pass a `cwd=` keyword (any value -- presence is what "pins cwd"
          means for this gate, matching the fix shape above), or
      (b) pass an `env=` keyword AND the file sets `PYTHONPATH` somewhere
          (a `dict[...] = ...` assignment or a dict literal keyed
          `"PYTHONPATH"`, anywhere in the file -- see heuristic note below),
          or
      (c) have its `-c`/shell payload perform its own `sys.path.insert`.

Heuristic blind spots -- named explicitly per dispatch instructions, and
deliberately biased toward a false NEGATIVE (a missed unguarded spawn) over
a false POSITIVE (blocking a legitimate, already-guarded test), because a
tripwire that cries wolf gets disabled:

  - The "argv would import coordinator_core" check only sees LITERAL string
    constants in the call's positional list/tuple argv or its `args=`
    keyword. A dynamically built argv entry (an f-string, a variable, a
    comprehension) is invisible to this scanner and will never be flagged --
    exactly the same tradeoff the precedent gate makes for the same reason.
  - The `env=`-carries-PYTHONPATH check (b) is FILE-LEVEL, not call-site
    tight: it asks "does this file set a `PYTHONPATH` key anywhere at all",
    not "does the specific `env` value passed to THIS call carry it". The
    real codebase's dominant pattern is a `_make_env()`/`_invoke()` style
    helper that builds `env["PYTHONPATH"] = ...` in one function and passes
    `env=env` to `subprocess.run` in another (see
    `coordinator_core/tests/test_invoke_main.py`, `test_strategic_emit.py`,
    `test_strategic_generate.py`, and siblings) -- resolving that precisely
    would require whole-program data-flow analysis this gate does not
    attempt. The coarser file-level heuristic can therefore be fooled by a
    file that sets PYTHONPATH for one call but passes `env=` unguarded on an
    unrelated call; accepted per the false-negative-over-false-positive
    preference stated above, and named here rather than silently widened.
  - `cwd=` presence, not its VALUE, satisfies leg (a). A call that passes
    `cwd=` pointed somewhere other than the repo root would not actually be
    guaranteed to resolve `coordinator_core`, but per the fix shape this
    gate enforces (`cwd=_REPO_ROOT` derived from `__file__`), presence is
    the signal every landed fix actually uses, and re-deriving "is this
    cwd value actually the repo root" would mean evaluating arbitrary
    expressions this scanner does not execute.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_ROOTS = (
    _REPO_ROOT / "coordinator_core",
    _REPO_ROOT / "coordinator" / "tests",
    _REPO_ROOT / "coordinator" / "bin",
)

_SPAWN_CALL_NAMES = {"run", "Popen", "check_output", "call", "check_call"}

# Known, LIVE, outstanding exemptions -- keyed on (relpath, lineno). Empty by
# construction: every real site this gate would otherwise flag was already
# fixed by 69f20f52/75b51038. A live entry here means the sweep missed a
# site; per this gate's own spec, that is a reason to report and fix the
# scanner or the site, not to populate this set.
_EXEMPT_CALL_SITES: set[tuple[str, int]] = set()


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        # root is outside the repo (a tmp_path self-test fixture) -- fall
        # back to a root-relative path, which can never collide with a real
        # repo-relative _EXEMPT_CALL_SITES entry.
        return path.resolve().relative_to(root.resolve()).as_posix()


def _spawn_call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _string_constants(node: ast.expr) -> list[str]:
    """Literal string constants reachable from one call argument.

    Handles the list/tuple argv form (`["python3", "-m", "coordinator_core.x"]`)
    and the bare string form (a `shell=True` command line). Anything else (a
    Name, an f-string, a comprehension) is not a literal and is skipped --
    see the module docstring's heuristic-blind-spots note.
    """
    strings: list[str] = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                strings.append(elt.value)
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        strings.append(node.value)
    return strings


def _call_argument_strings(call: ast.Call) -> list[str]:
    strings: list[str] = []
    for arg in call.args:
        strings.extend(_string_constants(arg))
    for kw in call.keywords:
        if kw.arg == "args" and kw.value is not None:
            strings.extend(_string_constants(kw.value))
    return strings


def _argv_imports_coordinator_core(strings: list[str]) -> bool:
    """True when the collected argv/`-c`-payload strings would cause the
    spawned child to import the `coordinator_core` package -- either a `-m
    coordinator_core...` module invocation (as two separate argv entries, or
    inline in one shell string) or a `-c`/shell payload containing an
    `import coordinator_core` / `from coordinator_core` statement.
    """
    for s in strings:
        if "import coordinator_core" in s or "from coordinator_core" in s:
            return True
        if "-m coordinator_core" in s or s.startswith("-mcoordinator_core"):
            return True
    if any(s == "-m" for s in strings):
        for s in strings:
            if s == "coordinator_core" or s.startswith("coordinator_core."):
                return True
    return False


def _file_sets_pythonpath(tree: ast.Module) -> bool:
    """True when the file assigns a `"PYTHONPATH"`-keyed subscript
    (`env["PYTHONPATH"] = ...`) or builds a dict literal keyed
    `"PYTHONPATH"` anywhere. File-level, not call-site-tight -- see the
    module docstring's heuristic-blind-spots note.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "PYTHONPATH"
                ):
                    return True
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "PYTHONPATH":
                    return True
    return False


def _call_guarantees_resolution(call: ast.Call, strings: list[str], file_sets_pythonpath: bool) -> bool:
    kwarg_names = {kw.arg for kw in call.keywords if kw.arg is not None}
    if "cwd" in kwarg_names:
        return True
    if any("sys.path.insert" in s for s in strings):
        return True
    if "env" in kwarg_names and file_sets_pythonpath:
        return True
    return False


class _CwdUnguardedSpawnVisitor(ast.NodeVisitor):
    """Collects (lineno, argv-strings) for every coordinator_core-importing
    subprocess spawn call in one parsed module that does not guarantee its
    own resolution."""

    def __init__(self, file_sets_pythonpath: bool) -> None:
        self._file_sets_pythonpath = file_sets_pythonpath
        self.violations: list[tuple[int, list[str]]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _spawn_call_name(node.func)
        if name in _SPAWN_CALL_NAMES:
            strings = _call_argument_strings(node)
            if _argv_imports_coordinator_core(strings) and not _call_guarantees_resolution(
                node, strings, self._file_sets_pythonpath
            ):
                self.violations.append((node.lineno, strings))
        self.generic_visit(node)


def find_cwd_unguarded_spawns(roots: tuple[Path, ...]) -> list[tuple[str, int, list[str]]]:
    """Walk each root for `test_*.py` files and return every (relpath,
    lineno, argv-strings) tuple that is a coordinator_core-importing
    subprocess spawn without a guaranteed-resolution guard, skipping
    exempted (relpath, lineno) pairs.

    Used both against the real repo test trees (the standing gate) and
    against an isolated tmp_path fixture (the gate's own self-tests, proving
    it actually detects rather than merely passing by absence).
    """
    violations: list[tuple[str, int, list[str]]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError:
                continue
            relpath = _relpath(path, root)
            file_sets_pythonpath = _file_sets_pythonpath(tree)
            visitor = _CwdUnguardedSpawnVisitor(file_sets_pythonpath)
            visitor.visit(tree)
            for lineno, strings in visitor.violations:
                if (relpath, lineno) in _EXEMPT_CALL_SITES:
                    continue
                violations.append((relpath, lineno, strings))
    return violations


def _format_violation(relpath: str, lineno: int, strings: list[str]) -> str:
    return (
        f"{relpath}:{lineno} -- this subprocess spawn's child must import "
        f"coordinator_core (argv: {strings!r}) but the call pins neither "
        f"cwd= nor a PYTHONPATH-carrying env=, so it will verify the "
        f"author's working directory instead of the property under test: "
        f"from any cwd other than the repo root the child dies with "
        f"ModuleNotFoundError before writing anything to stdout. Fix "
        f"(worked example: commit 69f20f52):\n"
        f"    _REPO_ROOT = Path(__file__).resolve().parents[N]  # N = repo-root depth from this file\n"
        f"    ...\n"
        f"    subprocess.run([...], ..., cwd=_REPO_ROOT)"
    )


def test_no_cwd_unguarded_coordinator_core_subprocess_spawn():
    """Standing gate: no test-suite subprocess spawn whose child must import
    `coordinator_core` may omit a guaranteed-resolution guard (`cwd=`, a
    PYTHONPATH-carrying `env=`, or its own `sys.path.insert`), except the
    explicit, narrow, dated exemption(s) in _EXEMPT_CALL_SITES -- empty by
    construction, since 69f20f52/75b51038 already fixed every known site.
    """
    violations = find_cwd_unguarded_spawns(_SCAN_ROOTS)
    assert violations == [], "\n\n".join(
        _format_violation(relpath, lineno, strings) for relpath, lineno, strings in violations
    )


def test_gate_detects_a_planted_cwd_unguarded_dash_m_spawn(tmp_path):
    """Proves the gate has teeth for the `-m coordinator_core...` shape:
    without this test, the gate's own soundness would be passing-by-absence
    against the real tree, and would not catch a future regression."""
    fixture = tmp_path / "test_fixture_dash_m_unguarded.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def test_something():\n"
        "    proc = subprocess.run(\n"
        "        [sys.executable, \"-m\", \"coordinator_core.invoke\", \"ping\", \"{}\"],\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "    )\n"
        "    assert proc.returncode == 0\n",
        encoding="utf-8",
    )

    violations = find_cwd_unguarded_spawns((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, strings = violations[0]
    assert relpath.endswith("test_fixture_dash_m_unguarded.py")
    assert lineno == 5
    assert "-m" in strings
    assert "coordinator_core.invoke" in strings


def test_gate_detects_a_planted_cwd_unguarded_dash_c_spawn(tmp_path):
    """Proves the gate has teeth for the `-c "import coordinator_core..."`
    shape, the other pattern the fixed commits covered."""
    fixture = tmp_path / "test_fixture_dash_c_unguarded.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def test_cold_import():\n"
        "    proc = subprocess.run(\n"
        "        [sys.executable, \"-c\", \"import coordinator_core.machine_resolver\"],\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "    )\n"
        "    assert proc.returncode == 0\n",
        encoding="utf-8",
    )

    violations = find_cwd_unguarded_spawns((tmp_path,))

    assert len(violations) == 1
    relpath, lineno, strings = violations[0]
    assert relpath.endswith("test_fixture_dash_c_unguarded.py")
    assert "import coordinator_core.machine_resolver" in strings


def test_gate_ignores_a_cwd_guarded_spawn(tmp_path):
    """Negative control: the exact fixed shape (cwd= pinned) must not be
    flagged."""
    fixture = tmp_path / "test_fixture_cwd_guarded.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "_REPO_ROOT = Path(__file__).resolve().parents[0]\n"
        "\n"
        "def test_something():\n"
        "    proc = subprocess.run(\n"
        "        [sys.executable, \"-m\", \"coordinator_core.invoke\", \"ping\", \"{}\"],\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "        cwd=_REPO_ROOT,\n"
        "    )\n"
        "    assert proc.returncode == 0\n",
        encoding="utf-8",
    )

    violations = find_cwd_unguarded_spawns((tmp_path,))

    assert violations == []


def test_gate_ignores_an_env_pythonpath_guarded_spawn(tmp_path):
    """Negative control: the `env=` carries `PYTHONPATH` (the
    `_make_env()`/`_invoke()` helper pattern seen throughout
    coordinator_core/tests/) so no `cwd=` is required."""
    fixture = tmp_path / "test_fixture_env_pythonpath_guarded.py"
    fixture.write_text(
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def _make_env():\n"
        "    env = os.environ.copy()\n"
        "    env[\"PYTHONPATH\"] = \"/some/repo/root\"\n"
        "    return env\n"
        "\n"
        "def test_something():\n"
        "    proc = subprocess.run(\n"
        "        [sys.executable, \"-m\", \"coordinator_core.invoke\", \"ping\", \"{}\"],\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "        env=_make_env(),\n"
        "    )\n"
        "    assert proc.returncode == 0\n",
        encoding="utf-8",
    )

    violations = find_cwd_unguarded_spawns((tmp_path,))

    assert violations == []


def test_gate_ignores_spawns_unrelated_to_coordinator_core(tmp_path):
    """Negative control: a subprocess spawn (e.g. `git`) that never imports
    coordinator_core must not be flagged even with no cwd/env guard at all."""
    fixture = tmp_path / "test_fixture_unrelated_spawn.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def test_something():\n"
        "    proc = subprocess.run([\"git\", \"status\"], capture_output=True, text=True)\n"
        "    assert proc.returncode in (0, 128)\n",
        encoding="utf-8",
    )

    violations = find_cwd_unguarded_spawns((tmp_path,))

    assert violations == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
