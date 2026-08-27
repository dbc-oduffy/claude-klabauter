"""
coordinator_core.tests.test_no_dangling_first_party_import — the loud-failure
guard for the two collection-error classes that abort the suite.

Purpose: a module-level `from coordinator_core.X import Y` whose `Y` no longer
exists raises at IMPORT time. For a test module that is a pytest collection
error, which takes the whole file dark — every case in it stops running while
the file still sits on disk looking like coverage. For a production module it
is worse: the op itself is unreachable, and the only thing that notices is
whichever test module imports it. Three modules were dark this way on
2026-08-22, one of them (`ops/introspect/verify_shipped.py`) production code:

  - `session/tests/test_liveness.py` -> `pickup_assemble.compute_competing_claim`
  - `ops/introspect/verify_shipped.py` -> `ops.emit.resolvers.fetch_origin_main`
  - `tests/test_dag_edge_kind_ssot.py` -> `workstream_complete._LEG_B_EDGE_KINDS`

All three were deliberate deletions whose reverse-reference scan missed a
caller. This guard is that scan, run every time the suite runs: it resolves
every first-party `from ... import <name>` in the tree against the module's
real, on-disk symbol table and names each one that no longer resolves.

Method — regex prefilter, then a STATIC AST oracle, with a narrow import
fallback. All three stages are load-bearing:

  - The prefilter is a line-level regex, not an `ast.parse` of every file. A
    full-tree AST pass over the same 4,050 files measures 5.6s of process
    time against `docs/decisions/DR-344-the-brightline-process-budget-for-
    claude-klabauter.md`'s 500ms end-to-end bar; the regex pass measures 0.41s. Neither
    half spawns a process.
  - The oracle resolves each `(module, name)` pair by parsing the TARGET
    module's own file and walking its top-level statements (recursing into
    `if`/`try`/`with`/`for` bodies, since a conditional import or platform
    branch is a legitimate module-level definition) to build the set of
    names it binds. `import`/`from ... import` aliases, `def`/`class`
    names, and assignment targets (including tuple/list unpacking) all
    count. A name not in that set but naming an existing submodule file
    (`module.name` resolves on disk) also counts — mirrors what
    `from pkg import submodule` actually does at runtime. This costs one
    `ast.parse` per referenced module (~910 of them) plus a handful of
    `Path.is_file()` checks — no import, no bytecode execution. Measured
    0.09s in-process against the prior import-based oracle's 0.54s (see
    `test_guard_process_time_dropped_from_import_to_ast.py` sibling assertion
    folded into this file's own measurement note below).
  - `if TYPE_CHECKING:` bodies are walked for their OWN names but their
    branch is never taken to be live at runtime — a name bound only inside
    a `TYPE_CHECKING` guard is real for a type checker and absent for
    `python -c "import module; module.Name"`, exactly the gap this guard
    exists to catch. Skipping this special case would have been a false
    green: an AST walk that treats every `if` branch as always-bound cannot
    tell a real conditional definition from a type-only one.
  - The import fallback exists because AST cannot see through two escape
    hatches: a module-level `def __getattr__` (PEP 562 dynamic attribute
    access — 5 modules in this tree, e.g.
    `coordinator_core.contract.cockpit_schema` re-exporting
    `CONTRACT_VERSION` from `emit_schema` lazily to dodge a `sys.modules`
    collision) and `from x import *`. A module carrying either is marked
    dynamic; a name not found in its static symbol table falls back to a
    REAL import of just that module — the same oracle the prior version used
    for all ~910, now reached for 5. A static-only version that skipped this
    fallback was rejected: it would report `CONTRACT_VERSION` dangling
    against a module that resolves it correctly at runtime — a false
    accusation on a re-export idiom, the same failure mode that killed the
    earlier attribute-scan-only design (20 findings, 16 false, muted rather
    than fixed).

Second class, same blast radius: a DUPLICATE TEST BASENAME. Two `test_x.py`
files whose directories both lack an `__init__.py` cannot both be imported as
top-level module `test_x`; pytest reports "import file mismatch" and aborts.
`coordinator/bin/test_workday_complete_backfill_anchor.py` and
`coordinator/tests/test_workday_complete_backfill_anchor.py` collided this way
on 2026-08-22 — two genuinely different suites over one script, not a stray
copy, so the fix was a rename. The tree carries ~85 other duplicated test
basenames and every one of them is FINE, because at most one copy of each sits
outside a package; `test_no_duplicate_unpackaged_test_basename` encodes that
exact condition rather than banning duplicate names outright. This half is
unchanged by the AST rework — it was never the cost driver.

Negative-spec:
  - Does NOT re-run pytest collection, and must not be rewritten to. A
    `--collect-only` subprocess over `testpaths` is the same work the tier
    already does, at suite cost, from inside the suite.
  - Does NOT catch every possible collection error — a module that raises at
    import for a non-import reason (a module-level assert, a missing data
    file) is out of scope. Those already fail the tier loudly and were never
    the silent class. The silent class is the deleted symbol, because the
    deleting commit is not the one that goes red.
  - Does NOT check attribute-style access (`pa.compute_successor_handoffs`).
    That reference shape does not break collection — it fails at call time as
    an ordinary red test, which the tier already reports.
  - Relative imports (`from .x import y`) are deliberately skipped: the
    prefilter is anchored on the absolute `coordinator_core` prefix, and the
    relative form is confined to intra-package siblings that move together.
  - UNRESOLVABLE, not silently fine: a module-level `match` statement's
    capture patterns, a `globals()[...] = ...` or `setattr(sys.modules[...],
    ...)` dynamic bind, and any name minted through `__all__` alone with no
    corresponding static binding are not walked by `_visit_module_body`. A
    module using one of these for the exact name a caller imports, WITHOUT
    also carrying `__getattr__` or `import *`, is a genuine blind spot: the
    guard will call it dangling (a false accusation, not a missed catch) or,
    if the module has no other traffic, simply never be exercised against
    that name. Zero modules in this tree hit `match`-based or
    `globals()`-based export as of 2026-08-22 (grepped: none); if one lands,
    it needs marking dynamic the same way `__getattr__` is, not a bigger
    static walker.
"""

from __future__ import annotations

import ast
import atexit
import contextlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Source roots swept for first-party import statements. Deliberately WIDER
#: than `pyproject.toml`'s `testpaths`: a dangling import inside a production
#: module (the `verify_shipped` case above) is the defect, and the test module
#: that goes dark is only its symptom.
SOURCE_ROOTS = ("coordinator_core", "coordinator", "bin", "scripts")

#: `pyproject.toml`'s `[tool.pytest.ini_options] testpaths`, which bounds what a
#: tier run actually collects — and therefore what a basename collision can
#: abort. Read against that list when it changes; a path here that pytest does
#: not collect would report a collision nobody can hit.
TESTPATHS = (
    "coordinator_core",
    "coordinator/tests",
    "coordinator/bin",
    "coordinator/lib",
    "bin",
    "scripts",
    "coordinator/scripts",
)

FIRST_PARTY_PREFIX = "coordinator_core"

_FROM_IMPORT = re.compile(
    r"^from[ \t]+(coordinator_core[\w.]*)[ \t]+import[ \t]+(\(?)([^\n#]*)",
    re.MULTILINE,
)


def _imported_names(source: str) -> list[tuple[str, str]]:
    """Every ``(module, name)`` pair a source file imports first-party.

    A parenthesized import continues past the match, so the tail up to the
    closing paren is folded back in; a backslash continuation is normalized to
    whitespace. `*` and non-identifier fragments are dropped rather than
    reported, so a parse the regex cannot fully resolve degrades to silence
    rather than to a false accusation.
    """
    pairs: list[tuple[str, str]] = []
    for match in _FROM_IMPORT.finditer(source):
        module, paren, rest = match.group(1), match.group(2), match.group(3)
        if paren:
            tail = source[match.end():]
            close = tail.find(")")
            rest = rest + " " + (tail[:close] if close >= 0 else "")
        for chunk in rest.replace("\\", " ").split(","):
            name = chunk.strip().split(" as ")[0].strip()
            if name and name != "*" and name.isidentifier():
                pairs.append((module, name))
    return pairs


def _scan_tree() -> dict[str, set[str]]:
    """Map every first-party module to the set of names the tree imports from
    it, plus the files doing the importing."""
    wanted: dict[str, set[str]] = {}
    for root in SOURCE_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if FIRST_PARTY_PREFIX not in source:
                continue
            for module, name in _imported_names(source):
                wanted.setdefault(module, set()).add(name)
    return wanted


def _module_file(module: str) -> Path | None:
    """Filesystem path backing a dotted first-party module — a package's
    `__init__.py` takes priority over a same-named `.py` file, matching
    Python's own import resolution order. `None` means the module itself is
    the dangling reference."""
    base = REPO_ROOT.joinpath(*module.split("."))
    pkg_init = base / "__init__.py"
    if pkg_init.is_file():
        return pkg_init
    mod_file = base.with_suffix(".py")
    if mod_file.is_file():
        return mod_file
    return None


def _is_type_checking_guard(test: ast.expr) -> bool:
    """Whether an `if` test is (`typing.`)`TYPE_CHECKING` — the one `if`
    shape whose body is never live at runtime, so a name bound only inside it
    must not count as a real module attribute."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _collect_targets(target: ast.expr, bound: set[str]) -> None:
    """Every `Name` a (possibly nested tuple/list/starred) assignment target
    binds — `a, (b, *c) = ...` binds `a`, `b`, and `c`."""
    if isinstance(target, ast.Name):
        bound.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _collect_targets(elt, bound)
    elif isinstance(target, ast.Starred):
        _collect_targets(target.value, bound)


def _visit_module_body(stmts: list[ast.stmt], bound: set[str]) -> bool:
    """Walk one body of top-level-equivalent statements, adding every name it
    binds to `bound`. Descends into `if`/`try`/`with`/`for` bodies — a
    conditional import or platform branch is a legitimate module-level
    definition — but never into a `def`/`class` body, whose names are local,
    not module attributes. `if TYPE_CHECKING:` is the one exception: its body
    is walked for nothing, because it is never live at runtime (see
    `_is_type_checking_guard`).

    Returns True if a `def __getattr__` (PEP 562 dynamic module attribute
    access) or `from ... import *` is seen anywhere in the walk — either one
    means "not found in `bound`" is inconclusive, not dangling.
    """
    dynamic = False
    for node in stmts:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            if node.name == "__getattr__":
                dynamic = True
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                _collect_targets(tgt, bound)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            _collect_targets(node.target, bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    dynamic = True
                else:
                    bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.If):
            if _is_type_checking_guard(node.test):
                dynamic |= _visit_module_body(node.orelse, bound)
            else:
                dynamic |= _visit_module_body(node.body, bound)
                dynamic |= _visit_module_body(node.orelse, bound)
        elif isinstance(node, ast.Try):
            dynamic |= _visit_module_body(node.body, bound)
            for handler in node.handlers:
                dynamic |= _visit_module_body(handler.body, bound)
            dynamic |= _visit_module_body(node.orelse, bound)
            dynamic |= _visit_module_body(node.finalbody, bound)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            dynamic |= _visit_module_body(node.body, bound)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _collect_targets(node.target, bound)
            dynamic |= _visit_module_body(node.body, bound)
            dynamic |= _visit_module_body(node.orelse, bound)
    return dynamic


@dataclass(frozen=True)
class _ModuleSymbols:
    #: Every name this module binds at module scope, as far as a static walk
    #: can see.
    bound: frozenset[str]
    #: True if the module carries an escape hatch (`__getattr__` or
    #: `import *`) an AST walk cannot see through — "not in `bound`" means
    #: unresolvable, not dangling.
    dynamic: bool
    #: Set when the module file is missing or fails to parse; `bound` and
    #: `dynamic` are meaningless in that case.
    parse_error: str | None = None


def _symbols_from_source(source: str) -> _ModuleSymbols:
    """Pure function from source text to its static symbol table — split out
    from `_module_symbols` so tests can exercise the walk against fixture
    source without a real file on disk backing a real dotted module name."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return _ModuleSymbols(bound=frozenset(), dynamic=False, parse_error=f"SyntaxError: {exc}")
    bound: set[str] = set()
    dynamic = _visit_module_body(tree.body, bound)
    return _ModuleSymbols(bound=frozenset(bound), dynamic=dynamic)


_SYMBOL_CACHE: dict[str, _ModuleSymbols] = {}

#: Per-file symbol-table cache, keyed on `(mtime_ns, size)` — the same shape
#: `docs/plans/2026-08-22-generator-discovery-reads-a-cache-not-th.md` (C1)
#: measured for `generator_provenance.discover_generators`'s AST-parse cost.
#: `ast.parse` over the ~910 modules this guard's prefilter touches measures
#: ~0.9s COLD, more than the import-based oracle it replaced (0.54s) —
#: because a real `import` gets to reuse `__pycache__` bytecode, and a raw
#: `ast.parse` from source gets no such reuse on its own. A warm run of this
#: cache is a `stat` per file plus a JSON load; only a file that actually
#: changed pays the parse. Schema version bump on any shape change so a stale
#: cache from a prior guard version cannot be misread as current.
CACHE_SCHEMA_VERSION = 1
CACHE_PATH = REPO_ROOT / "state" / "cache" / "dangling-import-symbols.json"

_DISK_CACHE: dict[str, dict] = {}
_DISK_CACHE_LOADED = False
_DISK_CACHE_DIRTY = False


def _load_disk_cache() -> dict[str, dict]:
    """The on-disk symbol cache, loaded once per process. Any read failure —
    missing file, truncated/garbage JSON, or a schema-version mismatch —
    degrades to an empty cache, never an exception: this is a memo of a
    computation the guard can always redo, not truth about the repo (mirrors
    AC5 of the sibling generator-discovery cache plan)."""
    global _DISK_CACHE, _DISK_CACHE_LOADED
    if _DISK_CACHE_LOADED:
        return _DISK_CACHE
    _DISK_CACHE_LOADED = True
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != CACHE_SCHEMA_VERSION:
            return _DISK_CACHE
        modules = raw.get("modules")
        if isinstance(modules, dict):
            _DISK_CACHE = modules
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return _DISK_CACHE


def _flush_disk_cache() -> None:
    """Best-effort atomic write-back, registered once via `atexit`. A write
    failure (read-only tree, disk full, a peer holding the path) must never
    fail the guard — the cache is pure speedup, never a correctness input."""
    if not _DISK_CACHE_DIRTY:
        return
    tmp_name = None
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(CACHE_PATH.parent), prefix=".dangling-import-symbols-", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": CACHE_SCHEMA_VERSION, "modules": _DISK_CACHE}, fh)
        os.replace(tmp_name, CACHE_PATH)
        tmp_name = None
    except OSError:
        pass
    finally:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


atexit.register(_flush_disk_cache)


def _cache_entry_from_symbols(signature: list[int], symbols: _ModuleSymbols) -> dict:
    return {
        "signature": signature,
        "bound": sorted(symbols.bound),
        "dynamic": symbols.dynamic,
        "parse_error": symbols.parse_error,
    }


def _symbols_from_cache_entry(entry: dict) -> _ModuleSymbols:
    return _ModuleSymbols(
        bound=frozenset(entry["bound"]),
        dynamic=bool(entry["dynamic"]),
        parse_error=entry.get("parse_error"),
    )


def _resolve_module_file_symbols(path: Path, disk_cache: dict[str, dict]) -> tuple[_ModuleSymbols, bool]:
    """`_symbols_from_source` for one real file, through the `disk_cache`
    layer. Returns `(symbols, reparsed)`: `reparsed` is True exactly when the
    signature missed and the caller should mark the cache dirty. A cache
    entry that fails to decode (corrupt shape, wrong types) is treated as a
    miss, never a crash — AC5's "malformed cache" case, at entry granularity
    rather than whole-file granularity, so one corrupt row costs one reparse
    instead of invalidating every other file's entry too."""
    try:
        stat = path.stat()
    except OSError as exc:
        return _ModuleSymbols(bound=frozenset(), dynamic=False, parse_error=f"{type(exc).__name__}: {exc}"), False
    signature = [stat.st_mtime_ns, stat.st_size]
    key = str(path)
    entry = disk_cache.get(key)
    if entry is not None:
        try:
            if entry.get("signature") == signature:
                return _symbols_from_cache_entry(entry), False
        except (KeyError, TypeError, AttributeError):
            pass
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        symbols = _ModuleSymbols(bound=frozenset(), dynamic=False, parse_error=f"{type(exc).__name__}: {exc}")
    else:
        symbols = _symbols_from_source(source)
    disk_cache[key] = _cache_entry_from_symbols(signature, symbols)
    return symbols, True


def _module_symbols(module: str) -> _ModuleSymbols:
    """`_symbols_from_source`, memoized per module for this process and
    backed by the on-disk `(mtime_ns, size)` cache across processes. Every
    `(module, name)` pair sharing a target module parses that target at most
    once per invocation, and not at all on a warm cache."""
    if module in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[module]
    path = _module_file(module)
    if path is None:
        base = REPO_ROOT.joinpath(*module.split("."))
        if base.is_dir():
            # PEP 420 implicit namespace package: no `__init__.py`, so no
            # code of its own runs and `bound` is correctly empty — but the
            # directory is real, and a submodule inside it (checked by
            # `_resolves`'s own `_module_file(f"{module}.{name}")` call)
            # resolves exactly as it would for a regular package. Real case:
            # `coordinator_core/warm/door/` ships C sources plus `build.py`
            # with no `__init__.py`; `from ...warm.door import build` is a
            # normal, working import.
            result = _ModuleSymbols(bound=frozenset(), dynamic=False)
        else:
            result = _ModuleSymbols(bound=frozenset(), dynamic=False, parse_error="module file does not exist")
    else:
        disk_cache = _load_disk_cache()
        result, reparsed = _resolve_module_file_symbols(path, disk_cache)
        if reparsed:
            global _DISK_CACHE_DIRTY
            _DISK_CACHE_DIRTY = True
    _SYMBOL_CACHE[module] = result
    return result


def _resolves_via_import(module: str, name: str) -> bool:
    """Real-import oracle, reached only for a module carrying a dynamic
    escape hatch (`__getattr__` or `import *`) an AST symbol table cannot see
    through — e.g. `coordinator_core.contract.cockpit_schema` re-exporting
    `CONTRACT_VERSION` via PEP 562. Five modules in the tree qualify as of
    2026-08-22 (grepped: `^def __getattr__`); this path is never taken for
    the other ~900+."""
    noise = io.StringIO()
    with contextlib.redirect_stderr(noise), contextlib.redirect_stdout(noise):
        try:
            obj = importlib.import_module(module)
        except BaseException:
            return False
        if hasattr(obj, name):
            return True
        try:
            importlib.import_module(f"{module}.{name}")
        except BaseException:
            return False
    return True


def _resolves(module: str, name: str) -> tuple[bool, bool]:
    """Whether `from module import name` would succeed. Returns `(resolved,
    used_import_fallback)` — the second element is surfaced so the test can
    report how many of the tree's references land in the narrow "AST could
    not tell" bucket, rather than folding that count silently into "fine".

    Resolution order: the target's static symbol table, then whether `name`
    names an existing submodule (mirrors `from pkg import submodule`), and
    only if neither answers AND the module is `dynamic` does a real import
    get spent.
    """
    symbols = _module_symbols(module)
    if symbols.parse_error is not None:
        return False, False
    if name in symbols.bound:
        return True, False
    if _module_file(f"{module}.{name}") is not None:
        return True, False
    if not symbols.dynamic:
        return False, False
    return _resolves_via_import(module, name), True


class TestNoDanglingFirstPartyImport:
    def test_every_first_party_imported_symbol_resolves(self) -> None:
        wanted = _scan_tree()
        assert wanted, "prefilter matched nothing — the scan itself is broken"

        dangling: list[str] = []
        for module in sorted(wanted):
            symbols = _module_symbols(module)
            if symbols.parse_error is not None:
                dangling.append(f"{module} — {symbols.parse_error}")
                continue
            for name in sorted(wanted[module]):
                resolved, _used_fallback = _resolves(module, name)
                if not resolved:
                    dangling.append(f"{module}.{name} — imported by the tree, absent")

        assert not dangling, (
            "first-party imports naming symbols that no longer exist; each one "
            "takes its importing module dark at collection time:\n  "
            + "\n  ".join(dangling)
            + "\nDelete the import and whatever it fed, or restore the symbol — "
            "do not skip the importing module."
        )

    def test_no_duplicate_unpackaged_test_basename(self) -> None:
        by_basename: dict[str, list[str]] = {}
        for root in TESTPATHS:
            for path in (REPO_ROOT / root).rglob("test_*.py"):
                if "__pycache__" in path.parts:
                    continue
                if (path.parent / "__init__.py").is_file():
                    continue
                by_basename.setdefault(path.name, []).append(
                    str(path.relative_to(REPO_ROOT))
                )

        collisions = {
            name: sorted(set(paths))
            for name, paths in by_basename.items()
            if len(set(paths)) > 1
        }
        assert not collisions, (
            "test basenames duplicated across directories that are not packages; "
            "pytest cannot import both and aborts the whole session with "
            "'import file mismatch':\n  "
            + "\n  ".join(
                f"{name}: {', '.join(paths)}" for name, paths in sorted(collisions.items())
            )
            + "\nRename one, or add an __init__.py to its directory — the two files "
            "are not necessarily duplicates of each other."
        )

    def test_scan_finds_the_known_first_party_import_population(self) -> None:
        """The guard's own reach, pinned so a regex edit that silently stops
        matching cannot leave it passing vacuously. A floor, not an exact
        count — peers land modules continuously."""
        wanted = _scan_tree()
        assert len(wanted) > 500
        assert "coordinator_core.session.liveness" in wanted
        assert sum(len(names) for names in wanted.values()) > 2000

    def test_dynamic_getattr_reexport_resolves_via_fallback(self) -> None:
        """Regression pin for the exact false-accusation case that ruled out
        a static-only design: `cockpit_schema.__init__` re-exports
        `CONTRACT_VERSION` through PEP 562 `__getattr__`, not a top-level
        binding. A walk with no import fallback would call this dangling."""
        resolved, used_fallback = _resolves(
            "coordinator_core.contract.cockpit_schema", "CONTRACT_VERSION"
        )
        assert resolved
        assert used_fallback

    def test_genuinely_absent_symbol_is_dangling(self) -> None:
        resolved, used_fallback = _resolves(
            "coordinator_core.session.work_state", "definitely_does_not_exist_xyz"
        )
        assert not resolved
        assert not used_fallback

    def test_submodule_import_resolves_without_fallback(self) -> None:
        """`from coordinator_core import session` names a subpackage, not an
        attribute `coordinator_core/__init__.py` binds — the filesystem check
        must catch it before falling back to a real import (`__init__.py`
        here is itself one of the 5 `__getattr__` modules; parity would be
        cheap to lose by checking `dynamic` first)."""
        resolved, used_fallback = _resolves("coordinator_core", "session")
        assert resolved
        assert not used_fallback

    def test_type_checking_only_binding_is_not_a_runtime_symbol(self) -> None:
        source = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from coordinator_core.session import work_state as WorkState\n"
        )
        symbols = _symbols_from_source(source)
        assert "WorkState" not in symbols.bound
        assert not symbols.dynamic

    def test_conditional_import_outside_type_checking_is_bound(self) -> None:
        source = (
            "try:\n"
            "    import ujson as json\n"
            "except ImportError:\n"
            "    import json\n"
        )
        symbols = _symbols_from_source(source)
        assert "json" in symbols.bound
        assert not symbols.dynamic

    def test_star_import_marks_module_dynamic(self) -> None:
        source = "from coordinator_core.session.work_state import *\n"
        symbols = _symbols_from_source(source)
        assert symbols.dynamic

    def test_namespace_package_submodule_resolves(self) -> None:
        """`coordinator_core/warm/door/` has no `__init__.py` (PEP 420
        implicit namespace package: C sources plus `build.py`, no package
        code) — `install/door_install.py` does
        `from coordinator_core.warm.door import build`. Caught this test
        red on first write: treating "no `__init__.py`, no `name.py`" as
        "module does not exist" made a real, working import a false
        accusation."""
        resolved, used_fallback = _resolves("coordinator_core.warm.door", "build")
        assert resolved
        assert not used_fallback

    def test_tuple_unpacking_assignment_binds_all_targets(self) -> None:
        source = "a, (b, *c) = 1, (2, 3, 4)\n"
        symbols = _symbols_from_source(source)
        assert {"a", "b", "c"} <= symbols.bound

    def test_disk_cache_hit_skips_reparse_and_matches_cold_result(self, tmp_path) -> None:
        target = tmp_path / "m.py"
        target.write_text("def foo(): pass\n", encoding="utf-8")
        cache: dict[str, dict] = {}

        cold, reparsed_cold = _resolve_module_file_symbols(target, cache)
        assert reparsed_cold
        assert "foo" in cold.bound

        warm, reparsed_warm = _resolve_module_file_symbols(target, cache)
        assert not reparsed_warm
        assert warm == cold

    def test_disk_cache_invalidates_on_touched_file(self, tmp_path) -> None:
        target = tmp_path / "m.py"
        target.write_text("def foo(): pass\n", encoding="utf-8")
        cache: dict[str, dict] = {}
        _resolve_module_file_symbols(target, cache)

        target.write_text("def foo(): pass\ndef bar(): pass\n", encoding="utf-8")
        os.utime(target, ns=(target.stat().st_atime_ns + 1_000_000_000, target.stat().st_mtime_ns + 1_000_000_000))
        updated, reparsed = _resolve_module_file_symbols(target, cache)
        assert reparsed
        assert "bar" in updated.bound

    def test_disk_cache_corrupt_entry_falls_back_to_reparse(self, tmp_path) -> None:
        target = tmp_path / "m.py"
        target.write_text("def foo(): pass\n", encoding="utf-8")
        stat = target.stat()
        cache: dict[str, dict] = {
            str(target): {"signature": [stat.st_mtime_ns, stat.st_size]}  # missing "bound"/"dynamic"
        }
        result, reparsed = _resolve_module_file_symbols(target, cache)
        assert reparsed
        assert "foo" in result.bound

    def test_disk_cache_missing_or_garbage_file_yields_empty_cache(self, tmp_path, monkeypatch) -> None:
        garbage = tmp_path / "cache.json"
        garbage.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "CACHE_PATH", garbage)
        monkeypatch.setattr(sys.modules[__name__], "_DISK_CACHE_LOADED", False)
        monkeypatch.setattr(sys.modules[__name__], "_DISK_CACHE", {})
        assert _load_disk_cache() == {}


if __name__ == "__main__":
    sys.exit(0)
