"""Guards the origin baton: every benchmark driver under
coordinator_core/benchmarks/ that spawns `invoke` traffic must declare that
traffic's origin, and no module lacking a driver entry may declare it either.

THE DEFECT THIS EXISTS FOR. `declare_benchmark_origin()` (see
`coordinator_core/benchmarks/__init__.py`) is how the op-latency sink tells
benchmark traffic apart from production traffic. Before this guard, "every
driver declares" was a convention enforced by 22 individual fixes and nothing
else -- a new driver that forgot the call silently polluted the production
op-latency census, with no failure anywhere to say so. See
`state/handoffs/2026-08-27-the-origin-field-cannot-see-an-undeclared-harness.md`.

PREDICATE, FAIL-CLOSED. The subject set is every module under
coordinator_core/benchmarks/ (excluding this tests/ package) that carries a
module-level `if __name__ == "__main__":` block, MINUS an explicit
allowlist (`NON_SPAWNING_MAIN_ALLOWLIST`) of modules that carry a `__main__`
entry but never spawn -- each allowlist entry needs a one-line reason. A new
driver is a subject by default; exempting it is a visible, reviewable edit.

This is deliberately NOT enumerated by spawn mechanism (raw
subprocess.run/Popen, time_invocation, batched_process_time_ms /
single_invocation_tree_process_time, cc_invoke): a driver spawning by a sixth,
unenumerated mechanism would silently leave that subject set and the guard
would stay green with no signal. Spawn-mechanism detection is not used as the
predicate here at all.

THIRD CLASS -- library spawn helpers. `LIBRARY_SPAWN_HELPERS` names modules
that carry a spawn shape but no `__main__` entry of their own -- callers
invoke their functions FROM a driver's own entry, so these modules must never
call `declare_benchmark_origin()` themselves (the driver that calls into them
does, once, at its own entry). They are neither subjects (no entry to declare
from) nor exempt from the inverse rule below.

INVERSE RULE. No module lacking a `__main__` entry may call
`declare_benchmark_origin()` anywhere in its body -- module level or inside a
function body -- EXCEPT the three named sites in
`INVERSE_RULE_FUNCTION_CARVEOUT` below. `floor.py::measure_floor` carried
exactly this shape (a library-function-body call, not a module-level one)
until C2 fixed it; the guard's own detection machinery is proved today by a
synthetic fixture (`test_inverse_rule_fixture_catches_library_body_declare`)
rather than that now-fixed live instance. A guard checking only "at module
level" would have passed floor.py and missed that violation -- the one
originally cited as the reason this leg exists at all.

CARVE-OUT, NOW EMPTY (opened by chunk C1c, closed by chunk C1d).
`INVERSE_RULE_FUNCTION_CARVEOUT` briefly exempted three named library
functions -- `timer.py::time_invocation`,
`process_time.py::batched_process_time_ms`, and
`process_time.py::single_invocation_tree_process_time` -- which C1b had
declaring from their own body. That declaration mutated the interpreter-global
`os.environ` and never restored it, mislabelling the caller's whole process
and everything it later spawned; C1d replaced it with a child-scoped env
passed to the subprocess. The set is now EMPTY and still consulted, so
re-introducing a process-global declare at those sites turns this guard red
instead of passing on a stale exemption. It is keyed by (module filename,
function name) pairs and must never become a module-wide or category-wide
exemption -- "library helpers may declare" is fail-open and re-opens the hole
this rule exists to close, which is exactly the shape of floor.py's own former
violation. Every library-body `declare_benchmark_origin()` call, anywhere,
stays red.

REACHABILITY, NOT PRESENCE. A subject passes only when `declare_benchmark_origin()`
is reached from its `__main__` block (following same-module calls) and no
detectable spawn call precedes it. A declare in a helper nothing calls, or after
the first spawn, stamps nothing. Ceiling: branches are read linearly, and a spawn
hidden behind an imported library helper is invisible to the ordering check.

WIDENED SUBJECT SET. Every `__main__`-bearing module under coordinator_core/,
coordinator/bin/, bin/ and scripts/ that spawns the invoke CLI by a raw route
(a spawn call plus a `coordinator_core.invoke` argv literal) is a subject too.
Modules going through `coordinator/bin/lib/cc_invoke.py` are the sanctioned
production route and are out of the set. `PRODUCTION_INVOKE_SPAWNERS` carries
the adjudicated exemptions, one reason each:
  - bin/claude-klabauter-doctor-probe.py: health probes; the ping is real production use.
  - bin/claude-klabauter-commit-anchors.py: commit-anchor tool; production traffic.
  - coordinator/bin/survey-consume-gate.py: survey gate; production traffic.
  - coordinator_core/ops/setup_chain_walker.py: setup chain walker; production traffic.
Every `__main__`-bearing module under benchmarks/ passes the reachability model unchanged.

POPULATION IS GREEN (chunk C1c, after C2). Every subject/inverse case in the
current population passes with the allowlist, carve-out, and third-class set
below applied -- the parametrized subject/inverse tests carry no
`designed_red` marker for that reason: a guard whose population is compliant
must sit in the tier that enforces it, not the tier that merely worklists it.

Spec backlink: state/handoffs/2026-08-27-the-origin-field-cannot-see-an-undeclared-harness.md
"""

from __future__ import annotations

import ast
import pathlib

import pytest

BENCHMARKS_DIR = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR_NAME = "tests"
DECLARE_ORIGIN_NAME = "declare_benchmark_origin"

# __main__-bearing modules exempt from the declare_benchmark_origin()
# requirement, each with a one-line reason for why it genuinely never spawns
# invoke traffic. Verified individually against each module's own body
# (chunk C1c) -- a module that does spawn invoke traffic gets a declare,
# never an allowlist entry. See test_non_spawning_main_allowlist_entries_carry_no_spawn_shape
# below for the anti-drift check that keeps this list honest.
NON_SPAWNING_MAIN_ALLOWLIST: dict[str, str] = {
    "_import_probe.py": "fresh-interpreter import-cost probe; measures a sys.modules delta around __import__() in-process, never spawns invoke traffic itself.",
    "_read_events_probe.py": "one-shot spawn target that calls tracker_store.read_events() directly; a plain library call, not invoke traffic, and spawns nothing of its own.",
    "_render_status_probe.py": "one-shot spawn target that calls tracker_projection.render_status() directly; a plain library call, not invoke traffic, and spawns nothing of its own.",
    "ambient_sampler.py": "ambient-load snapshotter; gathers process/CPU/RAM via in-process psutil calls only (module docstring: 'Windows is first-class... never by shelling out'), never subprocess.run/Popen.",
    "listener_availability.py": "HTTP-only listener-health sampler; module docstring's own negative-spec is 'never ensure_listener, never the C door, never POST /hook' -- GET /health only, no subprocess of its own.",
    "shim_prototype_dispatcher.py": "throwaway C7 prototype's second hop; imports and calls plan_assemble.main(argv) in-process, spawns nothing itself (it IS what a forwarder spawns, not a spawner).",
    "shim_prototype_inprocess.py": "throwaway C7 prototype; runs its target via runpy.run_path in-process (mirrors exec_cli's Windows leg), spawns no child process at all.",
}

# Library spawn helpers: carry a spawn shape, no __main__ entry of their own.
# Neither subjects of the forward rule nor exempt from the inverse rule --
# they must never call declare_benchmark_origin() themselves.
LIBRARY_SPAWN_HELPERS = frozenset(
    {
        "bash_dispatch_probe.py",
        "floor.py",
        "hook_entry_cost.py",
        "import_budget.py",
        "leaf_spawn_migration_verify.py",
        "op_fixtures.py",
        "process_time.py",
    }
)

# Chunk C1c (b) opened this carve-out for C1b's three runtime-leg sites;
# chunk C1d CLOSED it, and it is deliberately EMPTY rather than deleted.
#
# C1b had those three functions call declare_benchmark_origin() from their own
# body. That call writes ORIGIN_ENV into the interpreter-global os.environ and
# never restores it, so calling one stamped the CALLER'S WHOLE PROCESS -- and
# every later subprocess it spawned -- as benchmark. The repo's own env-leak
# fixture caught it via test_timer.py. C1d fixed it at the mechanism: the three
# helpers now build a child-scoped env dict and pass it as the subprocess
# `env=`, so the tag reaches the process that writes the sink row and stops
# there. No library body declares any more.
#
# The set stays here, empty and still consulted, because an empty exemption is
# the point: re-introducing the process-global write at any of those three
# sites must turn this guard RED, not pass on a carve-out left behind after the
# calls it licensed were removed. Re-populate it only for a call that is again
# genuinely process-global by design, and say why on the entry.
INVERSE_RULE_FUNCTION_CARVEOUT: frozenset[tuple[str, str]] = frozenset()

# Spawn-mechanism call shapes checked ONLY by the allowlist anti-drift
# assertion below -- the core subject/inverse predicate above deliberately
# does NOT enumerate spawn mechanisms (module docstring). This narrower
# enumeration exists solely so an allowlisted module that later grows one of
# these calls turns red instead of sitting exempt forever.
#
# `subprocess.*`/`os.*` entries are QUALIFIED (module.attr), not bare names --
# a bare "system"/"run"/"call" collides with unrelated stdlib calls (e.g.
# `platform.system()`, seen as a false positive here during this chunk's own
# verification). The three benchmark timing primitives are bare names
# because they are this package's own unambiguous, always-imported-by-name
# spawn helpers.
_SPAWN_QUALIFIED_CALLS = frozenset(
    {
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "call"),
        ("subprocess", "check_call"),
        ("subprocess", "check_output"),
        ("os", "system"),
        ("os", "posix_spawn"),
        ("os", "posix_spawnp"),
    }
)
_SPAWN_BARE_NAMES = frozenset(
    {
        "time_invocation",
        "batched_process_time_ms",
        "single_invocation_tree_process_time",
    }
)

# A bare-name
# import (`from subprocess import run`) then a bare `run(...)` call evaded
# both branches above (no qualifying `.id` in _SPAWN_BARE_NAMES, no
# `subprocess.` attribute access), letting an allowlisted module grow live
# spawn traffic while staying exempt forever. Fix flags the IMPORT BINDING
# itself, not a bare call-site name -- keeps the false-positive reasoning
# above (`platform.system()`) intact, since this never inspects call sites
# for these names at all.
_SPAWN_IMPORTABLE_NAMES: dict[str, frozenset[str]] = {
    "subprocess": frozenset({"run", "Popen", "call", "check_call", "check_output"}),
    "os": frozenset({"system", "posix_spawn", "posix_spawnp"}),
}


def _iter_benchmark_module_paths() -> list[pathlib.Path]:
    """All .py modules under coordinator_core/benchmarks/, excluding tests/."""
    out = []
    for path in sorted(BENCHMARKS_DIR.rglob("*.py")):
        rel = path.relative_to(BENCHMARKS_DIR)
        if rel.parts[0] == TESTS_DIR_NAME:
            continue
        if path == BENCHMARKS_DIR / "__init__.py":
            # Defines declare_benchmark_origin() itself; not a driver, not a
            # subject, not a library-spawn-helper -- excluded from both sets.
            continue
        out.append(path)
    return out


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def module_has_main_entry(tree: ast.Module) -> bool:
    """True if `tree` carries a module-level `if __name__ == "__main__":`,
    in either comparison order (`__name__ == "__main__"` or the reversed
    `"__main__" == __name__`; Review: coordinatorcode-reviewer.a9ee6eb5c87c4b7d7
    Finding 4 -- only the canonical ordering was recognized before)."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
        ):
            continue
        left, right = test.left, test.comparators[0]
        canonical = (
            isinstance(left, ast.Name)
            and left.id == "__name__"
            and isinstance(right, ast.Constant)
            and right.value == "__main__"
        )
        reversed_order = (
            isinstance(left, ast.Constant)
            and left.value == "__main__"
            and isinstance(right, ast.Name)
            and right.id == "__name__"
        )
        if canonical or reversed_order:
            return True
    return False


# Matching the
# literal string "declare_benchmark_origin" let an aliased import
# (`from ... import declare_benchmark_origin as d` then `d()`) evade both the
# forward and inverse call-site checks. Resolve the local bound name(s) per
# module first and match calls against those, not the literal import name.
# getattr(...)-indirection is deliberate evasion and stays outside this
# fix's ceiling -- see module docstring note near DECLARE_ORIGIN_NAME.
def _declare_origin_local_names(tree: ast.Module) -> frozenset[str]:
    names = {DECLARE_ORIGIN_NAME}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name == DECLARE_ORIGIN_NAME:
                names.add(alias.asname or alias.name)
    return frozenset(names)


def _spawn_local_names(tree: ast.Module) -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _SPAWN_IMPORTABLE_NAMES:
            wanted = _SPAWN_IMPORTABLE_NAMES[node.module]
            names.update(a.asname or a.name for a in node.names if a.name in wanted)
    return frozenset(names)


class _EntryEvents(ast.NodeVisitor):
    """Ordered "declare"/"spawn" events reached from a module's `__main__` block,
    following calls to same-module top-level functions (arguments evaluate before
    the call; nested defs, lambdas and classes are not entered)."""

    def __init__(self, tree: ast.Module) -> None:
        self._funcs = {
            n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self._declare_names = _declare_origin_local_names(tree)
        self._spawn_names = _spawn_local_names(tree)
        self._active: set[str] = set()
        self.events: list[str] = []

    def visit_FunctionDef(self, node: ast.AST) -> None:
        return None

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        func = node.func
        if (isinstance(func, ast.Name) and func.id in self._declare_names) or (
            isinstance(func, ast.Attribute) and func.attr == DECLARE_ORIGIN_NAME
        ):
            self.events.append("declare")
        elif (
            (isinstance(func, ast.Name) and (func.id in _SPAWN_BARE_NAMES or func.id in self._spawn_names))
            or (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in _SPAWN_QUALIFIED_CALLS
            )
        ):
            self.events.append("spawn")
        elif isinstance(func, ast.Name) and func.id in self._funcs and func.id not in self._active:
            self._active.add(func.id)
            for stmt in self._funcs[func.id].body:
                self.visit(stmt)
            self._active.discard(func.id)

    def run(self, tree: ast.Module) -> list[str]:
        for node in tree.body:
            if isinstance(node, ast.If) and _is_main_guard(node):
                for stmt in node.body:
                    self.visit(stmt)
        return self.events


def _is_main_guard(node: ast.If) -> bool:
    return module_has_main_entry(ast.Module(body=[node], type_ignores=[]))


def module_calls_declare_origin(tree: ast.Module) -> bool:
    """True if `declare_benchmark_origin()` is reached from the module's `__main__`
    block (same-module calls followed) before any detectable spawn call."""
    events = _EntryEvents(tree).run(tree)
    return "declare" in events and ("spawn" not in events or events.index("declare") < events.index("spawn"))


class _DeclareOriginCallSites(ast.NodeVisitor):
    """Records every declare_benchmark_origin() call site in a module, each
    tagged with the name of its innermost enclosing function (None for a
    module-level call) -- what INVERSE_RULE_FUNCTION_CARVEOUT is checked
    against, since the carve-out is by function name, never by module."""

    def __init__(self, local_names: frozenset[str]) -> None:
        self._stack: list[str] = []
        self._local_names = local_names
        self.sites: list[str | None] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_declare = (isinstance(func, ast.Name) and func.id in self._local_names) or (
            isinstance(func, ast.Attribute) and func.attr == DECLARE_ORIGIN_NAME
        )
        if is_declare:
            self.sites.append(self._stack[-1] if self._stack else None)
        self.generic_visit(node)


def _declare_origin_call_sites(tree: ast.Module) -> list:
    visitor = _DeclareOriginCallSites(_declare_origin_local_names(tree))
    visitor.visit(tree)
    return visitor.sites


def _module_has_spawn_shape(tree: ast.Module) -> bool:
    """True if `tree` contains a call whose name matches a known
    invoke-spawn mechanism (subprocess.run/Popen/etc, this package's own
    timing primitives, os.system, posix_spawn). Anti-drift check for
    NON_SPAWNING_MAIN_ALLOWLIST only (module docstring (a)): an allowlisted
    module that later grows a spawn-shaped call must turn this guard red
    instead of sitting exempt forever."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _SPAWN_IMPORTABLE_NAMES:
            wanted = _SPAWN_IMPORTABLE_NAMES[node.module]
            if any(alias.name in wanted for alias in node.names):
                return True
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _SPAWN_BARE_NAMES:
            return True
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (func.value.id, func.attr) in _SPAWN_QUALIFIED_CALLS
        ):
            return True
    return False


def _subject_paths() -> list[pathlib.Path]:
    subjects = []
    for path in _iter_benchmark_module_paths():
        if path.name in NON_SPAWNING_MAIN_ALLOWLIST:
            continue
        if module_has_main_entry(_parse(path)):
            subjects.append(path)
    return subjects


PRODUCTION_INVOKE_SPAWNERS: dict[str, str] = {
    "bin/claude-klabauter-doctor-probe.py": "health probes; the cold ping is real production use, not benchmark traffic.",
    "bin/claude-klabauter-commit-anchors.py": "commit-anchor tool; its invoke calls are production traffic.",
    "coordinator/bin/survey-consume-gate.py": "survey consume gate; its invoke calls are production traffic.",
    "coordinator_core/ops/setup_chain_walker.py": "setup chain walker; its invoke calls are production traffic.",
}

_WIDENED_ROOTS = ("coordinator_core", "coordinator/bin", "bin", "scripts")
_INVOKE_ARGV_LITERAL = "coordinator_core.invoke"
_REPO_ROOT = BENCHMARKS_DIR.parents[1]


def _names_invoke_cli(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith(_INVOKE_ARGV_LITERAL) and len(n.value) < 80
        for n in ast.walk(tree)
    )


def _widened_subject_paths(repo_root: pathlib.Path = _REPO_ROOT) -> list[pathlib.Path]:
    """`__main__`-bearing modules outside the benchmarks package that spawn the
    invoke CLI by a raw route, minus PRODUCTION_INVOKE_SPAWNERS."""
    subjects = []
    for root in _WIDENED_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(repo_root).as_posix()
            parts = path.relative_to(base).parts
            if not path.is_file() or "__pycache__" in parts or "tests" in parts or "fixtures" in parts:
                continue
            if rel.startswith("coordinator_core/benchmarks/") or rel in PRODUCTION_INVOKE_SPAWNERS:
                continue
            if path.name.startswith("test_") or not (path.suffix == ".py" or (path.suffix == "" and root == "coordinator/bin")):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _INVOKE_ARGV_LITERAL not in text:
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            if module_has_main_entry(tree) and _module_has_spawn_shape(tree) and _names_invoke_cli(tree):
                subjects.append(path)
    return subjects


@pytest.mark.parametrize("path", _widened_subject_paths(), ids=lambda p: p.relative_to(_REPO_ROOT).as_posix())
def test_every_raw_invoke_spawner_outside_benchmarks_declares_origin(path: pathlib.Path) -> None:
    assert module_calls_declare_origin(_parse(path)), (
        f"{path.relative_to(_REPO_ROOT).as_posix()} spawns the invoke CLI from its __main__ without "
        f"declaring origin ahead of the spawn -- declare it, or add it to PRODUCTION_INVOKE_SPAWNERS "
        f"with a one-line reason if its traffic is production use."
    )


def test_production_invoke_spawner_entries_are_still_subjects_by_shape() -> None:
    """An exemption must stay earned: each entry still spawns the invoke CLI from a __main__."""
    for rel in PRODUCTION_INVOKE_SPAWNERS:
        tree = _parse(_REPO_ROOT / rel)
        assert module_has_main_entry(tree) and _module_has_spawn_shape(tree) and _names_invoke_cli(tree), (
            f"{rel} no longer matches the raw-spawner shape; drop its PRODUCTION_INVOKE_SPAWNERS entry."
        )


def _plant(tmp_path: pathlib.Path, body: str) -> ast.Module:
    f = tmp_path / "planted.py"
    f.write_text(
        "import subprocess\n"
        "from coordinator_core.benchmarks import declare_benchmark_origin\n" + body,
        encoding="utf-8",
    )
    return _parse(f)


def test_declare_in_never_called_helper_is_caught(tmp_path: pathlib.Path) -> None:
    tree = _plant(
        tmp_path,
        "def helper():\n    declare_benchmark_origin()\n"
        "def main():\n    subprocess.run(['x'])\n"
        "if __name__ == '__main__':\n    main()\n",
    )
    assert not module_calls_declare_origin(tree)


def test_declare_after_the_spawn_is_caught(tmp_path: pathlib.Path) -> None:
    tree = _plant(
        tmp_path,
        "def main():\n    subprocess.run(['x'])\n    declare_benchmark_origin()\n"
        "if __name__ == '__main__':\n    main()\n",
    )
    assert not module_calls_declare_origin(tree)


def test_declare_in_called_helper_before_spawn_passes(tmp_path: pathlib.Path) -> None:
    tree = _plant(
        tmp_path,
        "def setup():\n    declare_benchmark_origin()\n"
        "def main():\n    setup()\n    subprocess.run(['x'])\n"
        "if __name__ == '__main__':\n    main()\n",
    )
    assert module_calls_declare_origin(tree)


def test_raw_spawning_driver_in_bin_is_a_subject_and_fails(tmp_path: pathlib.Path) -> None:
    (tmp_path / "bin").mkdir()
    driver = tmp_path / "bin" / "raw_driver.py"
    driver.write_text(
        "import subprocess\n"
        "if __name__ == '__main__':\n"
        "    subprocess.run(['python', '-m', 'coordinator_core.invoke', 'ping', '{}'])\n",
        encoding="utf-8",
    )
    assert _widened_subject_paths(tmp_path) == [driver]
    assert not module_calls_declare_origin(_parse(driver))


def _non_main_paths() -> list[pathlib.Path]:
    return [path for path in _iter_benchmark_module_paths() if not module_has_main_entry(_parse(path))]


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(BENCHMARKS_DIR)).replace("\\", "/")


@pytest.mark.parametrize("path", _subject_paths(), ids=_rel)
def test_every_main_bearing_module_declares_origin(path: pathlib.Path) -> None:
    tree = _parse(path)
    assert module_calls_declare_origin(tree), (
        f"{_rel(path)} has a __main__ entry and is a benchmark driver but "
        f"never calls declare_benchmark_origin() -- call it from this "
        f"module's own entry, or add {path.name!r} to "
        f"NON_SPAWNING_MAIN_ALLOWLIST with a one-line reason if it never "
        f"spawns invoke traffic."
    )


def test_non_spawning_main_allowlist_entries_carry_no_spawn_shape() -> None:
    """Anti-drift (chunk C1c (a)): an allowlisted module that later grows a
    spawn-shaped call (subprocess.run/Popen/etc, or this package's own
    timing primitives) must turn this guard red -- the allowlist must not
    become inert cover for a module that starts spawning invoke traffic."""
    for path in _iter_benchmark_module_paths():
        if path.name not in NON_SPAWNING_MAIN_ALLOWLIST:
            continue
        tree = _parse(path)
        assert not _module_has_spawn_shape(tree), (
            f"{path.name} is in NON_SPAWNING_MAIN_ALLOWLIST but now carries "
            f"a spawn-shaped call -- remove it from the allowlist and make "
            f"it call declare_benchmark_origin() from its own __main__ "
            f"entry instead."
        )


@pytest.mark.parametrize("path", _non_main_paths(), ids=_rel)
def test_no_main_entry_means_no_declare_call(path: pathlib.Path) -> None:
    tree = _parse(path)
    sites = _declare_origin_call_sites(tree)
    offending = [fn for fn in sites if (path.name, fn) not in INVERSE_RULE_FUNCTION_CARVEOUT]
    assert not offending, (
        f"{_rel(path)} has no __main__ entry but calls "
        f"declare_benchmark_origin() from {offending!r} -- move the call "
        f"into a benchmark driver's own __main__-bearing module; library "
        f"code must never stamp the origin baton itself, except from the "
        f"three C1b-authorized sites named in INVERSE_RULE_FUNCTION_CARVEOUT "
        f"(see declare_benchmark_origin's own negative-spec in "
        f"coordinator_core/benchmarks/__init__.py)."
    )


def test_library_spawn_helpers_are_a_third_class_not_subjects() -> None:
    subject_names = {path.name for path in _subject_paths()}
    for name in LIBRARY_SPAWN_HELPERS:
        assert name not in subject_names, (
            f"{name} carries a spawn shape but no __main__ entry -- it must "
            f"stay out of the subject set (it is a library helper, not a "
            f"driver)."
        )


def test_inverse_rule_fixture_catches_library_body_declare(tmp_path: pathlib.Path) -> None:
    """Proves the inverse rule (test_no_main_entry_means_no_declare_call)
    would catch a library module calling declare_benchmark_origin() from a
    function body, not merely a module-level one -- the exact shape
    `floor.py::measure_floor` carried until C2 fixed it (see this module's
    own docstring's INVERSE RULE section). floor.py::measure_floor WAS the
    original, real-violation fixture case for this test; C2 landed the fix,
    so this synthetic fixture (required by chunk C1's own body) replaces it,
    proving the guard still catches the violation shape rather than merely
    having described one that is now gone."""
    fixture = tmp_path / "library_helper_with_body_declare.py"
    fixture.write_text(
        "from coordinator_core.benchmarks import declare_benchmark_origin\n"
        "\n"
        "\n"
        "def helper() -> None:\n"
        "    declare_benchmark_origin()\n",
        encoding="utf-8",
    )
    tree = _parse(fixture)

    assert not module_has_main_entry(tree), "fixture must stay a library helper (no __main__ entry) for this fixture case to hold."
    sites = _declare_origin_call_sites(tree)
    offending = [fn for fn in sites if (fixture.name, fn) not in INVERSE_RULE_FUNCTION_CARVEOUT]
    assert offending, (
        "fixture library module unexpectedly appears not to call "
        "declare_benchmark_origin() from a function body -- fixture is "
        "broken, not the guard."
    )


def test_fixture_driver_missing_declaration_is_caught(tmp_path: pathlib.Path) -> None:
    """Proves the guard would catch driver 33 -- a brand-new __main__-bearing
    module that spawns invoke traffic and never declares -- not merely
    describe today's violators. Constructs a fixture driver omitting the
    declaration and asserts the same predicate the parametrized subject test
    uses fails on it."""
    fixture = tmp_path / "undeclared_driver.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "\n"
        "def main() -> int:\n"
        "    subprocess.run(['echo', 'hi'])\n"
        "    return 0\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    tree = _parse(fixture)

    assert module_has_main_entry(tree)
    assert not module_calls_declare_origin(tree), (
        "fixture driver unexpectedly appears to declare its origin -- fixture is broken, not the guard."
    )


def test_fixture_driver_with_declaration_passes(tmp_path: pathlib.Path) -> None:
    """Companion to the omission fixture above: a driver that DOES declare
    from its own entry must pass the same predicate, proving the guard is
    not vacuously red."""
    fixture = tmp_path / "declared_driver.py"
    fixture.write_text(
        "from coordinator_core.benchmarks import declare_benchmark_origin\n"
        "\n"
        "\n"
        "def main() -> int:\n"
        "    declare_benchmark_origin()\n"
        "    return 0\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    tree = _parse(fixture)

    assert module_has_main_entry(tree)
    assert module_calls_declare_origin(tree)
