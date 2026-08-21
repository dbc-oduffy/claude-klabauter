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

MODULE GRANULARITY, DELIBERATELY (not function-level reachability). The
existing gate module already ships a precise, function-level, transitive-BFS
reachability predicate for its own nine live entrypoints (`_reachable_functions`
in `test_no_uncounted_spawn_on_budgeted_path.py`) — reusing or duplicating that
machinery for all ~274 live ops is explicitly NOT this chunk's job (the plan's
own anti-scope: "do not build a second spawn inventory"). `ops_with_spawn_evidence`
instead asks a cheaper, coarser question: does `spawn_policy.detect.sites_in_source`
find ANY recognised spawn call site anywhere in the op's OWNING MODULE FILE (not
narrowed to the registered handler function's own body)? This is a deliberate,
named over-approximation: a module that registers several ops (e.g.
`coordinator_core.hooks`, sixteen ops in one module) will report spawn evidence
for every op it registers if the module contains a spawn site anywhere, even one
that belongs to a sibling op's own handler. That bias is intentional and matches
this plan's own stated preference (`test_no_uncounted_spawn_on_budgeted_path.py`'s
docstring: "it would rather over-report a site than silently drop one whose
gating it cannot prove") — a false positive here means one more row enters the
frozen inventory or gets enrolled; a false negative would recreate the exact
invisibility bug this chunk exists to close. Narrowing this to function-level
reachability for every live op, if ever wanted, is a future chunk's job, not a
silent tightening here.

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

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C2.md
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys
from typing import Callable, Dict, FrozenSet, Iterable, NamedTuple, Optional, Tuple

from coordinator_core import ipc
from coordinator_core.ops import _registry_map
from coordinator_core.spawn_policy.detect import SpawnParseError, SpawnSite, sites_in_source

__all__ = [
    "OpEntrypoint",
    "RegistryDivergence",
    "live_registry_op_names",
    "fast_path_op_names",
    "registry_divergence",
    "resolve_op_entrypoints",
    "spawn_sites_by_relpath",
    "ops_with_spawn_evidence",
]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def live_registry_op_names() -> FrozenSet[str]:
    """The authoritative op-name set: `ipc._REGISTRY.keys()` after importing
    `coordinator_core.ops` (registers every op module's `@register_op` side
    effect). This is the fidelity source hard constraint 7 (amended) and PM
    Ruling 3-B require — never the fast-path map alone. Safe to call from a
    test process or a once-per-boot/once-per-cold-run caller; not safe to call
    on a per-invocation hot path (see module docstring)."""
    import coordinator_core.ops  # noqa: F401 -- side effect: populates ipc._REGISTRY

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
        import coordinator_core.ops  # noqa: F401 -- side effect: populates ipc._REGISTRY

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

        try:
            relpath = pathlib.Path(module_file).resolve().relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            out[op_name] = OpEntrypoint(
                op_name, None, function_name, f"module file {module_file!r} is outside the repo root"
            )
            continue

        out[op_name] = OpEntrypoint(op_name, relpath, function_name, None)
    return out


def spawn_sites_by_relpath(relpaths: Iterable[str]) -> Dict[str, Tuple[SpawnSite, ...]]:
    """`spawn_policy.detect.sites_in_source` over each distinct repo-relative
    path in `relpaths`, read exactly once per unique path. A path that cannot
    be read (vanished, permission error) or fails to parse resolves to an
    empty tuple rather than raising -- matching `module_summary.py`'s own
    fail-closed-to-empty posture for a file this module cannot make sense of,
    never silently dropping the path from the returned mapping."""
    out: Dict[str, Tuple[SpawnSite, ...]] = {}
    for relpath in sorted(set(relpaths)):
        if relpath in out:
            continue
        abs_path = _REPO_ROOT / relpath
        try:
            text = abs_path.read_text(encoding="utf-8")
        except OSError:
            out[relpath] = ()
            continue
        try:
            out[relpath] = tuple(sites_in_source(text, relpath))
        except SpawnParseError:
            out[relpath] = ()
    return out


def ops_with_spawn_evidence(
    entrypoints: Dict[str, OpEntrypoint],
) -> Dict[str, Tuple[SpawnSite, ...]]:
    """`op_name -> spawn sites found in that op's owning module`, restricted
    to ops whose owning module resolved (`relpath is not None`) AND contains
    at least one recognised spawn site. This is MODULE granularity, not
    handler-function granularity -- see module docstring's "MODULE
    GRANULARITY" section for why that is the deliberate choice here."""
    relpaths = [ep.relpath for ep in entrypoints.values() if ep.relpath is not None]
    sites_by_relpath = spawn_sites_by_relpath(relpaths)

    out: Dict[str, Tuple[SpawnSite, ...]] = {}
    for op_name, ep in entrypoints.items():
        if ep.relpath is None:
            continue
        sites = sites_by_relpath.get(ep.relpath, ())
        if sites:
            out[op_name] = sites
    return out
