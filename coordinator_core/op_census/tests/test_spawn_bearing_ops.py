"""coordinator_core.op_census.tests.test_spawn_bearing_ops — tests for the
op-registry-derived spawn-evidence layer (C2).

Covers: authoritative vs fast-path op-name derivation, registry-divergence
comparison, per-op entrypoint resolution (success and every named failure
mode), and module-granularity spawn evidence over a synthetic corpus.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C2.md
"""

from __future__ import annotations

from coordinator_core.op_census import spawn_bearing_ops
from coordinator_core.op_census.spawn_bearing_ops import OpEntrypoint, RegistryDivergence


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
