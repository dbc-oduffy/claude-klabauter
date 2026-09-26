"""coordinator_core.tests.test_no_wall_clock_ratchets -- the zero-spawn
guard that refuses a new wall-clock perf ratchet.

Spec backlink: docs/plans/2026-09-11-perf-ratchets-measure-process-time-
not-t.md § C8. Prior art this chunk depends on: C3 moved the known
instance off wall clock, C4 converted or deleted every accidental hit
this guard would otherwise land red against with no baseline, and C5
registered `deliberate_wall_clock` in pyproject.toml's `markers` list.

WHY THIS GUARD EXISTS
    CLAUDE.md's § "The brightline" is explicit: DR-344 gates on PROCESS
    TIME, never wall clock -- wall clock on this box measures peer load
    (50-70 concurrent sessions is the design condition), not cost. A perf
    ratchet written against `time.perf_counter()`/`time.monotonic()`/
    `time.time()` deltas is a gate ANOTHER concurrent session can move,
    which is the same defect DR-344 already named, wearing a new name
    every time a new one is added by hand. This guard is the recurrence
    fence: it fails collection the moment a NEW undischarged one lands,
    the same "the rule enforces itself" shape
    `test_no_new_spawning_tests.py` already established for spawn sites.

WHAT THIS GUARD DETECTS
    Pure AST, zero spawns, fast tier (no `cadence`, no `spawns_process`
    marker on this module itself). A byte-level prefilter for wall-clock
    tokens runs before any file is parsed, so the vast majority of
    testpaths files (no wall-clock call at all) never pay parse cost.

    - wall read: a call resolving to `time.perf_counter`/`_ns`,
      `time.monotonic`/`_ns`, `time.time`/`_ns` -- including
      `from time import perf_counter as X`-style aliases and `import time
      as t`-style module aliases.
    - stamp: a name bound to a wall read.
    - duration: a `Sub` (`-`) expression whose BOTH operands are wall
      reads or stamps. This is deliberately narrower than "any
      subtraction of tainted values" -- it is what keeps ordinary
      timestamp arithmetic (subtracting two unrelated, non-wall-clock
      values) off this guard's radar, and it is why a lone stamp compared
      directly (a deadline check, not an elapsed-time bound) does not
      trip the sink below.
    - taint propagation: through assignment, arithmetic, subscripts,
      `.append`/`.extend`, and `min`/`max`/`sum`/`sorted`/`statistics.*`
      over a tainted collection. A same-module function whose `return` is
      tainted taints its call results too (the "helper-return shape"
      below) -- cross-module helpers are NOT resolved (see NEGATIVE SPEC).
    - sink: an `assert` whose Compare has a tainted operand on the SMALL
      side of `</`<=` (the left operand -- `assert elapsed < BUDGET`) or
      the LARGE side of `>`/`>=` (the right operand -- `assert BUDGET >
      elapsed`). This is deliberately an UPPER-bound-only sink: a lower
      bound (`assert elapsed > MIN_WAIT`, waiting out an eventual-
      consistency window) is not a perf ratchet and is not flagged.

    DISCHARGE: `pytest.mark.deliberate_wall_clock(reason="...")` with a
    non-empty string-literal `reason=`, on the test function itself, its
    enclosing class (decorator OR class-body `pytestmark`), or the module
    `pytestmark`. A BARE marker (no `reason=`, or an empty one) does NOT
    discharge -- it is indistinguishable from "forgot to fill it in."
    There is no allowlist and no baseline: every undischarged hit fails,
    every time, from this guard's first day.

MARKER DETECTION extends `coordinator_core.spawn_policy.marker_check`
    with its additive `marker_call_nodes` sibling (keywords intact,
    same-signatures-as-existing-functions shape) -- see that module's own
    docstring for why the existing functions could not do this
    themselves. This guard is the sole decider of what counts as
    "discharged"; `marker_call_nodes` only surfaces candidate nodes.

NEGATIVE SPEC -- this guard's known blind spots, by design, not oversight
    - Fixture-injected values: a duration handed in through a pytest
      fixture parameter is invisible to this guard's local, per-function
      taint tracking -- it has no assignment or call site inside the test
      function body to taint.
    - Cross-module helpers: "a same-module function whose return is
      tainted taints its call results" is exactly that -- same module
      only. A helper imported from another file is not resolved.
    - Assertions made inside a helper assert-function, or via
      `pytest.approx`: this guard only inspects `ast.Assert` nodes in the
      function it is scanning; an assertion hidden behind
      `_assert_within_budget(elapsed, BUDGET)` or expressed through
      `pytest.approx` is not an `assert ... < ...` Compare shape and is
      not seen.
    - This guard does NOT itself decide whether some other elapsed-time
      use is "a perf ratchet" in spirit -- it decides exactly the AST
      shape above, nothing broader.

Failure message register (docs/wiki/guard-messaging.md § Register): one
fact (file::qualname, line), then the two routes -- measure through
`coordinator_core/benchmarks/process_time.py`, or declare
`deliberate_wall_clock(reason=...)`.

COST (warm-cache, Intel Ultra 9 285K, CPython 3.11+): the spike this
chunk's own body cites measured walk+read close to 296ms and parse+taint
close to 531ms against the ~2932 pre-`norecursedirs` testpaths files, put
the whole-scan bar (walk, read, parse, taint together, not parse alone)
near 830ms against the naïve approach. The two levers this guard takes
to stay under the 500ms whole-scan bar: (1) `_walk_testpaths` prunes
pytest's own default `norecursedirs` (plus dot-directories and `*.egg`)
during the walk itself rather than after, so excluded subtrees are never
even `os.walk`-descended into; (2) the byte-level prefilter above skips
`ast.parse` entirely for any file that does not contain one of the
wall-clock tokens as a raw substring -- in this repo the large majority of
testpaths files never mention `time.perf_counter`/`monotonic`/`time.time`
at all, so the parse+taint leg only runs against a small fraction of the
walked set. Both legs are warm-cache figures; this guard does not attempt
a cold-cache measurement of its own.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from coordinator_core.spawn_policy.marker_check import marker_call_nodes

_DELIBERATE_WALL_CLOCK_MARKER = "pytest.mark.deliberate_wall_clock"

_NORECURSEDIRS = {"_darcs", "CVS", "{arch}", "build", "dist", "node_modules", "venv"}

_WALL_TOKENS = (
    b"perf_counter",
    b"monotonic",
    b"time.time",
    b"time_ns",
    b"from time import",
)

_WALL_FUNCS = {"perf_counter", "perf_counter_ns", "monotonic", "monotonic_ns", "time", "time_ns"}

_COLLECTION_REDUCERS = {"min", "max", "sum", "sorted"}


@dataclass(frozen=True)
class Finding:
    path: str
    qualname: str
    lineno: int


def _collect_aliases(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    module_aliases: dict[str, str] = {}
    name_aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    module_aliases[alias.asname or alias.name] = "time"
        elif isinstance(node, ast.ImportFrom):
            if node.module == "time":
                for alias in node.names:
                    name_aliases[alias.asname or alias.name] = alias.name
    return module_aliases, name_aliases


def _is_wall_call(call: ast.Call, module_aliases: dict[str, str], name_aliases: dict[str, str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return module_aliases.get(func.value.id) == "time" and func.attr in _WALL_FUNCS
    if isinstance(func, ast.Name):
        return name_aliases.get(func.id) in _WALL_FUNCS
    return False


def _expr_taint(
    node: ast.expr,
    env: dict[str, str],
    module_aliases: dict[str, str],
    name_aliases: dict[str, str],
    func_returns: dict[str, str],
) -> str | None:
    if isinstance(node, ast.Call):
        if _is_wall_call(node, module_aliases, name_aliases):
            return "stamp"
        fname = None
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        if fname in _COLLECTION_REDUCERS and node.args:
            arg_t = _expr_taint(node.args[0], env, module_aliases, name_aliases, func_returns)
            if arg_t in ("collection", "duration"):
                return "duration"
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "statistics"
            and node.args
        ):
            arg_t = _expr_taint(node.args[0], env, module_aliases, name_aliases, func_returns)
            if arg_t in ("collection", "duration"):
                return "duration"
        if isinstance(node.func, ast.Name) and node.func.id in func_returns:
            return func_returns[node.func.id]
        return None
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.BinOp):
        left_t = _expr_taint(node.left, env, module_aliases, name_aliases, func_returns)
        right_t = _expr_taint(node.right, env, module_aliases, name_aliases, func_returns)
        if isinstance(node.op, ast.Sub) and left_t in ("stamp", "duration") and right_t in ("stamp", "duration"):
            return "duration"
        if left_t == "duration" or right_t == "duration":
            return "duration"
        return None
    if isinstance(node, ast.Subscript):
        base_t = _expr_taint(node.value, env, module_aliases, name_aliases, func_returns)
        return "duration" if base_t == "collection" else base_t
    return None


def _iter_stmts(stmts: list[ast.stmt]):
    for stmt in stmts:
        yield stmt
        for field in ("body", "orelse", "finalbody"):
            sub = getattr(stmt, field, None)
            if sub:
                yield from _iter_stmts(sub)


def _scan_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    module_aliases: dict[str, str],
    name_aliases: dict[str, str],
    func_returns: dict[str, str],
) -> tuple[list[int], str | None]:
    env: dict[str, str] = {}
    linenos: list[int] = []
    return_taint: str | None = None

    for stmt in _iter_stmts(func.body):
        if isinstance(stmt, ast.Assign):
            t = _expr_taint(stmt.value, env, module_aliases, name_aliases, func_returns)
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    if t is not None:
                        env[target.id] = t
                    else:
                        env.pop(target.id, None)
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                t = _expr_taint(stmt.value, env, module_aliases, name_aliases, func_returns)
                if t == "duration" or env.get(stmt.target.id) in ("duration", "stamp"):
                    env[stmt.target.id] = "duration"
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr in ("append", "extend")
                and isinstance(call.func.value, ast.Name)
                and call.args
            ):
                arg_t = _expr_taint(call.args[0], env, module_aliases, name_aliases, func_returns)
                if arg_t in ("duration", "stamp", "collection"):
                    env[call.func.value.id] = "collection"
        elif isinstance(stmt, ast.Return):
            if stmt.value is not None:
                t = _expr_taint(stmt.value, env, module_aliases, name_aliases, func_returns)
                if t is not None:
                    return_taint = t
        elif isinstance(stmt, ast.Assert):
            test = stmt.test
            if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
                op = test.ops[0]
                left_t = _expr_taint(test.left, env, module_aliases, name_aliases, func_returns)
                right_t = _expr_taint(test.comparators[0], env, module_aliases, name_aliases, func_returns)
                violation = (isinstance(op, (ast.Lt, ast.LtE)) and left_t == "duration") or (
                    isinstance(op, (ast.Gt, ast.GtE)) and right_t == "duration"
                )
                if violation:
                    linenos.append(stmt.lineno)

    return linenos, return_taint


def _compute_return_taints(
    tree: ast.Module, module_aliases: dict[str, str], name_aliases: dict[str, str]
) -> dict[str, str]:
    returns: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _, ret = _scan_function(node, module_aliases, name_aliases, {})
            if ret is not None:
                returns[node.name] = ret
    return returns


def _is_discharged(calls: list[ast.Call]) -> bool:
    for call in calls:
        for kw in call.keywords:
            if (
                kw.arg == "reason"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
                and kw.value.value.strip()
            ):
                return True
    return False


def _function_discharged(func: ast.FunctionDef | ast.AsyncFunctionDef, class_discharged: bool) -> bool:
    if class_discharged:
        return True
    return _is_discharged(marker_call_nodes(decorators=func.decorator_list, marker=_DELIBERATE_WALL_CLOCK_MARKER))


def _scan_source(text: str, path: str) -> list[Finding]:
    tree = ast.parse(text)
    module_aliases, name_aliases = _collect_aliases(tree)
    func_returns = _compute_return_taints(tree, module_aliases, name_aliases)

    module_discharged = _is_discharged(
        marker_call_nodes(body=tree.body, marker=_DELIBERATE_WALL_CLOCK_MARKER)
    )

    findings: list[Finding] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            if _function_discharged(node, module_discharged):
                continue
            linenos, _ = _scan_function(node, module_aliases, name_aliases, func_returns)
            findings.extend(Finding(path, node.name, ln) for ln in linenos)
        elif isinstance(node, ast.ClassDef):
            class_discharged = module_discharged or _is_discharged(
                marker_call_nodes(decorators=node.decorator_list, marker=_DELIBERATE_WALL_CLOCK_MARKER)
            ) or _is_discharged(
                marker_call_nodes(body=node.body, marker=_DELIBERATE_WALL_CLOCK_MARKER)
            )
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    if _function_discharged(item, class_discharged):
                        continue
                    linenos, _ = _scan_function(item, module_aliases, name_aliases, func_returns)
                    findings.extend(Finding(path, f"{node.name}.{item.name}", ln) for ln in linenos)

    return findings


def _walk_testpaths(repo_root: Path) -> list[Path]:
    from coordinator_core.diff_scoped_tests import _read_testpaths

    files: list[Path] = []
    for root in _read_testpaths(str(repo_root)):
        root_path = repo_root / root
        if root_path.is_file():
            if root_path.name.startswith("test_") and root_path.suffix == ".py":
                files.append(root_path)
            continue
        if not root_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [
                d for d in dirnames if d not in _NORECURSEDIRS and not d.startswith(".") and not d.endswith(".egg")
            ]
            for fn in filenames:
                if fn.startswith("test_") and fn.endswith(".py"):
                    files.append(Path(dirpath) / fn)
    return files


def _scan_repo(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _walk_testpaths(repo_root):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not any(tok in raw for tok in _WALL_TOKENS):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            rel = str(path.relative_to(repo_root))
        except ValueError:
            rel = str(path)
        try:
            findings.extend(_scan_source(text, rel))
        except SyntaxError:
            continue
    return findings


def test_no_undischarged_wall_clock_ratchets():
    repo_root = Path(__file__).resolve().parents[2]
    findings = _scan_repo(repo_root)
    if findings:
        lines = [f"{f.path}::{f.qualname} line {f.lineno}" for f in findings]
        pytest.fail(
            "Undischarged wall-clock upper-bound assertion(s):\n"
            + "\n".join(lines)
            + "\nMeasure through coordinator_core/benchmarks/process_time.py, "
            "or declare pytest.mark.deliberate_wall_clock(reason=\"...\")."
        )


def _findings(text: str) -> list[Finding]:
    return _scan_source(text, "synthetic.py")


def test_red_host_sampler_append_then_min_shape():
    src = """
import time

BUDGET_MS = 500

def test_perf():
    samples = []
    for _ in range(5):
        t0 = time.perf_counter()
        do_thing()
        samples.append(time.perf_counter() - t0)
    elapsed_ms = min(samples) * 1000
    assert elapsed_ms < BUDGET_MS
"""
    assert _findings(src)


def test_red_stamp_variable_shape():
    src = """
import time

BUDGET_MS = 500

def test_perf():
    t0 = time.perf_counter()
    do_thing()
    assert (time.perf_counter() - t0) * 1000 < BUDGET_MS
"""
    assert _findings(src)


def test_red_helper_return_shape():
    src = """
import time

BUDGET_MS = 500

def _measure():
    t0 = time.perf_counter()
    do_thing()
    return time.perf_counter() - t0

def test_perf():
    elapsed = _measure()
    assert elapsed * 1000 < BUDGET_MS
"""
    assert _findings(src)


def test_red_bare_marker_does_not_discharge():
    src = """
import time
import pytest

BUDGET_MS = 500

@pytest.mark.deliberate_wall_clock
def test_perf():
    t0 = time.perf_counter()
    do_thing()
    assert (time.perf_counter() - t0) * 1000 < BUDGET_MS
"""
    assert _findings(src)


def test_green_reasoned_marker_at_function_level():
    src = """
import time
import pytest

BUDGET_MS = 500

@pytest.mark.deliberate_wall_clock(reason="waits on an external eventual-consistency window")
def test_perf():
    t0 = time.perf_counter()
    do_thing()
    assert (time.perf_counter() - t0) * 1000 < BUDGET_MS
"""
    assert not _findings(src)


def test_green_reasoned_marker_at_class_decorator_level():
    src = """
import time
import pytest

BUDGET_MS = 500

@pytest.mark.deliberate_wall_clock(reason="class-wide wait behaviour")
class TestPerf:
    def test_perf(self):
        t0 = time.perf_counter()
        do_thing()
        assert (time.perf_counter() - t0) * 1000 < BUDGET_MS
"""
    assert not _findings(src)


def test_green_reasoned_marker_at_class_pytestmark_level():
    src = """
import time
import pytest

BUDGET_MS = 500

class TestPerf:
    pytestmark = pytest.mark.deliberate_wall_clock(reason="class body pytestmark form")

    def test_perf(self):
        t0 = time.perf_counter()
        do_thing()
        assert (time.perf_counter() - t0) * 1000 < BUDGET_MS
"""
    assert not _findings(src)


def test_green_reasoned_marker_at_module_pytestmark_level():
    src = """
import time
import pytest

BUDGET_MS = 500

pytestmark = pytest.mark.deliberate_wall_clock(reason="module-wide wait behaviour")

def test_perf():
    t0 = time.perf_counter()
    do_thing()
    assert (time.perf_counter() - t0) * 1000 < BUDGET_MS
"""
    assert not _findings(src)


def test_green_lower_bound_wait_is_not_a_ratchet():
    src = """
import time

MIN_WAIT_MS = 50

def test_eventually_consistent():
    t0 = time.perf_counter()
    wait_for_thing()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms > MIN_WAIT_MS
"""
    assert not _findings(src)


def test_green_timestamp_arithmetic_is_not_wall_clock():
    src = """
def test_ordering():
    created = record.created_at
    updated = record.updated_at
    assert updated - created < MAX_DRIFT
"""
    assert not _findings(src)


def test_green_process_time_based_assert():
    src = """
from coordinator_core.benchmarks.process_time import batched_process_time_ms

BUDGET_MS = 500

def test_perf():
    ms = batched_process_time_ms(["true"], k=10)
    assert ms < BUDGET_MS
"""
    assert not _findings(src)
