"""Tests for `coordinator_core.ceremony_common.cli_dispatch` — the lifted
in-process CLI load/invoke primitive (C1 of
docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md).

Additive-only chunk: these tests exercise the primitive in isolation, not
through any of the trio's own `apply.py` (untouched in this chunk)."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from coordinator_core.ceremony_common.cli_dispatch import (
    UNRESOLVED_PLUGIN_CLI_ROOT,
    invoke_cli_main,
    load_cli_module,
    resolve_cli_script_root,
    resolve_plugin_cli_script_root,
)
from coordinator_core.ceremony_common.cli_rejection import CliExitClass
from coordinator_core.ops import coordinator_doe_root as _doe_root_mod


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    script_path = tmp_path / name
    script_path.write_text(body, encoding="utf-8")
    return script_path


def test_resolve_cli_script_root_anchors_on_the_engine_clone(tmp_path: Path):
    """Engine-anchored, and no `repo_root` parameter to conflate with it —
    see the module docstring's ENGINE-root paragraph and
    `merge_assemble/tests/test_producer_root_is_engine_not_target_repo.py`
    for the consumer-repo failure the old repo-root join produced."""
    import coordinator_core.ceremony_common.cli_dispatch as cli_dispatch

    engine_root = Path(cli_dispatch.__file__).resolve().parents[2]
    assert resolve_cli_script_root() == engine_root / "coordinator" / "bin"
    with pytest.raises(TypeError):
        resolve_cli_script_root(tmp_path)  # type: ignore[call-arg]


def test_load_cli_module_loads_extensionless_script(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "bareword-launcher",
        "def main(argv):\n    return 0\n",
    )
    module = load_cli_module("test_cli_dispatch_bareword", script)
    assert hasattr(module, "main")
    assert module.main([]) == 0


def test_load_cli_module_registers_in_sys_modules_before_exec(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "dataclass_user.py",
        "import sys\n"
        "assert sys.modules.get(__name__) is not None\n"
        "def main(argv):\n    return 0\n",
    )
    module_name = "test_cli_dispatch_dataclass_user"
    try:
        module = load_cli_module(module_name, script)
        assert sys.modules[module_name] is module
    finally:
        sys.modules.pop(module_name, None)


def test_load_cli_module_caches_on_resolved_script_path(tmp_path: Path):
    script = _write_script(tmp_path, "cached.py", "def main(argv):\n    return 0\n")
    first = load_cli_module("test_cli_dispatch_cached", script)
    second = load_cli_module("test_cli_dispatch_cached", script)
    assert first is second


def test_load_cli_module_does_not_collide_across_script_paths_sharing_a_module_name(
    tmp_path: Path,
):
    # The cache is keyed by
    # resolved script path, not caller-chosen module_name; two different
    # on-disk scripts loaded under the same module_name must not alias.
    first_script = _write_script(tmp_path, "first.py", "def main(argv):\n    return 1\n")
    second_script = _write_script(tmp_path, "second.py", "def main(argv):\n    return 2\n")
    shared_name = "test_cli_dispatch_shared_module_name"
    first = load_cli_module(shared_name, first_script)
    second = load_cli_module(shared_name, second_script)
    assert first is not second
    assert first.main([]) == 1
    assert second.main([]) == 2


def test_load_cli_module_binds_script_own_lib_over_a_preceding_namespace_package(
    tmp_path: Path,
):
    """A bare `import lib` at a loaded script's top level must resolve
    against the script's OWN sibling `lib/` package, never against a
    same-named PEP-420 namespace package (no `__init__.py`) that happens to
    sit earlier on the process's ambient `sys.path` -- the failure mode
    `_exec_with_own_dir_on_path` closes (module docstring, "What this
    module does NOT isolate")."""
    decoy_root = tmp_path / "decoy_root"
    (decoy_root / "lib").mkdir(parents=True)

    script_dir = tmp_path / "script_dir"
    real_lib = script_dir / "lib"
    real_lib.mkdir(parents=True)
    (real_lib / "__init__.py").write_text("MARKER = 'real-package'\n", encoding="utf-8")
    script = _write_script(
        script_dir,
        "uses_lib.py",
        "import lib\n"
        "def main(argv):\n"
        "    return 0\n",
    )

    module_name = "test_cli_dispatch_uses_lib"
    saved_sys_path = list(sys.path)
    sys.path.insert(0, str(decoy_root))
    sys.modules.pop("lib", None)
    try:
        module = load_cli_module(module_name, script)
        assert hasattr(module.lib, "__file__")
        assert module.lib.__file__ is not None
        assert module.lib.MARKER == "real-package"
    finally:
        sys.path[:] = saved_sys_path
        sys.modules.pop(module_name, None)
        sys.modules.pop("lib", None)


def test_load_cli_module_propagates_import_time_exception(tmp_path: Path):
    script = _write_script(tmp_path, "broken.py", "raise ValueError('boom')\n")
    module_name = "test_cli_dispatch_broken"
    with pytest.raises(ValueError, match="boom"):
        load_cli_module(module_name, script)
    assert module_name not in sys.modules


def test_invoke_cli_main_argv_taking_main_receives_args():
    module = _module_with_main(
        "def main(argv):\n"
        "    print('got:' + ','.join(argv))\n"
        "    return 0\n"
    )
    exit_code, stdout, stderr, exit_class = invoke_cli_main(module, ["--flag", "x"])
    assert exit_code == 0
    assert "got:--flag,x" in stdout
    assert stderr == ""
    assert exit_class is CliExitClass.RETURNED


def test_invoke_cli_main_zero_arg_trampoline_splices_argv():
    module = _module_with_main(
        "import sys\n"
        "def main():\n"
        "    print('argv:' + ','.join(sys.argv[1:]))\n"
        "    sys.exit(0)\n"
    )
    saved_argv = list(sys.argv)
    exit_code, stdout, _stderr, _exit_class = invoke_cli_main(module, ["--decisions", "{}"])
    assert sys.argv == saved_argv
    assert exit_code == 0
    assert "argv:--decisions,{}" in stdout


def test_invoke_cli_main_captures_stdout_and_stderr_separately():
    module = _module_with_main(
        "import sys\n"
        "def main(argv):\n"
        "    print('to-stdout')\n"
        "    print('to-stderr', file=sys.stderr)\n"
        "    return 0\n"
    )
    exit_code, stdout, stderr, _exit_class = invoke_cli_main(module, [])
    assert exit_code == 0
    assert "to-stdout" in stdout
    assert "to-stderr" in stderr


def test_invoke_cli_main_stdin_swap_feeds_stdin_text():
    module = _module_with_main(
        "import sys\n"
        "def main(argv):\n"
        "    print('read:' + sys.stdin.read())\n"
        "    return 0\n"
    )
    exit_code, stdout, _stderr, _exit_class = invoke_cli_main(
        module, [], stdin_text="hello-stdin"
    )
    assert exit_code == 0
    assert "read:hello-stdin" in stdout


def test_invoke_cli_main_leaves_stdin_untouched_when_none():
    module = _module_with_main(
        "import sys\n"
        "def main(argv):\n"
        "    return 0 if sys.stdin is _saved else 1\n"
    )
    saved_stdin = sys.stdin
    module.__dict__["_saved"] = saved_stdin
    exit_code, _stdout, _stderr, _exit_class = invoke_cli_main(module, [])
    assert sys.stdin is saved_stdin
    assert exit_code == 0


def test_invoke_cli_main_classifies_argv_rejected():
    module = _module_with_main(
        "import sys\n"
        "def main(argv):\n"
        "    print('usage: prog [-h]', file=sys.stderr)\n"
        "    print('prog: error: unrecognized arguments', file=sys.stderr)\n"
        "    sys.exit(2)\n"
    )
    exit_code, _stdout, _stderr, exit_class = invoke_cli_main(module, ["--bogus"])
    assert exit_code == 2
    assert exit_class is CliExitClass.ARGV_REJECTED


def test_invoke_cli_main_classifies_returned_for_semantic_exit_2():
    module = _module_with_main(
        "import sys\n"
        "def main(argv):\n"
        "    sys.exit(2)\n"
    )
    exit_code, _stdout, _stderr, exit_class = invoke_cli_main(module, [])
    assert exit_code == 2
    assert exit_class is CliExitClass.RETURNED


def test_invoke_cli_main_no_main_raises_value_error():
    module = _module_with_main("x = 1\n")
    del module.__dict__["main"]
    with pytest.raises(ValueError, match="exposes no main"):
        invoke_cli_main(module, [])


def test_invoke_cli_main_propagates_non_system_exit_exception():
    module = _module_with_main(
        "def main(argv):\n"
        "    raise RuntimeError('kaboom')\n"
    )
    with pytest.raises(RuntimeError, match="kaboom"):
        invoke_cli_main(module, [])


def _module_with_main(body: str):
    """Builds an in-memory module (no file needed) carrying `body` as its
    top-level source, for tests exercising `invoke_cli_main` in isolation
    from `load_cli_module`."""
    import types

    module = types.ModuleType("test_cli_dispatch_inline")
    module.__dict__["main"] = None
    exec(compile(body, "<test_cli_dispatch_inline>", "exec"), module.__dict__)
    return module


# ---------------------------------------------------------------------------
# resolve_plugin_cli_script_root() -- AC1, AC2, AC2b (P036-T1).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _plugin_cli_clean_env(monkeypatch, tmp_path):
    """Isolates every test in this module from the operator's real DoE
    resolution state and resets both `coordinator_doe_root` memo pairs before
    and after -- same discipline as
    `coordinator_core/ops/test_coordinator_doe_root.py`'s own `_clean_env`."""
    _doe_root_mod._reset_doe_root_cache()
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    empty_registry = tmp_path / "ml-registry"
    empty_registry.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(empty_registry))
    yield
    _doe_root_mod._reset_doe_root_cache()


def test_ac1_resolve_plugin_cli_script_root_takes_zero_parameters():
    assert inspect.signature(resolve_plugin_cli_script_root).parameters == {}


def test_ac1_resolve_cli_script_root_unchanged(tmp_path: Path):
    """`resolve_cli_script_root`'s signature and body are untouched by this
    plan (AC1) -- the existing test above already pins its return value;
    this pins the zero-parameter signature stays a TypeError on a positional
    arg, unchanged."""
    with pytest.raises(TypeError):
        resolve_cli_script_root(tmp_path)  # type: ignore[call-arg]


def test_ac2_i_resolves_from_a_fixture_doe_root_with_coordinator_bin(tmp_path, monkeypatch):
    fixture_root = tmp_path / "doe-root"
    (fixture_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(fixture_root))

    result = resolve_plugin_cli_script_root()

    assert result == fixture_root / "coordinator" / "bin"


def test_ac2_ii_every_rung_unresolvable_returns_none_and_raises_nothing(tmp_path, monkeypatch):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    assert resolve_plugin_cli_script_root() is None


def test_ac2_iii_stale_clone_root_with_no_coordinator_bin_returns_none(tmp_path, monkeypatch):
    """The stale/moved-clone case: the ladder resolves a real, existing root
    that has no `coordinator/bin` subdirectory under it."""
    stale_root = tmp_path / "stale-doe-root"
    stale_root.mkdir()
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(stale_root))

    assert resolve_plugin_cli_script_root() is None


def test_ac2_iv_rung_cut_spawns_zero_processes(tmp_path, monkeypatch):
    """With every in-process rung unresolvable, the resolver never descends
    to rung 3's `resolve_coordinator_clone.resolve_clone_root()` and its
    `subprocess.run` -- patching both raising is the cheapest proof."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    def _boom(*_args, **_kwargs):
        raise AssertionError("resolve_plugin_cli_script_root must not spawn a subprocess")

    monkeypatch.setattr(
        _doe_root_mod._resolve_coordinator_clone, "resolve_clone_root", _boom
    )

    assert resolve_plugin_cli_script_root() is None


def test_ac2_iv_reset_clears_both_memo_pairs(tmp_path, monkeypatch):
    _doe_root_mod.coordinator_doe_root_in_process()
    _doe_root_mod.coordinator_doe_root()
    _doe_root_mod._reset_doe_root_cache()

    assert _doe_root_mod._IN_PROCESS_DOE_ROOT_RESOLVED is False
    assert _doe_root_mod._DOE_ROOT_RESOLVED is False


def test_ac2_v_flat_layout_root_is_not_an_admissible_source(tmp_path, monkeypatch):
    """A rung-2.75 flat OSS/marketplace root (`schemas/` and `bin/` directly
    under it, no `coordinator/` subdirectory) resolves at
    `coordinator_doe_root_in_process()` but is not admissible here -- the
    join is deliberately not layout-aware."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "flat-fake-home"
    flat_root = fake_home / ".claude" / "plugins" / "coordinator-claude"
    (flat_root / ".claude-plugin").mkdir(parents=True)
    (flat_root / ".claude-plugin" / "plugin.json").write_text("{}")
    (flat_root / "schemas").mkdir()
    (flat_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")
    (flat_root / "bin").mkdir()

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    root, rung = _doe_root_mod.coordinator_doe_root_in_process()
    assert root == str(flat_root)
    assert rung == "codename-free"

    _doe_root_mod._reset_doe_root_cache()

    def _boom(*_args, **_kwargs):
        raise AssertionError("must not spawn a subprocess")

    monkeypatch.setattr(
        _doe_root_mod._resolve_coordinator_clone, "resolve_clone_root", _boom
    )

    assert resolve_plugin_cli_script_root() is None


def test_ac2b_unresolved_plugin_cli_root_sentinel_shape():
    assert isinstance(UNRESOLVED_PLUGIN_CLI_ROOT, Path)
    assert UNRESOLVED_PLUGIN_CLI_ROOT.name == "bin"
    assert UNRESOLVED_PLUGIN_CLI_ROOT.exists() is False
