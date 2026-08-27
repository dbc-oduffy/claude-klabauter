"""coordinator_core.op_census.spawn_bearing_ops — evidence: which ops spawn, and from where.

Purpose: C2 (`docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md` §C2,
`state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C2.md`) —
"THE defect this plan exists to close." `coordinator_core/tests/
test_no_uncounted_spawn_on_budgeted_path.py`'s `_BUDGETED_ENTRYPOINTS` was a
hand-maintained nine-op dict with no completeness guard: an op that spawns and
was never enrolled was not under-measured, it was invisible. This module is the
DERIVATION layer that closes that hole — resolve every op the AUTHORITATIVE
registry (`coordinator_core.ops` / `ipc._REGISTRY`) actually knows about to its
owning source module, and report which of those modules carry a recognised
spawn call site.

EVIDENCE, NEVER VERDICTS (hard constraint 9 / Finding 9). This module answers
"does this op's owning module contain a spawn site" and "does the fast-path map
agree with the live registry" — nothing here decides pass/fail, and nothing
here holds an exemption. Every pass/fail and every exemption key stays in the
gate module (`test_no_uncounted_spawn_on_budgeted_path.py`), which is exactly
why this module was renamed from the plan's working name `enrolment.py` to
`spawn_bearing_ops.py`: the filename states what it is, not what it is used to
decide.

TWO SOURCES, ONE AUTHORITATIVE (Finding 1 / PM Ruling 3-B). `ipc._REGISTRY`,
populated by importing `coordinator_core.ops`, is the fidelity-complete source
— every `@register_op` side effect that has actually run. `coordinator_core/
ops/_registry_map.py::OP_MODULE_MAP` is a hand-maintained fast path whose own
docstring disqualifies it as a sole source ("a PERFORMANCE OPTIMIZATION, not a
correctness gate ... a stale/incomplete map degrades to today's correctness").
Deriving a completeness gate from the fast path alone would relocate this
plan's own invisibility hole one layer down rather than closing it, so
`live_registry_op_names` (the authoritative read) and `fast_path_op_names` (the
optimisation) are both exposed, and `registry_divergence` is the required,
loud comparison between them — never silently reconciled here.

HARD CONSTRAINT 7 (amended), why the authoritative import is safe here.
Importing `coordinator_core.ops` costs ~343.8ms, but that cost is paid once
per interpreter/process, not per invocation (`'coordinator_core.ops' in
sys.modules` is `False` after the client's own door import finishes, per PM
Ruling 3-A). This module's own callers are the gate test (`pytest`, one
process, one call) and, later, a warm-resident or once-per-cold-run census
path — never a per-invocation hot-path caller. `live_registry_op_names` is
therefore free to trigger that import; nothing here re-imports
`coordinator_core.ops` per call (it is idempotent via `sys.modules` regardless,
but the negative-spec below states the intent plainly).

MODULE GRANULARITY BY DEFAULT, FUNCTION GRANULARITY BEHIND `function_granular=True`
(this chunk). `ops_with_spawn_evidence`'s default question is still the cheap,
coarse one: does `spawn_policy.detect.sites_in_source` find ANY recognised spawn
call site anywhere in the op's OWNING MODULE FILE (not narrowed to the
registered handler function's own body)? This is a deliberate, named
over-approximation: a module that registers several ops (e.g.
`coordinator_core.hooks`, sixteen ops in one module) will report spawn evidence
for every op it registers if the module contains a spawn site anywhere, even one
that belongs to a sibling op's own handler. That bias is intentional and matches
this plan's own stated preference (`test_no_uncounted_spawn_on_budgeted_path.py`'s
docstring: "it would rather over-report a site than silently drop one whose
gating it cannot prove") — a false positive here means one more row enters the
frozen inventory or gets enrolled; a false negative would recreate the exact
invisibility bug this chunk exists to close. This module-level lens stays the
default and stays reachable: existing callers depend on its over-approximating
bias, and it remains the safer answer when a sharper one cannot be computed.

`function_granular=True` reuses (never re-derives) the gate module's own
transitive-BFS reachability predicate — `_reachable_functions`,
`_build_corpus_with_dispatch_tables`, and `_on_path_spawn_sites` in
`coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py` — over the
same `(relpath, top-level function name)` domain that predicate already ships
for its own nine live entrypoints, widened here to every op whose entrypoint
resolves inside that gate module's own scope roots (`coordinator_core`,
`coordinator/bin`). An op whose entrypoint function is not a TOP-LEVEL
definition the reused index can see (a handler outside those scope roots, or
whose registered `__name__` does not match its own `def` site because of a
decorator that does not preserve it) reports NO function-granular evidence for
that op — a named, accepted false negative rather than a silent fall-back to
the module-level over-approximation, so that turning this flag on never
quietly mixes the two lenses within one result. Callers that need the safer
answer for such an op call `function_granular=False` (the default) instead.

`function_granular=True` inherits every accepted false-negative gap
`_reachable_functions`/`_import_function_aliases` already name in that
module's own docstring (deeper relative imports, longer alias chains) — this
is why the property this module now asks for is keyed on the `(op, site)`
PAIR, never "does some op reach this site": a site a same-module sibling op
reaches by a visible path must never stand in for "this op reaches it too."
A THIRD gap the walker's docstring did not previously name — a by-reference
dispatch table (a module-level container literal whose members are function
references, loaded by name rather than called directly, e.g.
`merge_assemble/apply.py::_CLI_DISPATCH` passed into `apply_base.
execute_directives`) — is now RESOLVED: `_module_callable_tables`/
`_direct_call_targets`'s table-load edge (D8) makes a reachable function
that LOADS such a table reach every function that table's own literal
resolves, with the same same-file-first/precise-alias resolution every
other hop in that gate uses.

Negative-spec:
    - This module holds no `_LEGITIMIZED_SITES`-shaped exemption table and no
      pass/fail assertion. Those stay in the gate module.
    - `resolve_op_entrypoints` never raises on an op whose handler cannot be
      resolved to a repo-relative, `coordinator_core`-or-`coordinator`-rooted
      source file (a C-extension, a dynamically-built closure, anything
      outside this repo's own tree) — it records the reason on the
      `OpEntrypoint` and moves on. A row this module cannot resolve is
      evidence too, not a crash.
    - No per-invocation re-import: every function here that needs the live
      registry accepts an already-populated `registry` mapping as an optional
      parameter (`resolve_op_entrypoints(..., registry=ipc._REGISTRY)`) so a
      caller that has already paid the one-time import cost never pays it
      twice; only `live_registry_op_names`/`registry_divergence`, called at
      most once per process by their own callers, trigger the import.

PERSISTED TIER (op_census/C-thread cold-path fix, additive). A COLD process
(no in-process `cache._REVALIDATED_CACHE` history) paid a full re-scan of
every recognised-spawn-bearing module on its first `ops_with_spawn_evidence`
call, ~1.3s over this repo's real op corpus -- well over the 1s kill bar
(DR-344) even though a WARM second call in the same process was ~300ms via
the in-process tier alone. `spawn_sites_by_relpath`/`ops_with_spawn_evidence`
now also accept an optional caller-owned `SpawnIndex` (`load_spawn_index` /
`save_spawn_index`), routed through `cache.read_disk_revalidated` -- the
SAME persisted, content-revalidated tier `module_summary.py` already grows
`cache.py` with (Ruling 1), not a second cache. `index=None` (the default)
is byte-for-byte the prior in-process-only behaviour; nothing here changes
`_MAX_CACHE`, `cache.read_revalidated`'s own contract, or any existing
caller's cost.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C2.md
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from typing import Callable, Dict, FrozenSet, Iterable, NamedTuple, Optional, Tuple

from coordinator_core import cache, ipc
from coordinator_core.ops import _registry_map
from coordinator_core.spawn_policy.detect import SpawnKind, SpawnParseError, SpawnSite, sites_in_source

__all__ = [
    "OpEntrypoint",
    "RegistryDivergence",
    "live_registry_op_names",
    "fast_path_op_names",
    "registry_divergence",
    "resolve_op_entrypoints",
    "spawn_sites_by_relpath",
    "ops_with_spawn_evidence",
    "load_spawn_index",
    "save_spawn_index",
]

#: `{relpath: (stamp, (SpawnSite, ...))}` -- the persisted-tier revalidation
#: shape `spawn_sites_by_relpath`/`load_spawn_index`/`save_spawn_index` share,
#: mirroring `module_summary.py`'s own `{path: (stamp, ModuleSummary)}` index
#: convention (same two-tier design: caller loads once, revalidates via
#: `cache.read_disk_revalidated`, saves back once).
SpawnIndex = Dict[str, Tuple[str, Tuple[SpawnSite, ...]]]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _relpath_under_repo_root(path: pathlib.Path) -> Optional[str]:
    """`path` as a POSIX repo-relative string, or None if it sits outside the repo.

    `Path.resolve()` is a filesystem round-trip -- on Windows one
    `nt._getfinalpathname` syscall per call, ~35us each. Calling it per module
    made it the single largest cost in the census: measured 1177 calls and
    ~112ms of a ~300ms warm run in `op_census_report.census` alone, against a
    200ms per-process bar, dwarfing the sha256 corpus read that module's budget
    table names as the dominant term.

    A module `__file__` is already absolute and already under `_REPO_ROOT` in
    the ordinary case, so `relative_to` answers without touching disk.
    `resolve()` is kept as the fallback, not dropped, and the fast leg refuses
    any result still carrying a `..` component. `relative_to` is a string
    operation and will answer `pkg/../pkg/mod.py` rather than raising, keying
    one module under two different strings; only `resolve()` collapses that.
    The caller reports a None as an unresolved entrypoint rather than crashing,
    so a fast leg that answered alone would degrade silently.

    The `..`-component check reads `relative_str.split("/")` rather than
    `relative.parts` -- profiled on the warm path, `PurePath.parts` (a
    `Sequence`-mixin object in this interpreter's pathlib, not a plain
    tuple) falls back to the ABC's `__contains__`, which walks it via
    `__iter__`/`__getitem__` and re-parses on the way; splitting the POSIX
    string already computed for the return value is the same check over a
    real `list[str]`, at native `str.split`/`in` cost (same finding as
    `op_census_report._relpath_under`, this function's sibling).
    """
    try:
        relative = path.relative_to(_REPO_ROOT)
    except ValueError:
        relative = None
    if relative is not None:
        relative_str = relative.as_posix()
        if ".." not in relative_str.split("/"):
            return relative_str
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return None


def live_registry_op_names() -> FrozenSet[str]:
    """The authoritative op-name set: `ipc._REGISTRY.keys()` after importing
    `coordinator_core.ops` (registers every op module's `@register_op` side
    effect). This is the fidelity source hard constraint 7 (amended) and PM
    Ruling 3-B require — never the fast-path map alone. Safe to call from a
    test process or a once-per-boot/once-per-cold-run caller; not safe to call
    on a per-invocation hot path (see module docstring)."""
    import coordinator_core.ops as _ops_pkg

    _ops_pkg._eager_import_all()
    return frozenset(ipc._REGISTRY.keys())


def fast_path_op_names() -> FrozenSet[str]:
    """The `_registry_map.py::OP_MODULE_MAP` op-name set — a PERFORMANCE
    OPTIMIZATION per that module's own docstring, never a sole source of
    completeness truth. Compare against `live_registry_op_names()` via
    `registry_divergence()` before trusting this for anything completeness-
    shaped."""
    return frozenset(_registry_map.OP_MODULE_MAP.keys())


class RegistryDivergence(NamedTuple):
    """`only_in_live` / `only_in_fast_path` -- both empty exactly when the two
    sources agree. A required deliverable of this chunk (plan §C2): "add a
    DIVERGENCE GUARD that fails loudly when the map-derived set and the
    live-registry-derived set disagree." This type carries the evidence; the
    gate test (`test_spawn_bearing_ops.py` /
    `test_no_uncounted_spawn_on_budgeted_path.py`) carries the assertion."""

    only_in_live: FrozenSet[str]
    only_in_fast_path: FrozenSet[str]

    @property
    def agrees(self) -> bool:
        return not self.only_in_live and not self.only_in_fast_path


def registry_divergence() -> RegistryDivergence:
    """One-shot comparison of the two op-name sources. Calls
    `live_registry_op_names()` (imports `coordinator_core.ops`) and
    `fast_path_op_names()` exactly once each."""
    live = live_registry_op_names()
    fast = fast_path_op_names()
    return RegistryDivergence(
        only_in_live=frozenset(live - fast),
        only_in_fast_path=frozenset(fast - live),
    )


@dataclasses.dataclass(frozen=True)
class OpEntrypoint:
    """One op's resolved owning source location, or the reason it could not be
    resolved. `relpath` is repo-relative POSIX (`"coordinator_core/ops/ping.py"`);
    `None` whenever resolution failed -- check `unresolved_reason` for why.
    `function_name` is the registered handler callable's own `__name__`
    (best-effort identity, e.g. for a decorator-wrapped handler that does not
    preserve it via `functools.wraps`, this may not match the literal `def`
    site -- this module does not attempt to unwrap decorators)."""

    op_name: str
    relpath: Optional[str]
    function_name: Optional[str]
    unresolved_reason: Optional[str] = None


def resolve_op_entrypoints(
    op_names: Iterable[str],
    *,
    registry: Optional[Dict[str, Callable]] = None,
) -> Dict[str, OpEntrypoint]:
    """`op_name -> OpEntrypoint` for every name in `op_names`. Never raises on
    an individual op's resolution failure -- an op whose handler has no
    `__module__`, whose module has no `__file__` (a namespace package, a
    C extension), or whose file sits outside this repo's own tree gets an
    `OpEntrypoint` with `relpath=None` and a stated `unresolved_reason`,
    never dropped from the result and never a crash.

    `registry` defaults to `ipc._REGISTRY` after importing
    `coordinator_core.ops` (via `live_registry_op_names`'s same one-time
    cost) when omitted -- pass an already-populated registry mapping to avoid
    paying that import a second time in a caller that already has one."""
    if registry is None:
        import coordinator_core.ops as _ops_pkg

        _ops_pkg._eager_import_all()
        registry = ipc._REGISTRY

    out: Dict[str, OpEntrypoint] = {}
    for op_name in op_names:
        handler = registry.get(op_name)
        if handler is None:
            out[op_name] = OpEntrypoint(op_name, None, None, "op not present in registry")
            continue

        module_name = getattr(handler, "__module__", None)
        function_name = getattr(handler, "__name__", None)
        module = sys.modules.get(module_name) if module_name else None
        module_file = getattr(module, "__file__", None) if module is not None else None

        if not module_file:
            out[op_name] = OpEntrypoint(
                op_name,
                None,
                function_name,
                f"handler's module {module_name!r} has no resolvable __file__",
            )
            continue

        relpath = _relpath_under_repo_root(pathlib.Path(module_file))
        if relpath is None:
            out[op_name] = OpEntrypoint(
                op_name, None, function_name, f"module file {module_file!r} is outside the repo root"
            )
            continue

        out[op_name] = OpEntrypoint(op_name, relpath, function_name, None)
    return out


def _compute_spawn_sites_for_relpath(path: pathlib.Path, relpath: str) -> Tuple[SpawnSite, ...]:
    """`compute_fn` for `cache.read_revalidated` -- reads `path` (re-read,
    see `cache.read_revalidated`'s own TOCTOU negative-spec) and returns the
    recognised spawn sites found in it, or `()` on a parse failure. Never
    raises `SpawnParseError` out to the caller."""
    text = path.read_text(encoding="utf-8")
    try:
        return tuple(sites_in_source(text, relpath))
    except SpawnParseError:
        return ()


def spawn_sites_by_relpath(
    relpaths: Iterable[str],
    *,
    index: Optional[SpawnIndex] = None,
) -> Dict[str, Tuple[SpawnSite, ...]]:
    """`spawn_policy.detect.sites_in_source` over each distinct repo-relative
    path in `relpaths`. A path that cannot be read (vanished, permission
    error) or fails to parse resolves to an empty tuple rather than raising --
    matching `module_summary.py`'s own fail-closed-to-empty posture for a
    file this module cannot make sense of, never silently dropping the path
    from the returned mapping.

    `index` is `None` by default (unchanged behaviour, existing callers keep
    today's semantics and cost): cached via
    `coordinator_core.cache.read_revalidated`, keyed on the absolute path's
    content-hash, into the SAME existing in-process revalidating tier
    `module_summary.py` already uses (`cache._REVALIDATED_CACHE`). A call
    whose file hasn't changed since the last call in THIS process is a
    cache hit: no re-read, no re-scan.

    Passing `index` (a caller-owned `SpawnIndex`, typically loaded via
    `load_spawn_index`) routes through `cache.read_disk_revalidated`
    instead -- the PERSISTED, cross-process tier `module_summary.py`
    consumes for its own index (Ruling 1: additive to `cache.py`, not a
    second cache module). This is the fix for the ~1.3s of uncached,
    unbounded-per-COLD-PROCESS rescan `ops_with_spawn_evidence` used to pay
    on every fresh process, warm or not -- see this module's own docstring
    negative-spec for what stays unchanged (no `_MAX_CACHE` raise, no
    change to `cache.read_revalidated`'s own contract or existing callers'
    cost -- `index=None` never touches `read_disk_revalidated` at all)."""
    out: Dict[str, Tuple[SpawnSite, ...]] = {}
    for relpath in sorted(set(relpaths)):
        abs_path = _REPO_ROOT / relpath
        compute_fn = lambda p, r=relpath: _compute_spawn_sites_for_relpath(p, r)
        try:
            if index is None:
                out[relpath] = cache.read_revalidated(abs_path, compute_fn)
            else:
                out[relpath] = cache.read_disk_revalidated(abs_path, compute_fn, index)
        except OSError:
            out[relpath] = ()
    return out


def _spawn_site_to_dict(site: SpawnSite) -> dict:
    return {
        "path": site.path,
        "enclosing": site.enclosing,
        "argv0": site.argv0,
        "ordinal": site.ordinal,
        "kind": site.kind.value,
        "argv_digest": site.argv_digest,
        "lineno": site.lineno,
    }


def _spawn_site_from_dict(data: dict) -> SpawnSite:
    return SpawnSite(
        path=data["path"],
        enclosing=data["enclosing"],
        argv0=data["argv0"],
        ordinal=data["ordinal"],
        kind=SpawnKind(data["kind"]),
        argv_digest=data["argv_digest"],
        lineno=data["lineno"],
    )


def load_spawn_index(disk_path: "str | pathlib.Path") -> SpawnIndex:
    """Loads a previously `save_spawn_index`-written JSON file into the
    `{relpath: (stamp, (SpawnSite, ...))}` shape `spawn_sites_by_relpath`
    expects -- mirrors `module_summary.load_index`'s own fail-closed
    contract exactly: a missing, unreadable, or malformed file degrades to
    an empty index (a cold build for every path on the next call), never a
    crash. One corrupt entry degrades only that entry, never the whole
    load."""
    disk_path = pathlib.Path(disk_path)
    try:
        raw = json.loads(disk_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    index: SpawnIndex = {}
    if not isinstance(raw, dict):
        return {}
    for relpath, entry in raw.items():
        try:
            stamp, site_dicts = entry
            index[relpath] = (stamp, tuple(_spawn_site_from_dict(d) for d in site_dicts))
        except (TypeError, ValueError, KeyError):
            continue
    return index


def save_spawn_index(index: SpawnIndex, disk_path: "str | pathlib.Path") -> None:
    """Serializes `index` to `disk_path` as JSON, creating parent
    directories as needed. Overwrites any existing file at `disk_path` --
    mirrors `module_summary.save_index`'s own shape."""
    disk_path = pathlib.Path(disk_path)
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        relpath: [stamp, [_spawn_site_to_dict(s) for s in sites]]
        for relpath, (stamp, sites) in index.items()
    }
    disk_path.write_text(json.dumps(serializable, sort_keys=True), encoding="utf-8", newline="\n")


def ops_with_spawn_evidence(
    entrypoints: Dict[str, OpEntrypoint],
    *,
    index: Optional[SpawnIndex] = None,
    function_granular: bool = False,
) -> Dict[str, Tuple[SpawnSite, ...]]:
    """`op_name -> spawn sites found reachable from that op`, restricted to
    ops whose owning module resolved (`relpath is not None`) AND reach at
    least one recognised spawn site under the selected granularity.

    `function_granular=False` (the default, unchanged behaviour) is MODULE
    granularity: any recognised spawn site anywhere in the op's owning module
    file counts, even one belonging to a sibling op's own handler -- see
    module docstring's "MODULE GRANULARITY BY DEFAULT" section.

    `function_granular=True` narrows to handler-FUNCTION granularity via a
    transitive-BFS reachability closure reused from
    `test_no_uncounted_spawn_on_budgeted_path.py` (see that section of this
    module's own docstring for the reuse contract and its accepted
    false-negative gaps). `index` is ignored under this mode -- the reused
    corpus builder does its own file read/parse, independent of
    `spawn_sites_by_relpath`'s cache tiers.

    `index`, passed straight through to `spawn_sites_by_relpath` when
    `function_granular` is `False`, is `None` by default (unchanged
    in-process-only behaviour); pass a caller-owned `SpawnIndex` (e.g. from
    `load_spawn_index`) to revalidate against a persisted, cross-process tier
    instead."""
    if function_granular:
        return _ops_with_spawn_evidence_function_granular(entrypoints)

    relpaths = [ep.relpath for ep in entrypoints.values() if ep.relpath is not None]
    sites_by_relpath = spawn_sites_by_relpath(relpaths, index=index)

    out: Dict[str, Tuple[SpawnSite, ...]] = {}
    for op_name, ep in entrypoints.items():
        if ep.relpath is None:
            continue
        sites = sites_by_relpath.get(ep.relpath, ())
        if sites:
            out[op_name] = sites
    return out


def _ops_with_spawn_evidence_function_granular(
    entrypoints: Dict[str, OpEntrypoint],
) -> Dict[str, Tuple[SpawnSite, ...]]:
    """`function_granular=True` leg of `ops_with_spawn_evidence`. Builds the
    reused gate module's corpus (`_build_corpus_with_dispatch_tables`) ONCE
    for the whole call,
    then for every op whose `(relpath, function_name)` resolves to a
    TOP-LEVEL function definition in that corpus's own `func_defs`, computes
    the transitive-BFS reachable-function set from that one entrypoint
    (`_reachable_functions`) and narrows to the spawn sites whose own
    top-level enclosing function is in that set (`_on_path_spawn_sites`, with
    an empty exemption set -- this module holds no exemptions, see module
    docstring's negative-spec).

    An op whose entrypoint does not resolve to a `func_defs` key (outside the
    reused gate module's own scope roots, or a decorator-obscured
    `__name__`) is OMITTED here -- a named, accepted false negative, never a
    silent fall-back to module granularity. Local import to break the
    circular dependency: the gate module imports this module by name."""
    from coordinator_core.tests.test_no_uncounted_spawn_on_budgeted_path import (
        _build_corpus_with_dispatch_tables,
        _on_path_spawn_sites,
        _reachable_functions,
    )

    (
        func_index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
        module_callable_tables_by_file,
        table_aliases_by_file,
    ) = _build_corpus_with_dispatch_tables()

    reached_sites_by_entry: Dict[Tuple[str, str], Tuple[SpawnSite, ...]] = {}
    out: Dict[str, Tuple[SpawnSite, ...]] = {}
    for op_name, ep in entrypoints.items():
        if ep.relpath is None or ep.function_name is None:
            continue
        entry_key = (ep.relpath, ep.function_name)
        if entry_key not in func_index.func_defs:
            continue
        if entry_key not in reached_sites_by_entry:
            reached = _reachable_functions(
                {entry_key},
                func_index,
                import_aliases_by_file,
                func_aliases_by_file,
                local_aliases_by_file,
                module_callable_tables_by_file,
                table_aliases_by_file,
            )
            reached_sites_by_entry[entry_key] = tuple(
                _on_path_spawn_sites(reached, spawn_sites_by_file, frozenset())
            )
        sites = reached_sites_by_entry[entry_key]
        if sites:
            out[op_name] = sites
    return out
