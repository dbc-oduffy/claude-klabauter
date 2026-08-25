"""coordinator_core.op_census.tests.test_spawn_bearing_ops — tests for the
op-registry-derived spawn-evidence layer (C2), plus C1's function-granular
narrowing of `ops_with_spawn_evidence`.

Covers: authoritative vs fast-path op-name derivation, registry-divergence
comparison, per-op entrypoint resolution (success and every named failure
mode), module-granularity spawn evidence over a synthetic corpus, and (C1)
`function_granular=True`'s (op, site)-keyed narrowing plus its width probe.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C2.md
               state/dispatch-briefs/2026-08-22-the-composition-gate-counts-processes-across-the-op-graph/C1.md
"""

from __future__ import annotations

import time

import pytest

from coordinator_core.op_census import spawn_bearing_ops
from coordinator_core.op_census.spawn_bearing_ops import OpEntrypoint, RegistryDivergence
from coordinator_core.tests import test_no_uncounted_spawn_on_budgeted_path as _gate
from coordinator_core.tests import test_no_unbatched_per_item_git_spawn as _gate_scope


def _fake_handler(module_name: str, func_name: str):
    def _fn():
        return None

    _fn.__module__ = module_name
    _fn.__name__ = func_name
    return _fn


# --------------------------------------------------------------------------
# live_registry_op_names / fast_path_op_names / registry_divergence
# --------------------------------------------------------------------------


def test_live_registry_op_names_is_a_superset_including_ping():
    names = spawn_bearing_ops.live_registry_op_names()
    assert isinstance(names, frozenset)
    assert "ping" in names
    assert len(names) > 100  # the live registry has hundreds of ops today


def test_fast_path_op_names_matches_registry_map_module():
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert spawn_bearing_ops.fast_path_op_names() == frozenset(OP_MODULE_MAP.keys())


def test_registry_divergence_agrees_property():
    agreeing = RegistryDivergence(only_in_live=frozenset(), only_in_fast_path=frozenset())
    assert agreeing.agrees

    live_only = RegistryDivergence(only_in_live=frozenset({"some.op"}), only_in_fast_path=frozenset())
    assert not live_only.agrees

    fast_only = RegistryDivergence(only_in_live=frozenset(), only_in_fast_path=frozenset({"some.op"}))
    assert not fast_only.agrees


def test_registry_divergence_computes_real_symmetric_difference():
    divergence = spawn_bearing_ops.registry_divergence()
    live = spawn_bearing_ops.live_registry_op_names()
    fast = spawn_bearing_ops.fast_path_op_names()
    assert divergence.only_in_live == frozenset(live - fast)
    assert divergence.only_in_fast_path == frozenset(fast - live)


# --------------------------------------------------------------------------
# resolve_op_entrypoints
# --------------------------------------------------------------------------


def test_resolve_op_entrypoints_resolves_a_real_live_op():
    """`ping` is registered by `coordinator_core.ops.ping`'s own `_ping`
    function -- the simplest real op in the tree, used here as a stable
    fixture rather than a synthetic registry."""
    live_ops = spawn_bearing_ops.live_registry_op_names()
    assert "ping" in live_ops
    entrypoints = spawn_bearing_ops.resolve_op_entrypoints(["ping"])
    ep = entrypoints["ping"]
    assert isinstance(ep, OpEntrypoint)
    assert ep.op_name == "ping"
    assert ep.relpath == "coordinator_core/ops/ping.py"
    assert ep.function_name == "_ping"
    assert ep.unresolved_reason is None


def test_resolve_op_entrypoints_unknown_op_name_is_evidence_not_a_crash():
    entrypoints = spawn_bearing_ops.resolve_op_entrypoints(
        ["not.a.real.op"], registry={"ping": _fake_handler("coordinator_core.ops.ping", "_ping")}
    )
    ep = entrypoints["not.a.real.op"]
    assert ep.relpath is None
    assert ep.function_name is None
    assert "not present in registry" in ep.unresolved_reason


def test_resolve_op_entrypoints_module_with_no_file_attribute():
    handler = _fake_handler("no_such_module_in_sys_modules_registry_test", "some_fn")
    entrypoints = spawn_bearing_ops.resolve_op_entrypoints(["x.op"], registry={"x.op": handler})
    ep = entrypoints["x.op"]
    assert ep.relpath is None
    assert ep.function_name == "some_fn"
    assert "no resolvable __file__" in ep.unresolved_reason


def test_resolve_op_entrypoints_never_raises_on_a_bare_object_handler():
    """A plain instance (no `__name__`, `__module__` inherited from its
    defining module) must never crash resolution -- it degrades to
    best-effort evidence, resolving through whatever `__module__` Python
    itself assigned rather than raising."""
    class _NoNameAttr:
        pass

    handler = _NoNameAttr()
    entrypoints = spawn_bearing_ops.resolve_op_entrypoints(["y.op"], registry={"y.op": handler})
    ep = entrypoints["y.op"]
    assert ep.op_name == "y.op"
    assert ep.function_name is None  # no __name__ on a plain instance
    # __module__ IS present (inherited from the defining module), so
    # resolution succeeds rather than failing -- this proves the missing
    # __name__ alone does not crash or block relpath resolution.
    assert ep.relpath is not None


# --------------------------------------------------------------------------
# spawn_sites_by_relpath / ops_with_spawn_evidence -- synthetic corpus
# --------------------------------------------------------------------------


def test_spawn_sites_by_relpath_over_synthetic_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn_bearing_ops, "_REPO_ROOT", tmp_path)

    spawning = tmp_path / "spawning.py"
    spawning.write_text(
        "import subprocess\n"
        "def run_git():\n"
        "    return subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    quiet = tmp_path / "quiet.py"
    quiet.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    result = spawn_bearing_ops.spawn_sites_by_relpath(["spawning.py", "quiet.py"])
    assert len(result["spawning.py"]) == 1
    assert result["spawning.py"][0].enclosing == "run_git"
    assert result["quiet.py"] == ()


def test_spawn_sites_by_relpath_missing_file_resolves_to_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn_bearing_ops, "_REPO_ROOT", tmp_path)
    result = spawn_bearing_ops.spawn_sites_by_relpath(["does_not_exist.py"])
    assert result == {"does_not_exist.py": ()}


def test_spawn_sites_by_relpath_unparseable_file_resolves_to_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn_bearing_ops, "_REPO_ROOT", tmp_path)
    broken = tmp_path / "broken.py"
    broken.write_text("def (((( not python\n", encoding="utf-8")
    result = spawn_bearing_ops.spawn_sites_by_relpath(["broken.py"])
    assert result == {"broken.py": ()}


def test_ops_with_spawn_evidence_module_granularity(tmp_path, monkeypatch):
    """Two ops sharing one module: the module carries a spawn site, so BOTH
    ops report evidence, even though only one op's own handler function is
    the one spawning -- the deliberate module-granularity over-approximation
    this module's docstring names."""
    monkeypatch.setattr(spawn_bearing_ops, "_REPO_ROOT", tmp_path)

    shared = tmp_path / "shared.py"
    shared.write_text(
        "import subprocess\n"
        "def handler_a():\n"
        "    return subprocess.run(['git', 'status'])\n"
        "def handler_b():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    quiet = tmp_path / "quiet.py"
    quiet.write_text("def handler_c():\n    return None\n", encoding="utf-8")

    entrypoints = {
        "op.a": OpEntrypoint("op.a", "shared.py", "handler_a"),
        "op.b": OpEntrypoint("op.b", "shared.py", "handler_b"),
        "op.c": OpEntrypoint("op.c", "quiet.py", "handler_c"),
        "op.unresolved": OpEntrypoint("op.unresolved", None, None, "not present in registry"),
    }
    evidence = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints)
    assert set(evidence) == {"op.a", "op.b"}
    assert len(evidence["op.a"]) == 1
    assert evidence["op.a"] == evidence["op.b"]


def test_ops_with_spawn_evidence_empty_when_no_module_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn_bearing_ops, "_REPO_ROOT", tmp_path)
    quiet = tmp_path / "quiet.py"
    quiet.write_text("def handler():\n    return None\n", encoding="utf-8")
    entrypoints = {"op.only": OpEntrypoint("op.only", "quiet.py", "handler")}
    assert spawn_bearing_ops.ops_with_spawn_evidence(entrypoints) == {}


# --------------------------------------------------------------------------
# ops_with_spawn_evidence(function_granular=True) -- C1, (op, site)-keyed
# --------------------------------------------------------------------------


def _synthetic_scope(monkeypatch, tmp_path):
    """Points the reused gate module's own corpus builder
    (`_build_corpus`/`_scope_roots`) at an isolated `tmp_path/coordinator_core`
    tree instead of the live repo -- both `_REPO_ROOT` and `_GATE_SCOPE_ROOTS`
    are plain module-level names imported by name into the gate module, so
    monkeypatching them there (not on `spawn_bearing_ops`, which never reads
    either) is what actually redirects `_build_corpus`."""
    monkeypatch.setattr(_gate, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(_gate, "_GATE_SCOPE_ROOTS", ("coordinator_core",))
    # `_relpath` (in `test_no_unbatched_per_item_git_spawn.py`) tries its OWN
    # module's `_REPO_ROOT` first, only falling back to root-relative (which
    # drops the `coordinator_core/` prefix `OpEntrypoint.relpath` always
    # carries) when that fails -- patch it too so `func_defs` keys land on
    # the SAME repo-root-relative strings entrypoint resolution produces.
    monkeypatch.setattr(_gate_scope, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(spawn_bearing_ops, "_REPO_ROOT", tmp_path)
    root = tmp_path / "coordinator_core"
    root.mkdir()
    return root


def test_ops_with_spawn_evidence_function_granular_keys_on_op_site_pair(tmp_path, monkeypatch):
    """AC4/AC4b/AC4c's real property: `handler_a` reaches the spawn site,
    `handler_b` in the SAME module does not call it at all -- module
    granularity (the default) reports evidence for both; `function_granular`
    narrows to the op whose OWN reachable set actually contains the site,
    proving the assertion is keyed on the (op, site) pair and not on "does
    some op in this module reach it."""
    root = _synthetic_scope(monkeypatch, tmp_path)
    shared = root / "shared.py"
    shared.write_text(
        "import subprocess\n"
        "def handler_a():\n"
        "    return subprocess.run(['git', 'status'])\n"
        "def handler_b():\n"
        "    return 1\n",
        encoding="utf-8",
    )

    entrypoints = {
        "op.a": OpEntrypoint("op.a", "coordinator_core/shared.py", "handler_a"),
        "op.b": OpEntrypoint("op.b", "coordinator_core/shared.py", "handler_b"),
    }

    module_granular = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints)
    assert set(module_granular) == {"op.a", "op.b"}

    function_granular = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)
    assert set(function_granular) == {"op.a"}
    assert len(function_granular["op.a"]) == 1
    assert function_granular["op.a"][0].enclosing == "handler_a"


def test_ops_with_spawn_evidence_function_granular_follows_transitive_call(tmp_path, monkeypatch):
    """The predicate is transitive, not one-hop: `handler` calls `helper`,
    which spawns -- the site's enclosing function is `helper`, not `handler`,
    and it must still surface under `handler`'s own op."""
    root = _synthetic_scope(monkeypatch, tmp_path)
    mod = root / "chain.py"
    mod.write_text(
        "import subprocess\n"
        "def helper():\n"
        "    return subprocess.run(['git', 'status'])\n"
        "def handler():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    entrypoints = {"op.chain": OpEntrypoint("op.chain", "coordinator_core/chain.py", "handler")}
    result = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)
    assert set(result) == {"op.chain"}
    assert result["op.chain"][0].enclosing == "helper"


def test_ops_with_spawn_evidence_function_granular_unresolvable_entry_is_omitted_not_widened(
    tmp_path, monkeypatch
):
    """AC3/negative-spec: an op whose `function_name` does not match a
    top-level `def` in the reused corpus's `func_defs` (e.g. resolved to a
    module the reused gate module's own scope roots do not cover) reports NO
    function-granular evidence -- never a silent fall-back to the module-wide
    over-approximation, even though that module DOES carry a spawn site."""
    root = _synthetic_scope(monkeypatch, tmp_path)
    shared = root / "shared2.py"
    shared.write_text(
        "import subprocess\n"
        "def real_handler():\n"
        "    return subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    entrypoints = {
        "op.unresolvable": OpEntrypoint(
            "op.unresolvable", "coordinator_core/shared2.py", "no_such_top_level_function"
        )
    }
    module_granular = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints)
    assert "op.unresolvable" in module_granular  # module lens still over-reports, as documented

    function_granular = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)
    assert function_granular == {}


def test_ops_with_spawn_evidence_function_granular_nested_site_attributes_to_top_level(
    tmp_path, monkeypatch
):
    """AC3 pin on shipped behaviour: a spawn nested inside a closure still
    attributes to its TOP-LEVEL enclosing function
    (`site.enclosing.split(".")[0]`, the C-13 gate's own reused predicate),
    matching the named oracles `_commit_delivered_memo._unstage_delivered_memo`
    and `_verify_scoped_to_sha_resolvable._rev_parse` that already resolve
    correctly today on the live tree."""
    root = _synthetic_scope(monkeypatch, tmp_path)
    mod = root / "nested.py"
    mod.write_text(
        "import subprocess\n"
        "def outer():\n"
        "    def _inner():\n"
        "        return subprocess.run(['git', 'status'])\n"
        "    return _inner()\n",
        encoding="utf-8",
    )
    entrypoints = {"op.nested": OpEntrypoint("op.nested", "coordinator_core/nested.py", "outer")}
    result = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)
    assert set(result) == {"op.nested"}
    assert result["op.nested"][0].enclosing == "outer._inner"


def test_ops_with_spawn_evidence_function_granular_follows_to_thread_hop(tmp_path, monkeypatch):
    """C8/AC15: `handler` reaches `helper` only through `asyncio.to_thread(helper)`, never a
    direct call -- before C8, `_reachable_functions` had no edge for a thread hop at all, so
    `helper`'s spawn site was invisible to the function-granular walk even though `handler`
    genuinely spawns through it at runtime. Same transitive-attribution shape as the direct-call
    sibling test above, but reached only via the thread-hop edge."""
    root = _synthetic_scope(monkeypatch, tmp_path)
    mod = root / "thread_hop.py"
    mod.write_text(
        "import asyncio\n"
        "import subprocess\n"
        "def helper():\n"
        "    return subprocess.run(['git', 'status'])\n"
        "async def handler():\n"
        "    return await asyncio.to_thread(helper)\n",
        encoding="utf-8",
    )
    entrypoints = {
        "op.thread_hop": OpEntrypoint("op.thread_hop", "coordinator_core/thread_hop.py", "handler")
    }
    module_granular = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints)
    assert "op.thread_hop" in module_granular  # module lens already saw it; not the regression

    function_granular = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)
    assert set(function_granular) == {"op.thread_hop"}
    assert function_granular["op.thread_hop"][0].enclosing == "helper"


def test_ops_with_spawn_evidence_function_granular_follows_run_in_executor_hop(tmp_path, monkeypatch):
    """Sibling of the `to_thread` case for `loop.run_in_executor(None, fn, ...)` -- the callee
    sits at the SECOND positional argument, not the first (the executor, often a bare `None`,
    occupies the first slot), so this pins that the resolver reads the right argument index."""
    root = _synthetic_scope(monkeypatch, tmp_path)
    mod = root / "executor_hop.py"
    mod.write_text(
        "import subprocess\n"
        "def helper():\n"
        "    return subprocess.run(['git', 'status'])\n"
        "def handler(loop):\n"
        "    return loop.run_in_executor(None, helper)\n",
        encoding="utf-8",
    )
    entrypoints = {
        "op.executor_hop": OpEntrypoint(
            "op.executor_hop", "coordinator_core/executor_hop.py", "handler"
        )
    }
    function_granular = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)
    assert set(function_granular) == {"op.executor_hop"}
    assert function_granular["op.executor_hop"][0].enclosing == "helper"


def test_ops_with_spawn_evidence_function_granular_unresolvable_thread_hop_is_counted(
    tmp_path, monkeypatch
):
    """AC15's own residual-gap requirement: a thread-hop call whose callee argument is not
    statically resolvable (here, a `getattr`-shaped dynamic dispatch) yields no edge -- the
    site stays invisible to `function_granular` exactly as before C8 -- but the gap is COUNTED
    in `_UNRESOLVED_THREAD_HOP_CALLEES` via `_gate._unresolved_thread_hop_report()`, not silently
    dropped as an assumption."""
    root = _synthetic_scope(monkeypatch, tmp_path)
    mod = root / "dynamic_hop.py"
    mod.write_text(
        "import asyncio\n"
        "def handler(obj):\n"
        "    return asyncio.to_thread(getattr(obj, 'method'))\n",
        encoding="utf-8",
    )
    entrypoints = {
        "op.dynamic_hop": OpEntrypoint("op.dynamic_hop", "coordinator_core/dynamic_hop.py", "handler")
    }
    function_granular = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)
    assert function_granular == {}

    report = _gate._unresolved_thread_hop_report()
    assert ("coordinator_core/dynamic_hop.py", "handler", 3) in report


@pytest.mark.cadence
def test_function_granular_width_probe_timing():
    """AC10's width probe: this chunk holds the function-granular result and
    needs no enrolment to run it, so it times a single `_reachable_functions`
    closure and the full pass at sampled widths of 8, ~40, and 277 against
    the LIVE tree -- C2 must not commit to a width before this number exists.
    `cadence`-marked (heavy, live-corpus scan): never in the fast tier, same
    posture `test_no_uncounted_spawn_on_budgeted_path.py`'s own docstring
    names for its leg-3 measurement work."""
    live_ops = sorted(spawn_bearing_ops.live_registry_op_names())
    all_entrypoints = spawn_bearing_ops.resolve_op_entrypoints(live_ops)
    resolved = [
        (name, ep)
        for name, ep in all_entrypoints.items()
        if ep.relpath is not None and ep.function_name is not None
    ]

    t0 = time.perf_counter()
    single = dict(resolved[:1])
    spawn_bearing_ops.ops_with_spawn_evidence(single, function_granular=True)
    single_closure_s = time.perf_counter() - t0

    timings: dict[int, float] = {}
    for width in (8, min(40, len(resolved)), len(resolved)):
        sample = dict(resolved[:width])
        t0 = time.perf_counter()
        spawn_bearing_ops.ops_with_spawn_evidence(sample, function_granular=True)
        timings[width] = time.perf_counter() - t0

    print(f"[C1 width probe] single closure: {single_closure_s:.3f}s")
    for width, elapsed in timings.items():
        print(f"[C1 width probe] width={width}: {elapsed:.3f}s")

    # Soft sanity only -- the width probe's job is to PRODUCE the number for
    # C2's own decision, not to gate on one here.
    assert single_closure_s >= 0
    assert all(elapsed >= 0 for elapsed in timings.values())
