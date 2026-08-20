"""test_machine_local_registry_reader_parity.py — pins
`machine_local_impl_resolve.registry_get`/`registry_dir` (the spawn-free,
`coordinator_core`-free registry reader in `coordinator/bin/lib/
machine_local_impl_resolve.py`) to `coordinator_core.machine_resolver.
registry_get` (the oracle) by direct value comparison across every
resolution-order case the two readers must agree on.

This test file is the ONLY place in this pair permitted to import
`coordinator_core` — it is the parity oracle. `machine_local_impl_resolve.py`
itself MUST NOT (see that module's docstring, HARD CONSTRAINT).

Spec backlink: state/dispatch-briefs/2026-08-20-doe-root-rung-2-stops-
spawning/C1.md.
"""
from __future__ import annotations

import ast
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
_REPO_ROOT = os.path.dirname(_BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import machine_local_impl_resolve as mlir  # noqa: E402

from coordinator_core import machine_resolver  # noqa: E402 — parity oracle only, in this test file


REGISTRY_TOML = """
schema = 1
flat_list_key = ["a", "b", "c"]
"schema.nested" = "also-excluded-by-value"

[coordinator]
machine_slug = "tracked-machine"

[concerns.leaked]
should_be_absent = "nope"

[repos]
doe_claude = "X:/DoE-claude"  # abs-path-ok: synthetic TOML fixture value, not a real repo reference

[repos.backslash_repo]
"""

REGISTRY_LOCAL_TOML = """
"flat.dotted.key" = "local-value"
shadowed_key = "local-wins"

[coordinator]
machine_slug = "local-machine"

[schema.leaked]
should_be_absent = "nope-either"
"""


def _seed_registry_dir(tmp_path, registry_text=None, registry_local_text=None):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    if registry_text is not None:
        (reg_dir / "registry.toml").write_text(registry_text, encoding="utf-8")
    if registry_local_text is not None:
        (reg_dir / "registry.local.toml").write_text(registry_local_text, encoding="utf-8")
    return reg_dir


def _reset_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MACHINE_LOCAL_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)


# ---------------------------------------------------------------------------
# AC2: MACHINE_LOCAL_REGISTRY_DIR-seeded parity cases
# ---------------------------------------------------------------------------


def _parity_case(monkeypatch, tmp_path, key):
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(tmp_path, REGISTRY_TOML, REGISTRY_LOCAL_TOML)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    mine = mlir.registry_get(key)
    theirs = machine_resolver.registry_get(key)
    assert mine == theirs, f"{key!r}: mine={mine!r} theirs={theirs!r}"
    return mine


def test_parity_nested_table_key(monkeypatch, tmp_path):
    assert _parity_case(monkeypatch, tmp_path, "coordinator.machine_slug") == "local-machine"


def test_parity_flat_quoted_dotted_key(monkeypatch, tmp_path):
    assert _parity_case(monkeypatch, tmp_path, "flat.dotted.key") == "local-value"


def test_parity_list_valued_key(monkeypatch, tmp_path):
    assert _parity_case(monkeypatch, tmp_path, "flat_list_key") == "a\nb\nc"


def test_parity_explicitly_empty_key(monkeypatch, tmp_path):
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(tmp_path, 'empty_key = ""\n')
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    mine = mlir.registry_get("empty_key")
    theirs = machine_resolver.registry_get("empty_key")
    assert mine is None
    assert theirs is None


def test_parity_local_over_tracked_shadowed_key(monkeypatch, tmp_path):
    assert _parity_case(monkeypatch, tmp_path, "shadowed_key") == "local-wins"


def test_parity_concerns_prefixed_table_key_absent(monkeypatch, tmp_path):
    """A key under a `concerns`-prefixed table must be absent from both
    flattenings — not just the root-level `concerns` table."""
    assert _parity_case(monkeypatch, tmp_path, "concerns.leaked.should_be_absent") is None


def test_parity_schema_prefixed_table_key_absent(monkeypatch, tmp_path):
    """A key under a `schema`-prefixed table must be absent from both
    flattenings at every nesting level, not only the root."""
    assert _parity_case(monkeypatch, tmp_path, "schema.leaked.should_be_absent") is None


def test_parity_absent_key(monkeypatch, tmp_path):
    assert _parity_case(monkeypatch, tmp_path, "does.not.exist") is None


def test_parity_env_override(monkeypatch, tmp_path):
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(tmp_path, REGISTRY_TOML, REGISTRY_LOCAL_TOML)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_SLUG", "env-wins")
    mine = mlir.registry_get("coordinator.machine_slug")
    theirs = machine_resolver.registry_get("coordinator.machine_slug")
    assert mine == theirs == "env-wins"


# ---------------------------------------------------------------------------
# MINOR-1: MACHINE_LOCAL_REGISTRY_DIR unset, COORDINATOR_SETTINGS_HOME pinned
# ---------------------------------------------------------------------------


def test_parity_via_settings_home_not_registry_dir_override(monkeypatch, tmp_path):
    """The seeded-override cases above short-circuit settings_home() before
    it ever runs, which is the only place the two readers can structurally
    diverge. This case is required to exercise that path."""
    _reset_env(monkeypatch)
    settings_home_root = tmp_path / "settings-home"
    settings_home_root.mkdir()
    _seed_registry_dir(settings_home_root, REGISTRY_TOML, REGISTRY_LOCAL_TOML)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_root))

    mine = mlir.registry_get("coordinator.machine_slug")
    theirs = machine_resolver.registry_get("coordinator.machine_slug")
    assert mine == theirs == "local-machine"


# ---------------------------------------------------------------------------
# AC4b: repos.* backslash-form normalization
# ---------------------------------------------------------------------------


def test_parity_repos_key_normalizes_backslash_form(monkeypatch, tmp_path):
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(
        tmp_path,
        # abs-path-ok: synthetic TOML fixture value, not a real repo reference
        r'"repos.doe_claude" = "X:\\DoE-claude\\worktree"' + "\n",
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    mine = mlir.registry_get("repos.doe_claude")
    theirs = machine_resolver.registry_get("repos.doe_claude")
    assert mine == theirs
    # The stored value itself carries backslashes; the point of AC4b is that
    # the return value is unchanged when it is already native drive form
    # (this is a preservation check, not a forced-forward-slash rewrite) —
    # cross-check against a genuine MSYS mount-form input below.


def test_registry_get_repairs_msys_mount_form_for_repos_key(monkeypatch, tmp_path):
    """A `repos.*`-shaped value stored in MSYS mount form (`/x/...`) must be
    returned in native drive form (`X:/...`) — mirrors
    `gen_doe_root_pointer._resolve_doe_root`'s `native_path_form(...)` wrap."""
    if os.name != "nt":
        import pytest

        pytest.skip("MSYS mount-form repair is Windows-only (os.name == 'nt' gated)")
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(
        tmp_path,
        '"repos.doe_claude" = "/x/DoE-claude"\n',
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    mine = mlir.registry_get("repos.doe_claude")
    assert mine == "X:/DoE-claude"  # abs-path-ok: synthetic TOML fixture value, not a real repo reference

    # PINNED DIVERGENCE (see module docstring's "Two divergences... ACCEPTED"
    # list, item 3): the oracle does no normalization anywhere in its body and
    # returns the raw stored value unrepaired. `mlir.registry_get` repairs it.
    # This is deliberate — see module docstring — so assert the divergence
    # explicitly rather than leaving it merely unasserted (which is what let
    # this exact case go unpinned before this test was extended).
    theirs = machine_resolver.registry_get("repos.doe_claude")
    assert theirs == "/x/DoE-claude"  # abs-path-ok: synthetic TOML fixture value, not a real repo reference
    assert mine != theirs


# ---------------------------------------------------------------------------
# AC1: spawn-free, coordinator_core-free AST assertions
# ---------------------------------------------------------------------------


def _load_module_ast():
    module_path = os.path.join(_LIB_DIR, "machine_local_impl_resolve.py")
    with open(module_path, "r", encoding="utf-8") as f:
        source = f.read()
    return ast.parse(source, filename=module_path)


def test_module_imports_no_coordinator_core():
    tree = _load_module_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("coordinator_core"), alias.name
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            assert not module_name.startswith("coordinator_core"), module_name


def _function_defs_by_name(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _reachable_helper_names(entry_name, funcs, seen=None):
    """Collect the transitive set of function names called (by bare Name)
    from `entry_name`'s body, restricted to functions defined in this same
    module (so we can walk into their bodies too)."""
    if seen is None:
        seen = set()
    if entry_name in seen or entry_name not in funcs:
        return seen
    seen.add(entry_name)
    node = funcs[entry_name]
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            _reachable_helper_names(sub.func.id, funcs, seen)
    return seen


def _has_subprocess_call(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            # subprocess.run(...) / subprocess.Popen(...) / subprocess.call(...)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "subprocess":
                    return True
            # os.system(...) is not subprocess.* — out of scope for this AC,
            # which names "no subprocess.* call node" specifically.
    return False


def test_registry_get_and_registry_dir_never_reach_a_subprocess_call():
    tree = _load_module_ast()
    funcs = _function_defs_by_name(tree)
    for entry in ("registry_get", "registry_dir"):
        assert entry in funcs, f"{entry} not found in module AST"
        reachable = _reachable_helper_names(entry, funcs)
        for name in reachable:
            assert not _has_subprocess_call(funcs[name]), (
                f"{entry} reaches a subprocess.* call via {name}()"
            )
