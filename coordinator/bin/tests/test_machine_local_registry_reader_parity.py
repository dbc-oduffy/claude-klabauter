"""test_machine_local_registry_reader_parity.py — pins
`machine_local_impl_resolve.registry_get`/`registry_dir` (the spawn-free,
`coordinator_core`-free registry reader in `coordinator/bin/lib/
machine_local_impl_resolve.py`) to `coordinator_core.machine_resolver.
registry_get` (the oracle) by direct value comparison across every
resolution-order case the two readers must agree on.

This test file is the ONLY place in this pair permitted to import
`coordinator_core` — it is the parity oracle. `machine_local_impl_resolve.py`
itself MUST NOT (see that module's docstring, HARD CONSTRAINT).

Spec backlink: the 2026-08-20 dispatch brief for the rung-2 registry read
that stops spawning (C1).
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ast import Module

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
Content_root = "C:/coordinator-content-repo"  # abs-path-ok: synthetic TOML fixture value, not a real repo reference

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


def _reset_env(monkeypatch) -> None:
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


def test_parity_nested_table_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _parity_case(monkeypatch, tmp_path, "coordinator.machine_slug") == "local-machine"


def test_parity_flat_quoted_dotted_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _parity_case(monkeypatch, tmp_path, "flat.dotted.key") == "local-value"


def test_parity_list_valued_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _parity_case(monkeypatch, tmp_path, "flat_list_key") == "a\nb\nc"


def test_parity_explicitly_empty_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(tmp_path, 'empty_key = ""\n')
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    mine = mlir.registry_get("empty_key")
    theirs = machine_resolver.registry_get("empty_key")
    assert mine is None
    assert theirs is None


def test_parity_local_over_tracked_shadowed_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _parity_case(monkeypatch, tmp_path, "shadowed_key") == "local-wins"


def test_parity_concerns_prefixed_table_key_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _parity_case(monkeypatch, tmp_path, "concerns.leaked.should_be_absent") is None


def test_parity_schema_prefixed_table_key_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _parity_case(monkeypatch, tmp_path, "schema.leaked.should_be_absent") is None


def test_parity_absent_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _parity_case(monkeypatch, tmp_path, "does.not.exist") is None


def test_parity_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


def test_parity_via_settings_home_not_registry_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset_env(monkeypatch)
    settings_home_root = tmp_path / "settings-home"
    settings_home_root.mkdir()
    _seed_registry_dir(settings_home_root, REGISTRY_TOML, REGISTRY_LOCAL_TOML)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_root))

    mine = mlir.registry_get("coordinator.machine_slug")
    theirs = machine_resolver.registry_get("coordinator.machine_slug")
    assert mine == theirs == "local-machine"


def test_parity_repos_key_normalizes_backslash_form(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(
        tmp_path,
        r'"repos.content_root" = "C:\\content-root\\worktree"' + "\n",  # abs-path-ok: synthetic fixture value
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    mine = mlir.registry_get("repos.content_root")
    theirs = machine_resolver.registry_get("repos.content_root")
    assert mine == theirs


def test_registry_get_repairs_msys_mount_form_for_repos_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if os.name != "nt":
        import pytest

        pytest.skip("MSYS mount-form repair is Windows-only (os.name == 'nt' gated)")
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(
        tmp_path,
        '"repos.content_root" = "/x/content-root"\n',
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    mine = mlir.registry_get("repos.content_root")
    assert mine == "C:/content-root"  # abs-path-ok: synthetic fixture value

    # PINNED DIVERGENCE (see module docstring's "Two divergences... ACCEPTED"
    # list, item 3): the oracle does no normalization anywhere in its body and
    # returns the raw stored value unrepaired. `mlir.registry_get` repairs it.
    # This is deliberate — see module docstring — so assert the divergence
    # explicitly rather than leaving it merely unasserted (which is what let
    # this exact case go unpinned before this test was extended).
    theirs = machine_resolver.registry_get("repos.content_root")
    assert theirs == "/x/content-root"
    assert mine != theirs


# ---------------------------------------------------------------------------
# Concern files: <c>.local.toml / <c>.toml layered above the registry files,
# pinned to the machine-local CLI's own layer resolution.
# ---------------------------------------------------------------------------

_CLI_SRC = os.path.join(_REPO_ROOT, "templates", "bin", "_machine_local.py")

CONCERN_REGISTRY_TOML = """
schema = 1
concerns = ["project_rag", "unreal"]

[example_retrieval_repo]
python = "registry-shadowed"
"""

CONCERN_REGISTRY_LOCAL_TOML = """
[example_retrieval_repo]
python = "registry-local-shadowed"
only_in_registry_local = "dropped"
"""

CONCERN_LOCAL_TOML = """
schema = 1

[example_retrieval_repo]
python = "/concern/local/python"
empty_here = ""

[example_retrieval_repo.bin]
cli = "/concern/local/cli"
"""

CONCERN_BASE_TOML = """
schema = 1
base_only = "concern-base"
python = "concern-base-python"
"""


def _cli_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_machine_local_parity_oracle", _CLI_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_concern_dir(tmp_path):
    reg_dir = _seed_registry_dir(tmp_path, CONCERN_REGISTRY_TOML, CONCERN_REGISTRY_LOCAL_TOML)
    (reg_dir / "example_retrieval_repo.local.toml").write_text(CONCERN_LOCAL_TOML, encoding="utf-8")
    (reg_dir / "example_retrieval_repo.toml").write_text(CONCERN_BASE_TOML, encoding="utf-8")
    return reg_dir


def _three_way(monkeypatch, tmp_path, key):
    _reset_env(monkeypatch)
    reg_dir = _seed_concern_dir(tmp_path)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    cli = _cli_module()
    cli_val = cli._resolve_key(key, cli._build_resolution_layers(str(reg_dir)))
    mine = mlir.registry_get(key)
    theirs = machine_resolver.registry_get(key)
    return cli_val, mine, theirs


def test_parity_concern_local_key_resolves_in_both_readers_and_cli(monkeypatch, tmp_path):
    cli_val, mine, theirs = _three_way(monkeypatch, tmp_path, "example_retrieval_repo.python")
    assert cli_val == mine == theirs == "/concern/local/python"


def test_parity_concern_nested_table_key(monkeypatch, tmp_path):
    cli_val, mine, theirs = _three_way(monkeypatch, tmp_path, "example_retrieval_repo.bin.cli")
    assert cli_val == mine == theirs == "/concern/local/cli"


def test_parity_concern_base_file_key_without_namespace_table(monkeypatch, tmp_path):
    cli_val, mine, theirs = _three_way(monkeypatch, tmp_path, "example_retrieval_repo.base_only")
    assert cli_val == mine == theirs == "concern-base"


def test_parity_registry_key_under_concern_prefix_does_not_resolve(monkeypatch, tmp_path):
    cli_val, mine, theirs = _three_way(
        monkeypatch, tmp_path, "example_retrieval_repo.only_in_registry_local"
    )
    assert cli_val is None
    assert mine is None
    assert theirs is None


def test_parity_concern_env_override_beats_concern_file(monkeypatch, tmp_path):
    _reset_env(monkeypatch)
    reg_dir = _seed_concern_dir(tmp_path)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("MACHINE_LOCAL_EXAMPLE_RETRIEVAL_REPO_PYTHON", "env-wins")
    assert mlir.registry_get("example_retrieval_repo.python") == "env-wins"
    assert machine_resolver.registry_get("example_retrieval_repo.python") == "env-wins"


def test_parity_non_concern_key_still_reads_registry_files(monkeypatch, tmp_path):
    cli_val, mine, theirs = _three_way(monkeypatch, tmp_path, "coordinator.machine_slug")
    assert cli_val is None and mine is None and theirs is None


def test_parity_empty_concern_value_is_not_found(monkeypatch, tmp_path):
    _, mine, theirs = _three_way(monkeypatch, tmp_path, "example_retrieval_repo.empty_here")
    assert mine is None
    assert theirs is None


def test_parity_key_not_listed_in_concerns_ignores_concern_file(monkeypatch, tmp_path):
    _reset_env(monkeypatch)
    reg_dir = _seed_registry_dir(tmp_path, "schema = 1\n")
    (reg_dir / "example_retrieval_repo.local.toml").write_text(CONCERN_LOCAL_TOML, encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    assert mlir.registry_get("example_retrieval_repo.python") is None
    assert machine_resolver.registry_get("example_retrieval_repo.python") is None


def _load_module_ast() -> Module:
    module_path = os.path.join(_LIB_DIR, "machine_local_impl_resolve.py")
    with open(module_path, "r", encoding="utf-8") as f:
        source = f.read()
    return ast.parse(source, filename=module_path)


def test_module_imports_no_coordinator_core() -> None:
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


def _has_subprocess_call(node) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "subprocess":
                    return True
    return False


def test_registry_get_and_registry_dir_never_reach_a_subprocess_call() -> None:
    tree = _load_module_ast()
    funcs = _function_defs_by_name(tree)
    for entry in ("registry_get", "registry_dir"):
        assert entry in funcs, f"{entry} not found in module AST"
        reachable = _reachable_helper_names(entry, funcs)
        for name in reachable:
            assert not _has_subprocess_call(funcs[name]), (
                f"{entry} reaches a subprocess.* call via {name}()"
            )
