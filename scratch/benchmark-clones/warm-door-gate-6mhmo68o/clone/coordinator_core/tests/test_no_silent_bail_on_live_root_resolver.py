"""Meta-test: no test in this suite may resolve a live repo root and then bail
via a bare ``return`` without ``@pytest.mark.real_home`` or a visible
``pytest.skip``.

The defect class (2026-07-27, six instances found in one day; see
``git show f622297b`` and ``git show a4ef240b``): ``conftest.py``'s suite-root
``_quarantine_real_home`` autouse fixture hides the machine-local registry
from every test (correct — it is the fix for the 2026-07-20 Windows
real-``~/.claude/.doe-root``-clobber incident). A test that resolves a real
repo root through one of the resolvers in ``_LIVE_ROOT_RESOLVER_NAMES`` below
therefore cannot reach its subject under quarantine unless it opts out via
``@pytest.mark.real_home``. How such a test bails when the resolver fails
determines whether a human ever learns it did not assert:

  - bare ``return`` (directly, or as the sole statement of an ``if`` guard or
    an ``except`` handler) -> pytest reports **PASS**. "No oracle was
    available" becomes indistinguishable from "the oracle held" — the exact
    shape ``a4ef240b`` found and fixed in three places (fourth found and
    fixed same-day in ``f622297b``).
  - ``pytest.skip(...)`` -> visible, honest, correctly non-asserting.
  - ``@pytest.mark.real_home`` -> the test actually runs against live state.

This gate mechanizes the hand-sweep that found those instances: it AST-walks
every ``test_*.py`` file under ``coordinator_core/`` (this suite's own
``testpaths`` root, per ``pytest.ini``) and fails when a ``test_*`` function
BOTH (a) calls one of the live-root resolvers, AND (b) contains a bare
``return`` nested inside an ``if`` or ``except`` block that is ITSELF
CORRELATED with that resolver call (the ``try: root = resolver() / except:
return`` shape, or an ``if`` whose test directly calls the resolver or tests
a name assigned from it — see ``_guard_is_correlated_with_resolver``), AND
does not carry ``@pytest.mark.real_home`` (function- or class-level). A bare
``return`` inside an if/except that is NOT correlated with the resolver call
(e.g. an unrelated Windows-skip guard in a function that also happens to
call a resolver elsewhere) is not flagged — this correlation requirement was
added to close Finding 2 of the 2026-07-27 code review, which named the
un-correlated version of this check as a false-positive risk. A bare
``return`` with no enclosing if/except at all is never flagged either way —
see "Blind spots" below for the precision tradeoffs both of these still
imply.

Resolver names (established from the tree, not asserted from memory) — every
function in ``coordinator_core`` whose contract is "resolve a REAL sibling-
repo or machine root, raising/returning falsy on failure":
``coordinator_core.machine_resolver.registry_get``,
``coordinator_core.doe_root_pointer.read_doe_root_pointer``,
``coordinator_core.install._shared.resolve_coordinator_root`` /
``coordinator_core.ops.emit.resolvers.resolve_coordinator_root`` (same name,
two call sites, both live-root oracles),
``coordinator_core.plugin_health.relocation_ledger.default_ledger_path``,
``coordinator_core.engine_root.coordinator_engine_root``. Matched by bare
name/attribute only, not full import resolution — the same precision
tradeoff ``test_no_node_schema_shellout.py`` makes for its spawn-call
detector, and for the same reason: a test corpus calls these through module
aliases (``rl.default_ledger_path()``) and bare imports alike, and requiring
full symbol resolution to disambiguate every possible call form would be a
much larger AST-resolution project for a corpus this size. The one aliased-
import form this gate DOES resolve, specifically because it was found live
in the corpus (``from ... import registry_get as real_registry_get``), is
handled by ``_resolver_aliases_in_module`` building a per-file alias->real-
name map from each file's own ``ImportFrom`` ``asname`` bindings — see that
function's docstring for what remains out of scope (module-alias attribute
calls like ``rl.default_ledger_path()`` are already covered by
``_call_name``'s attribute branch, which ignores the qualifying prefix).

Blind spots (named per the fleet's own honest-coverage-statement discipline
— an incomplete-but-declared gate beats an implied-complete one):

  - **Indirection.** A test that calls a LOCAL helper which itself calls a
    resolver and swallows the failure is invisible here — this gate only
    sees resolver calls made directly inside the ``test_*`` function body.
  - **Bail via a shared fixture.** A ``pytest.fixture`` that resolves a
    root and bails silently on behalf of every test that requests it is not
    itself a ``test_*`` function and is not scanned.
  - **Non-bare early return.** ``return False`` / ``return some_value`` are
    not matched — only a bare ``return`` (``ast.Return`` with ``value is
    None``) counts, because a non-bare return at least COULD be asserted on
    by the caller. A test harness that discards a non-None return value
    unseen would still be silent, and this gate would not catch it.
  - **New resolver names.** A future live-root resolver not added to
    ``_LIVE_ROOT_RESOLVER_NAMES`` is invisible until this list is updated —
    the list is a maintained enumeration, not derived automatically from the
    production tree on every run (deliberately: auto-deriving it would let a
    resolver rename silently reshape what this gate checks).
  - **Same-shaped bail with no resolver call in the immediate function
    body.** If the resolver call happens one call-frame away (e.g. a
    ``conftest.py`` fixture value is merely consumed, not resolved, in the
    test body) this gate sees no resolver call and does not flag it, even
    if the fixture itself has the silent-bail shape (fixtures are excluded
    per the indirection blind spot above).
  - **Correlation is syntactic, not full data-flow.** ``_guard_is_correlated_
    with_resolver`` only recognizes a resolver value fed through a direct
    ``name = resolver(...)`` assignment or a resolver call written literally
    in the guard's own test expression. A resolver result laundered through
    tuple/attribute unpacking, an intermediate helper call, or reassigned
    under a second name before the guard tests it would not be recognized as
    correlated and the guard would be treated as unrelated (silently
    under-flagging rather than over-flagging, the opposite failure mode from
    the one this restriction was added to close).

Spec backlink: ``git show f622297b`` (fleet_reachability's gate-cannot-pass-
vacuously worked fix and its counter-property test) and ``git show
a4ef240b`` (the three hand-found ``@pytest.mark.real_home`` corrections this
gate mechanizes the sweep for).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Live-root resolver names (established 2026-07-27 by reading the tree; see
# module docstring "Resolver names" above for provenance of each entry).
# ---------------------------------------------------------------------------
_LIVE_ROOT_RESOLVER_NAMES: frozenset[str] = frozenset({
    "registry_get",
    "read_doe_root_pointer",
    "resolve_coordinator_root",
    "default_ledger_path",
    "coordinator_engine_root",
})

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"


def _repo_relpath(path: Path, scan_root: Path) -> str:
    """Best-effort relpath for reporting: relative to the real repo when the
    scan ran there, relative to the scan root itself for an isolated
    tmp_path fixture (self-test fixtures live outside the repo)."""
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(scan_root.resolve()).as_posix()


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _resolver_aliases_in_module(tree: ast.Module) -> dict[str, str]:
    """Map a locally-bound name to its real resolver name for every
    ``from <module> import <resolver> as <alias>`` binding at any level of
    ``tree`` (module-level imports are the common case, but a local import
    inside a function body is walked too since ``ast.walk`` does not care
    about nesting).

    This closes Finding 1 of the 2026-07-27 code review of this file: a
    resolver call reached only through an aliased import
    (``from coordinator_core.machine_resolver import registry_get as
    real_registry_get``) previously bound the bare name ``real_registry_get``,
    which ``_LIVE_ROOT_RESOLVER_NAMES`` membership never matched -- the alias
    hid the call from this gate entirely. A bare (non-aliased) ``from ...
    import registry_get`` binds ``registry_get`` itself and needs no entry
    here; ``_calls_live_root_resolver`` already matches that directly."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name in _LIVE_ROOT_RESOLVER_NAMES and alias.asname:
                aliases[alias.asname] = alias.name
    return aliases


def _calls_live_root_resolver(
    func_node: ast.AST, alias_map: dict[str, str] | None = None
) -> list[int]:
    """Return line numbers of every direct call to a live-root resolver
    anywhere inside `func_node`'s own body (descends into nested if/try/for
    blocks, but ast.walk does not cross into a nested def, which is the
    "indirection" blind spot named in the module docstring).

    ``alias_map`` (from ``_resolver_aliases_in_module``) additionally matches
    a `Name`-form call whose identifier is a local alias of one of
    ``_LIVE_ROOT_RESOLVER_NAMES``, established by an ``ImportFrom`` `asname`
    in the same file -- see that function's docstring."""
    if alias_map is None:
        alias_map = {}
    linenos: list[int] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _LIVE_ROOT_RESOLVER_NAMES or name in alias_map:
                linenos.append(node.lineno)
    return linenos


def _is_resolver_call(node: ast.AST, alias_map: dict[str, str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and (_call_name(node.func) in _LIVE_ROOT_RESOLVER_NAMES or _call_name(node.func) in alias_map)
    )


def _names_assigned_from_resolver(func_node: ast.AST, alias_map: dict[str, str]) -> set[str]:
    """Every simple local name bound by a direct ``name = resolver(...)``
    assignment anywhere in ``func_node`` -- used to correlate a guard like
    ``if not root: return`` back to the ``root = resolver()`` call that fed
    it, per Finding 2's correlation requirement (see
    ``_bare_return_lines_in_guard``'s docstring)."""
    names: set[str] = set()
    for node in ast.walk(func_node):
        if (
            isinstance(node, ast.Assign)
            and _is_resolver_call(node.value, alias_map)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _guard_is_correlated_with_resolver(
    guard: ast.AST,
    parent_of: dict[int, ast.AST],
    func_node: ast.AST,
    alias_map: dict[str, str],
    resolver_bound_names: set[str],
) -> bool:
    """Is ``guard`` (the nearest enclosing `if`/`except` of a bare return)
    actually gated on a live-root resolver's own outcome, as opposed to some
    structurally unrelated early-exit that merely happens to share a
    function with a resolver call elsewhere?

    Two shapes count as correlated (the two named in Finding 2's suggested
    fix):

      - ``except`` handler: its enclosing ``try`` block's OWN body (not a
        sibling ``except``/``else``/``finally``) directly calls a resolver --
        the ``try: root = resolver() / except: return`` shape.
      - ``if`` guard: its test expression either calls a resolver directly
        (``if not resolver(): return``) or references a name previously
        assigned from a resolver call (``root = resolver(); if not root:
        return``), via ``_names_assigned_from_resolver``.

    A bare return in an unrelated ``if``/``except`` -- e.g. a Windows-skip
    guard in a function that also happens to call a resolver elsewhere --
    is correlated with neither shape and is therefore NOT flagged."""
    if isinstance(guard, ast.ExceptHandler):
        ancestor = parent_of.get(id(guard))
        while ancestor is not None and ancestor is not func_node:
            if isinstance(ancestor, ast.Try) and any(
                _is_resolver_call(node, alias_map) for stmt in ancestor.body for node in ast.walk(stmt)
            ):
                return True
            ancestor = parent_of.get(id(ancestor))
        return False
    if isinstance(guard, ast.If):
        for node in ast.walk(guard.test):
            if _is_resolver_call(node, alias_map):
                return True
            if isinstance(node, ast.Name) and node.id in resolver_bound_names:
                return True
        return False
    return False


def _bare_return_lines_in_guard(func_node: ast.AST, alias_map: dict[str, str] | None = None) -> list[int]:
    """Return line numbers of every bare `return` (no value) whose nearest
    enclosing compound statement, WITHIN this function, is an `if` or
    `except` block THAT IS ITSELF CORRELATED with a live-root resolver call
    -- the exact shape `a4ef240b` found and fixed:

        try:
            root = resolver()
        except RuntimeError:
            return
        if not root:
            return

    A bare `return` that is a direct statement of the function body itself
    (no enclosing if/except) is NOT flagged by this helper -- that shape is
    an unconditional early-exit, not a resolver-failure bail. Nor is a bare
    return inside an if/except that is NOT correlated with a resolver call
    (see ``_guard_is_correlated_with_resolver`` -- Finding 2 of the
    2026-07-27 review named the un-correlated version of this check as a
    false-positive risk: a function that calls a resolver AND separately has
    an unrelated early-exit guard, e.g. a Windows-skip, would otherwise be
    flagged even though the two are structurally unconnected).
    """
    if alias_map is None:
        alias_map = {}
    parent_of: dict[int, ast.AST] = {}
    for parent in ast.walk(func_node):
        for child in ast.iter_child_nodes(parent):
            parent_of[id(child)] = parent

    resolver_bound_names = _names_assigned_from_resolver(func_node, alias_map)

    guarded_returns: list[int] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value is None:
            ancestor = parent_of.get(id(node))
            while ancestor is not None and ancestor is not func_node:
                if isinstance(ancestor, (ast.If, ast.ExceptHandler)):
                    if _guard_is_correlated_with_resolver(
                        ancestor, parent_of, func_node, alias_map, resolver_bound_names
                    ):
                        guarded_returns.append(node.lineno)
                    break
                ancestor = parent_of.get(id(ancestor))
    return guarded_returns


def _has_real_home_marker(func_node: ast.FunctionDef | ast.AsyncFunctionDef, class_node: ast.ClassDef | None) -> bool:
    def _decorator_is_real_home(dec: ast.expr) -> bool:
        # `@pytest.mark.real_home` (Attribute chain) or
        # `@pytest.mark.real_home(...)` (Call wrapping that same chain).
        target = dec.func if isinstance(dec, ast.Call) else dec
        return isinstance(target, ast.Attribute) and target.attr == "real_home"

    for dec in func_node.decorator_list:
        if _decorator_is_real_home(dec):
            return True
    if class_node is not None:
        for dec in class_node.decorator_list:
            if _decorator_is_real_home(dec):
                return True
    return False


def find_silent_bail_violations(scan_root: Path) -> list[tuple[str, str, int]]:
    """Walk `scan_root` for `test_*.py` files and return one
    (relpath, test_function_name, first_offending_lineno) tuple per test
    function that resolves a live root and can bail via a bare `return`
    inside an if/except guard, without carrying `@pytest.mark.real_home`.

    Used both against the real `coordinator_core/` tree (the standing gate)
    and against an isolated tmp_path fixture (this gate's own self-test,
    proving it actually detects rather than merely passing by absence).

    FAILS LOUD (raises RuntimeError) if `scan_root` does not exist or if
    zero `test_*.py` files are found under it -- a vacuity check on a gate
    about vacuity must itself refuse to report clean on an empty sweep. See
    `test_meta_gate_fails_loud_on_empty_corpus` below for the standing proof.
    """
    if not scan_root.is_dir():
        raise RuntimeError(
            f"no-silent-bail meta-test: scan root does not exist: {scan_root}"
        )

    test_files = sorted(scan_root.rglob("test_*.py"))
    if not test_files:
        raise RuntimeError(
            f"no-silent-bail meta-test: zero test_*.py files found under {scan_root} -- "
            "a meta-test against vacuity that scanned nothing is itself vacuous. "
            "This is a checker bug (wrong scan_root, or the corpus moved), not a clean result."
        )

    violations: list[tuple[str, str, int]] = []
    parsed_any = False
    for path in test_files:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise RuntimeError(
                f"no-silent-bail meta-test: failed to parse {path} -- fix the syntax error "
                f"or this gate cannot see into the file at all: {exc}"
            ) from exc
        parsed_any = True

        relpath = _repo_relpath(path, scan_root)
        alias_map = _resolver_aliases_in_module(tree)

        # class_of[id(func_node)] -> enclosing ClassDef, for class-level marker lookup.
        class_of: dict[int, ast.ClassDef | None] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        class_of[id(child)] = node

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue

            resolver_hits = _calls_live_root_resolver(node, alias_map)
            if not resolver_hits:
                continue

            guarded_returns = _bare_return_lines_in_guard(node, alias_map)
            if not guarded_returns:
                continue

            if _has_real_home_marker(node, class_of.get(id(node))):
                continue

            violations.append((relpath, node.name, min(guarded_returns)))

    if not parsed_any:
        # Unreachable given the test_files non-empty check above (every listed
        # file either parses or raises), retained as an explicit belt-and-braces
        # vacuity guard rather than trusting the earlier check alone.
        raise RuntimeError(
            f"no-silent-bail meta-test: found {len(test_files)} test_*.py file(s) under "
            f"{scan_root} but parsed none of them -- checker bug."
        )

    return violations


def test_no_silent_bail_on_live_root_resolver_in_suite():
    """Standing gate: every `test_*` function in this suite that resolves a
    live repo root either carries `@pytest.mark.real_home` (runs for real)
    or bails via something other than a bare if/except `return` (a visible
    `pytest.skip`, or no bail at all). See module docstring for the full
    defect class and the resolver list this checks.
    """
    violations = find_silent_bail_violations(_SCAN_ROOT)
    assert violations == [], (
        "Found live-root-resolver test(s) that can silently PASS instead of asserting "
        "(bare `return` inside an if/except guard, no @pytest.mark.real_home): "
        f"{violations}\n"
        "Fix: add @pytest.mark.real_home (if the test is a read-only oracle against the "
        "live tree and should actually run), or replace the bare `return` with "
        "`pytest.skip(reason)` (if it should stay a visible non-assertion)."
    )


def test_gate_detects_a_planted_silent_bail(tmp_path):
    """Proves the gate has teeth: a planted test with the exact `a4ef240b`
    shape (resolver call in a `try`, bare `return` in the `except`) must be
    flagged. Without this, the gate widening would be passing by absence."""
    fixture = tmp_path / "test_fixture_planted_silent_bail.py"
    fixture.write_text(
        "from coordinator_core.plugin_health.relocation_ledger import default_ledger_path\n"
        "\n"
        "def test_planted_silent_bail():\n"
        "    try:\n"
        "        ledger_path = default_ledger_path()\n"
        "    except RuntimeError:\n"
        "        return\n"
        "    assert ledger_path.is_file()\n",
        encoding="utf-8",
    )

    violations = find_silent_bail_violations(tmp_path)

    assert len(violations) == 1
    relpath, func_name, lineno = violations[0]
    assert relpath.endswith("test_fixture_planted_silent_bail.py")
    assert func_name == "test_planted_silent_bail"
    assert lineno == 7


def test_gate_detects_aliased_import_silent_bail(tmp_path):
    """Proves Finding 1's fix: a resolver reached only through an aliased
    import (the exact ``test_fleet_reachability.py`` shape --
    ``from ... import registry_get as real_registry_get``) is detected, not
    invisible to the gate merely because the local name differs from the
    resolver's real name. Without ``_resolver_aliases_in_module``, this
    fixture's bare `return` would pass silently, which is exactly the
    coverage gap Finding 1 named."""
    fixture = tmp_path / "test_fixture_aliased_import_silent_bail.py"
    fixture.write_text(
        "from coordinator_core.machine_resolver import registry_get as real_registry_get\n"
        "\n"
        "def test_aliased_silent_bail():\n"
        "    if not real_registry_get('repos.doe_claude'):\n"
        "        return\n"
        "    assert True\n",
        encoding="utf-8",
    )

    violations = find_silent_bail_violations(tmp_path)

    assert len(violations) == 1
    relpath, func_name, lineno = violations[0]
    assert relpath.endswith("test_fixture_aliased_import_silent_bail.py")
    assert func_name == "test_aliased_silent_bail"
    assert lineno == 5


def test_gate_accepts_real_home_marker(tmp_path):
    """The same resolver-call-then-guard-return shape, but marked
    `@pytest.mark.real_home`, must NOT be flagged -- that is the correct
    resolution (the test actually runs against the live tree)."""
    fixture = tmp_path / "test_fixture_marked_real_home.py"
    fixture.write_text(
        "import pytest\n"
        "from coordinator_core.plugin_health.relocation_ledger import default_ledger_path\n"
        "\n"
        "@pytest.mark.real_home\n"
        "def test_marked_real_home():\n"
        "    try:\n"
        "        ledger_path = default_ledger_path()\n"
        "    except RuntimeError:\n"
        "        return\n"
        "    assert ledger_path.is_file()\n",
        encoding="utf-8",
    )

    violations = find_silent_bail_violations(tmp_path)

    assert violations == []


def test_gate_accepts_pytest_skip(tmp_path):
    """The same resolver-call shape, bailing via `pytest.skip(...)` instead
    of a bare `return`, must NOT be flagged -- a skip is visible and honest,
    which is the other correct resolution named in the module docstring."""
    fixture = tmp_path / "test_fixture_uses_skip.py"
    fixture.write_text(
        "import pytest\n"
        "from coordinator_core.plugin_health.relocation_ledger import default_ledger_path\n"
        "\n"
        "def test_uses_skip():\n"
        "    try:\n"
        "        ledger_path = default_ledger_path()\n"
        "    except RuntimeError as exc:\n"
        "        pytest.skip(f'root unresolvable: {exc}')\n"
        "    assert ledger_path.is_file()\n",
        encoding="utf-8",
    )

    violations = find_silent_bail_violations(tmp_path)

    assert violations == []


def test_gate_ignores_unrelated_guard_in_function_that_also_calls_resolver(tmp_path):
    """Finding 2's fix: a bare `return` inside an `if` guard that is
    structurally UNRELATED to a resolver call elsewhere in the same
    function -- e.g. a Windows-skip guard -- must not be flagged merely
    because the function also happens to call a resolver. Before
    `_guard_is_correlated_with_resolver`, the mere co-presence of a resolver
    call and a guarded bare return anywhere in the function was sufficient,
    which is exactly the false-positive risk Finding 2 named."""
    fixture = tmp_path / "test_fixture_unrelated_guard.py"
    fixture.write_text(
        "import platform\n"
        "from coordinator_core.plugin_health.relocation_ledger import default_ledger_path\n"
        "\n"
        "def test_unrelated_guard():\n"
        "    ledger_path = default_ledger_path()\n"
        "    if platform.system() == 'Windows':\n"
        "        return\n"
        "    assert ledger_path.is_file()\n",
        encoding="utf-8",
    )

    violations = find_silent_bail_violations(tmp_path)

    assert violations == []


def test_gate_ignores_unconditional_bare_return_with_no_guard(tmp_path):
    """A bare `return` that is a direct statement of the function body (not
    inside an if/except) is a plain early-exit, not a resolver-failure bail
    -- this shape must not be flagged even though the function also calls a
    resolver, since the two are structurally unrelated in this fixture."""
    fixture = tmp_path / "test_fixture_unconditional_return.py"
    fixture.write_text(
        "from coordinator_core.plugin_health.relocation_ledger import default_ledger_path\n"
        "\n"
        "def test_unconditional_return():\n"
        "    default_ledger_path()\n"
        "    return\n",
        encoding="utf-8",
    )

    violations = find_silent_bail_violations(tmp_path)

    assert violations == []


def test_meta_gate_fails_loud_on_empty_corpus(tmp_path):
    """Counter-property (fleet authoring assertion #3, per this module's own
    subject matter): a meta-test against vacuity that itself passes when it
    scanned zero files would be useless. An empty directory must raise, not
    report a clean [] result."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    with pytest.raises(RuntimeError, match="zero test_\\*.py files"):
        find_silent_bail_violations(empty_dir)


def test_meta_gate_fails_loud_on_missing_scan_root(tmp_path):
    missing = tmp_path / "does-not-exist"

    with pytest.raises(RuntimeError, match="does not exist"):
        find_silent_bail_violations(missing)
