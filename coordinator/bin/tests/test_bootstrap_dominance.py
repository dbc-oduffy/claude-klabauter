"""coordinator.bin.tests.test_bootstrap_dominance -- the guard for lazy-boot
part 5: every `coordinator/bin/*.py` module that lazily binds engine-side
names/imports inside a `_bootstrap_*()`-shaped function must have that
bootstrap call dominate every reachable path into every function that
depends on it, not merely `main()`.

Purpose: `__getattr__` (PEP 562) only serves a bootstrapped name to an
importer reaching for it as a MODULE ATTRIBUTE from outside the module. It
does nothing for a function inside the module that reads the name as a bare
global, and nothing for a function that does its own deferred
`from cc_invoke import X` / `from coordinator_core.foo import Y` without
calling the bootstrap first. Both shapes are invisible to `--help`,
`py_compile`, import, and any test that only enters through `main()` -- they
fire only when an external in-process caller (`workstream_complete.apply.
_load_cli_module`, `cli_dispatch.load_cli_module`, or any other
`importlib.util.spec_from_file_location` consumer) calls a NON-`main`
function directly, which is exactly what killed workstream-complete before
`3c6512c07` (see the bug-backlog row this discharges,
`state/bug-backlog/2026-08-28-nothing-enforces-part-5-of-the-lazy-boot-
5f465b8c1919.yaml`).

WHAT THIS ANALYSIS MODELS, PRECISELY (read before trusting a "clean" run).

A "bootstrap function" is any module-level function whose body contains a
bare `import lib` statement (the corpus's own marker for "this call puts
`coordinator/bin/lib` on `sys.path`" -- see `_bootstrap_engine_imports` in
`wsc-session-disposition.py` for the canonical shape). A "bootstrapped
global" is a name assigned `None` at module scope and later rebound via a
`global` statement inside a bootstrap function -- shape 1, the bare-global
read. A "deferred engine import" is an `import`/`from...import` inside any
OTHER function naming `coordinator_core`(.*), `coordinator_registry`,
`cc_invoke`, `repo_identity`, `cli_shared`, `op_trampoline`, or any
underscore-prefixed bin-sibling module (the `_queue_append_locator`
convention) -- shape 2, the deferred import.

A function F is SAFE if EITHER:
  (a) bootstrap is reachable DOWNWARD from F -- F itself is a bootstrap
      function, F calls one directly, or F calls something that (by the same
      rule, recursively) does; OR
  (b) bootstrap is reachable UPWARD from F -- F has at least one in-module
      caller and EVERY one of F's callers is itself safe by this same
      definition, computed to a fixed point (not one hop: a caller-safe-if-
      its-own-caller-is-safe chain of any length counts, closing exactly the
      gap the bug-backlog row names -- "one hop... is an upper bound with
      unknown slack").

A function with ZERO in-module callers that does not itself reach bootstrap
downward is treated as its own external entry point and is UNSAFE if risky
-- this is the load-bearing modeling choice: an exported (non-underscore)
helper nothing else in the file calls, like `primary_consumed_handoff_paths`
in `wsc-session-disposition.py`, is exactly as reachable from outside as
`main()` is, and a caller-only-if-called-internally story does not cover it.

WHAT THIS DOES NOT MODEL. No inter-procedural alias analysis (a name bound
to a bootstrap function under a second name is invisible), no data-flow
ordering WITHIN a function body (a bootstrap call appearing textually AFTER
a risky read in the same function is still counted as making that function
safe -- rare in this corpus's own idiom, which always bootstraps first, but
a real gap), and no cross-module call graph (a function only ever called
from a SIBLING bin file, not from within its own module, is scored as having
zero in-module callers and therefore must self-bootstrap -- conservative,
not unsound, but can over-flag). Findings this analysis produces that a
human reviews and confirms safe belong in `_REVIEWED_DISPOSITIONS` below,
keyed by `path:func`, never by silently narrowing the analysis to stop
seeing them.

Negative-spec: this module does NOT invoke, import, or exec any
`coordinator/bin/*.py` module body -- `_analyze_module` is AST-only, exactly
like `coordinator_core.warm.serve_classifier` (C1) this suite's shape is
lifted from. It does NOT re-derive the "9 candidates, one hop" checker the
backlog row names as already wrong -- this is a new, from-scratch analysis
with its own fail-closed proof (`test_fails_closed_on_known_bad_fixtures`
below), not a tuning of that one.

Spec backlink: state/bug-backlog/2026-08-28-nothing-enforces-part-5-of-the-lazy-boot-5f465b8c1919.yaml
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent

def _bin_lib_module_names() -> frozenset[str]:
    """Every bare name that only resolves once `coordinator/bin/lib` is on
    `sys.path` -- read off the directory, never hand-enumerated.

    Negative-spec: this is NOT a curated list of "the risky ones". The set of
    names that need the bootstrap IS the set of modules in that directory, so
    a literal tuple here is a copy that goes stale silently the moment a
    sibling is added. It did: the tuple this replaced named six modules out of
    thirty-one, and the twenty-five it omitted were unguarded --
    `check-no-illegal-paths.py` (`coordinator_safe_name`),
    `check-multi-event-hook-hardcoded-event.py` (`coordinator_data_root`), and
    `workday-start-handoff-triage.py` (`records_query`) all shipped the
    ModuleNotFoundError this guard exists to catch, past a green run.
    """
    lib_dir = _BIN_DIR / "lib"
    return frozenset(
        p.stem
        for p in lib_dir.glob("*.py")
        if p.stem != "__init__" and not p.stem.startswith("test_")
    )


_ENGINE_MODULE_PREFIXES = ("coordinator_core", *sorted(_bin_lib_module_names()))


def _is_engine_import(module: str) -> bool:
    """True for a module name that only resolves once `coordinator/bin/lib`
    is on `sys.path` -- `lib` itself is the bootstrap primitive, not a risky
    use of one, so it is explicitly excluded."""
    if not module or module == "lib":
        return False
    if module.startswith("_"):
        return True
    return any(module == p or module.startswith(p + ".") for p in _ENGINE_MODULE_PREFIXES)


def _contains_import_lib(node: ast.AST) -> bool:
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "lib":
                    return True
    return False


class Violation(NamedTuple):
    """`path` is repo-relative-to-`coordinator/bin`, POSIX-separated, so a
    baseline entry compares identically on every OS this suite runs on."""

    path: str
    func: str
    shape: str
    detail: str

    def key(self) -> str:
        return f"{self.path}:{self.func}"


def _analyze_module(rel_path: str, source: str) -> list[Violation]:
    """AST-only analysis of a single module's source text -- see module
    docstring for exactly what this does and does not model. Returns one
    `Violation` per (risky function, risky use) pair found unsafe."""
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return []

    funcs: dict[str, ast.FunctionDef] = {}
    module_none_names: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node
        elif isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        module_none_names.add(tgt.id)

    if not funcs:
        return []

    bootstrap_names = {n for n, f in funcs.items() if _contains_import_lib(f)}
    if not bootstrap_names:
        # This module does not use the lazy-bootstrap pattern at all --
        # out of scope for this guard, not evidence of anything.
        return []

    bootstrapped_globals: set[str] = set()
    for name in bootstrap_names:
        f = funcs[name]
        globaled: set[str] = set()
        for stmt in ast.walk(f):
            if isinstance(stmt, ast.Global):
                globaled.update(stmt.names)
        bootstrapped_globals.update(globaled & module_none_names)

    call_graph: dict[str, set[str]] = {}
    for name, f in funcs.items():
        called: set[str] = set()
        for node in ast.walk(f):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in funcs:
                    called.add(node.func.id)
        call_graph[name] = called

    memo: dict[str, bool] = {}

    def own_bootstraps(name: str, visiting: frozenset) -> bool:
        """Downward half of safety: does bootstrap sit somewhere in this
        function's own call subtree. Memoized DFS, cycle-safe (a cycle with
        no bootstrap call stays False, the conservative default)."""
        if name in memo:
            return memo[name]
        if name in bootstrap_names:
            memo[name] = True
            return True
        if name in visiting:
            return False
        result = False
        for callee in call_graph.get(name, ()):
            if callee in bootstrap_names or own_bootstraps(callee, visiting | {name}):
                result = True
                break
        memo[name] = result
        return result

    reverse_graph: dict[str, set[str]] = {name: set() for name in funcs}
    for name, callees in call_graph.items():
        for callee in callees:
            reverse_graph[callee].add(name)

    safe = {name: own_bootstraps(name, frozenset()) for name in funcs}
    changed = True
    while changed:
        # Upward half: least-fixpoint forward propagation over the reverse
        # call graph -- transitive, not one hop, closing the gap the origin
        # bug-backlog row names as the prior attempt's unresolved slack.
        changed = False
        for name in funcs:
            if safe[name]:
                continue
            callers = reverse_graph.get(name, set())
            if callers and all(safe[c] for c in callers):
                safe[name] = True
                changed = True

    violations: list[Violation] = []
    for name, f in funcs.items():
        if name in bootstrap_names:
            continue

        shape1_hit = bool(bootstrapped_globals) and any(
            isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in bootstrapped_globals
            for node in ast.walk(f)
        )

        shape2_hits: list[str] = []
        for node in ast.walk(f):
            if isinstance(node, ast.ImportFrom):
                if _is_engine_import(node.module or ""):
                    shape2_hits.append(node.module or "")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_engine_import(alias.name):
                        shape2_hits.append(alias.name)

        if not shape1_hit and not shape2_hits:
            continue
        if safe[name]:
            continue

        if shape1_hit:
            violations.append(
                Violation(rel_path, name, "bare-global-read", ",".join(sorted(bootstrapped_globals)))
            )
        for mod in shape2_hits:
            violations.append(Violation(rel_path, name, "deferred-import", mod))

    return violations


def _analyze_bin_dir(bin_dir: Path) -> list[Violation]:
    """Every `*.py` directly under `bin_dir` (the flat `coordinator/bin/`
    population the origin bug-backlog row scoped this guard to -- `bin/lib`
    and `bin/tests` are separate populations with their own conventions and
    are not walked here)."""
    out: list[Violation] = []
    for path in sorted(bin_dir.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out.extend(_analyze_module(path.name, source))
    return out


# --- Reviewed-disposition escape hatch -------------------------------------
#
# Keyed `"<file.py>:<function>"`, convention lifted from
# `coordinator/bin/classify-env-var-callers.py :: _REVIEWED_DISPOSITIONS`
# (read that map's own comment for why it lives in source, not generated
# output: a moved/renamed file falls out of the map and re-enters review
# rather than silently inheriting a verdict about a different location).
#
# Currently empty -- every LIVE finding at guard-authoring time is recorded
# in `_BASELINE` below as a genuine violation, not dispositioned away here.
# Add an entry only after confirming by hand that a flagged (path, func) is
# NOT reachable via an unbootstrapped path (e.g. the function's sole
# in-module caller is itself unreachable dead code verified by other means
# this analysis cannot see) -- never to silence a real finding.
_REVIEWED_DISPOSITIONS: dict[str, str] = {}


def _live_violations() -> list[Violation]:
    findings = _analyze_bin_dir(_BIN_DIR)
    return [v for v in findings if v.key() not in _REVIEWED_DISPOSITIONS]


# --- Committed baseline ------------------------------------------------
#
# Snapshotted 2026-08-29 against the then-live `coordinator/bin/*.py`
# population via this module's own `_analyze_bin_dir`. This is the corpus's
# UNFIXED state at guard-authoring time, not a target -- a new violation
# lands as a row NOT in this list and fails `test_no_new_violations`
# immediately; `test_baseline_has_no_stale_entries` forces an entry OUT the
# moment a fix (or a file/function rename) makes it stop matching a live
# finding, so this list can only shrink under review, never rot silently.
_BASELINE: list[tuple[str, str, str, str]] = []


def _assert_no_new(live: set[tuple[str, str, str, str]]) -> None:
    baseline = set(_BASELINE)
    new = sorted(live - baseline)
    rendered = "\n".join(f"  {p} :: {fn} [{shape}] {detail}" for p, fn, shape, detail in new)
    assert new == [], (
        f"Found {len(new)} NEW bootstrap-dominance violation(s) not in _BASELINE -- "
        f"a function reads a bootstrapped global or does a deferred engine import "
        f"with no bootstrap call reachable on any path into it. Fix it (call the "
        f"module's bootstrap function first) or, if a hand-verified false positive, "
        f"add it to _REVIEWED_DISPOSITIONS with a stated reason:\n{rendered}"
    )


def test_no_new_bootstrap_dominance_violations():
    """The guard proper: every LIVE finding must already be in `_BASELINE`.
    A new bin/ module (or an existing one edited) that adds a function
    reading a bootstrapped global, or doing a deferred engine import, with
    no bootstrap call dominating every path into it, fails this test the
    moment it lands."""
    live = {(v.path, v.func, v.shape, v.detail) for v in _live_violations()}
    _assert_no_new(live)


def _bootstraps_anywhere(tree: ast.AST, source: str) -> bool:
    """True if the module puts `coordinator/bin/lib` on `sys.path` by ANY of
    the three shapes the corpus actually uses.

    1. `import lib` -- the sanctioned idiom (`lib/__init__.py` is the single
       mutation site; see its module docstring).
    2. An explicit `sys.path` insert/append -- the preamble that idiom
       replaced. Still present in a handful of files and still effective, so
       it counts as bootstrapped here even though the negative-spec in
       `lib/__init__.py` asks new code not to write it.
    3. `spec_from_file_location("lib", ...)` -- `coordinator-doc-new.py`'s
       by-location import, which exists precisely because a bare `import lib`
       can bind `coordinator/lib` instead on an unlucky `sys.path` order.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "lib" for a in node.names):
            return True
    if "sys.path.insert" in source or "sys.path.append" in source:
        return True
    return "spec_from_file_location" in source and '"lib"' in source


def _unbootstrapped_modules() -> list[tuple[str, str]]:
    """`(filename, module)` for every `coordinator/bin/*.py` CLI that imports a
    `bin/lib` sibling while bootstrapping nowhere in the file.

    Why this is separate from the dominance analysis: that analysis returns
    early on `if not bootstrap_names` -- a module carrying no bootstrap at all
    is not an unsafe path through a bootstrap, it is a module with no
    bootstrap to be unsafe about, and it was skipped as out of scope. That
    early return is the blind spot the whole class shipped through. Dominance
    asks "does the bootstrap reach here"; this asks the prior question, "is
    there one".
    """
    lib_names = _bin_lib_module_names()
    out: list[tuple[str, str]] = []
    for path in sorted(_BIN_DIR.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        if _bootstraps_anywhere(tree, source):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] in lib_names:
                    out.append((path.name, node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in lib_names:
                        out.append((path.name, alias.name))
    return sorted(set(out))


def test_no_bin_lib_import_without_any_bootstrap():
    """A `bin/` CLI may not import a `bin/lib` sibling while bootstrapping
    nowhere in the file.

    `bin/lib` is not a package and sits on no default path, so such an import
    raises `ModuleNotFoundError` the moment its line is reached -- at import
    time if module-level, on first call if deferred, which is why three of
    these reached the fleet with `--help` and `py_compile` both green.
    """
    found = _unbootstrapped_modules()
    rendered = "\n".join(f"  {name} imports `{mod}`" for name, mod in found)
    assert found == [], (
        f"{len(found)} `coordinator/bin/*.py` import(s) of a `bin/lib` sibling with no "
        f"bootstrap anywhere in the file -- each raises ModuleNotFoundError when reached. "
        f"Add `import lib  # noqa: F401` before the import (in the same function, if the "
        f"import is deferred):\n{rendered}"
    )


def test_population_examined_is_nonzero_and_matches_corpus():
    """Pins the population, not just the derived findings -- a silently
    empty or partial scan (wrong `_BIN_DIR`, a swallowed exception, a glob
    that stopped matching) would make `test_no_new_bootstrap_dominance_
    violations` pass vacuously over nothing scanned. Same failure mode
    `test_every_allowlisted_name_warm_serves.py` (C8) was found to have this
    week: an empty live set matched an empty baseline and nobody could tell.

    The lower bound (300) is deliberately well under the ~370-419 files
    named across the origin backlog row and this session's own measurement
    (421 at guard-authoring time) -- this corpus grows continuously; the
    bound catches "examined nothing" and "examined a stray handful", not
    "grew or shrank by a few files since this was written"."""
    files = sorted(_BIN_DIR.glob("*.py"))
    assert len(files) > 300, (
        f"only found {len(files)} coordinator/bin/*.py files -- population "
        f"looks wrong, not merely different from the ~370-419 measured at "
        f"authoring time"
    )
    modules_with_bootstrap = 0
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        tree = ast.parse(source, filename=path.name)
        if any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _contains_import_lib(n)
            for n in tree.body
        ):
            modules_with_bootstrap += 1
    assert modules_with_bootstrap > 50, (
        f"only {modules_with_bootstrap} bin/ modules were classified as using "
        f"the lazy-bootstrap pattern at all -- the `import lib`-inside-a-"
        f"function marker this guard keys off may have stopped matching"
    )


def test_baseline_has_no_stale_entries():
    """The shrink-forcing half of the ratchet: a baseline entry that no
    longer matches a live finding means the underlying function was fixed,
    renamed, or removed -- delete the entry here rather than leaving it to
    rot as sibling executors repair this same corpus."""
    live = {(v.path, v.func, v.shape, v.detail) for v in _live_violations()}
    baseline = set(_BASELINE)
    stale = sorted(baseline - live)
    rendered = "\n".join(f"  {p} :: {fn} [{shape}] {detail}" for p, fn, shape, detail in stale)
    assert stale == [], (
        f"{len(stale)} _BASELINE entr(ies) no longer match a live violation -- "
        f"the underlying function was fixed, renamed, or removed; delete "
        f"these entries:\n{rendered}"
    )


# Shape-1 fail-closed fixture: a TRIMMED, byte-faithful excerpt of the real
# `coordinator/bin/wsc-session-disposition.py` at git revision `3c6512c07^`
# (`git show 3c6512c07^:coordinator/bin/wsc-session-disposition.py`) -- the
# actual historical pre-fix state where `_bootstrap_engine_imports()` had
# exactly one call site (`main()`), so every function on the
# `resolve_disposition()` in-process entry path (the path
# `workstream_complete.compute_session_shape_gate` actually calls, bypassing
# `main()`) read `None` globals. This is an EXCERPT, not the full file --
# only the module preamble, the bootstrap function, and stand-ins for the 7
# affected functions (bodies trimmed to the minimal read of each global that
# reproduces the defect; names and the global-read shape are verbatim) --
# deliberately literal text rather than a `git show` subprocess call at test
# time, so this guard's own proof-of-soundness stays on the fast tier
# instead of tripping the spawn ratchet (`coordinator_core/tests/
# test_no_new_spawning_tests.py`) and being forced onto `cadence`, which
# would remove this guard from the per-commit path the origin bug-backlog
# row exists to put it on.
_SHAPE1_FIXTURE_SOURCE = '''\
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(_SCRIPT_DIR, "lib")

resolve_claim_state = None
show_toplevel = None
rel_id = None
session_deliverable_ids = None


def _bootstrap_engine_imports() -> None:
    global resolve_claim_state, show_toplevel, rel_id, session_deliverable_ids

    import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
    import cc_invoke

    cc_invoke.ensure_engine_on_path(__file__)

    from coordinator_core.claim_state import resolve_claim_state as _resolve_claim_state
    from coordinator_core.git.repo_root import show_toplevel as _show_toplevel
    from coordinator_core.wire_paths import rel_id as _rel_id
    from coordinator_core.workstream_complete.session_identity import (
        session_deliverable_ids as _session_deliverable_ids,
    )

    resolve_claim_state = _resolve_claim_state
    show_toplevel = _show_toplevel
    rel_id = _rel_id
    session_deliverable_ids = _session_deliverable_ids


def primary_consumed_handoff_scan(repo_root, sid):
    state = resolve_claim_state(repo_root, repo_root=repo_root)
    return state


def detector_a(repo_root, sid):
    return show_toplevel(repo_root)


def _foreign_consumer_guard(repo_root, sid):
    return rel_id(repo_root, repo_root)


def _resolve_deliverable_id_join(repo_root, sid):
    return session_deliverable_ids(repo_root, sid)


def find_memo_predecessor(repo_root, sid):
    return resolve_claim_state(repo_root, repo_root=repo_root)


def _normalize_override_handoff(repo_root, sid):
    return rel_id(repo_root, repo_root)


def _git_show_toplevel(repo_root):
    return show_toplevel(repo_root)


def resolve_disposition(repo_root, sid):
    # Zero in-module callers, verbatim to the real pre-fix file --
    # `coordinator_core.workstream_complete.compute_session_shape_gate`
    # calls this directly by name, never through `main()`.
    return primary_consumed_handoff_scan(repo_root, sid)


def _cmd_resolve(args):
    return 0


def main(argv):
    _bootstrap_engine_imports()
    return _cmd_resolve(argv)
'''


def test_fails_closed_on_known_bad_fixtures():
    """The half of this guard's own claim that must not rest on prose alone
    -- two bespoke checkers on this exact workstream were already wrong (one
    by 148 findings, one a confident zero from an excluded ground truth), so
    this guard is proven to fail closed before it is trusted, on BOTH
    sub-shapes independently, per the origin backlog row's explicit
    "two sub-shapes and a guard that models only the first is worthless".

    Shape 1: `_SHAPE1_FIXTURE_SOURCE` above (see its own comment for
    provenance). Shape 2: synthetic below -- a bootstrap function importing
    `coordinator_registry`, and a SEPARATE risky function doing the same
    deferred import with no bootstrap call anywhere on its path (no caller,
    no self-call)."""
    shape1_violations = _analyze_module("wsc-session-disposition.py", _SHAPE1_FIXTURE_SOURCE)
    shape1_funcs = {v.func for v in shape1_violations if v.shape == "bare-global-read"}
    expected_shape1 = {
        "primary_consumed_handoff_scan",
        "detector_a",
        "_foreign_consumer_guard",
        "_resolve_deliverable_id_join",
        "find_memo_predecessor",
        "_normalize_override_handoff",
        "_git_show_toplevel",
    }
    assert shape1_funcs == expected_shape1, (
        f"shape-1 fail-closed fixture did not reproduce the known pre-3c6512c07 "
        f"defect exactly -- got {sorted(shape1_funcs)}, expected {sorted(expected_shape1)}"
    )

    shape2_source = (
        "def _bootstrap_engine():\n"
        "    import lib\n"
        "    from coordinator_registry import doe_root\n"
        "\n\n"
        "def _risky_helper(x):\n"
        "    from coordinator_registry import doe_root\n"
        "    return doe_root()\n"
        "\n\n"
        "def main(argv):\n"
        "    return _risky_helper(argv)\n"
    )
    shape2_violations = _analyze_module("shape2-fixture.py", shape2_source)
    assert [(v.func, v.shape, v.detail) for v in shape2_violations] == [
        ("_risky_helper", "deferred-import", "coordinator_registry")
    ], f"shape-2 fail-closed fixture did not reproduce the deferred-import defect: {shape2_violations}"

    # And the fixed shape stays clean -- proves the guard does not merely
    # flag every function that ever imports an engine module, only the ones
    # with no bootstrap call reachable on any path into them.
    shape2_fixed_source = shape2_source.replace(
        "def _risky_helper(x):\n    from coordinator_registry import doe_root\n    return doe_root()\n",
        "def _risky_helper(x):\n    _bootstrap_engine()\n    from coordinator_registry import doe_root\n    return doe_root()\n",
    )
    shape2_fixed_violations = _analyze_module("shape2-fixture.py", shape2_fixed_source)
    assert shape2_fixed_violations == [], (
        f"guard still flags a function that DOES call its bootstrap first: "
        f"{shape2_fixed_violations}"
    )
