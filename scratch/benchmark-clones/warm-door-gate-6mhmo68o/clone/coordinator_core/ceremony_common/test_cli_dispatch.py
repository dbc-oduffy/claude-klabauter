"""Tests for `coordinator_core.ceremony_common.cli_dispatch` — the lifted
in-process CLI load/invoke primitive (C1 of
docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md).

Additive-only chunk: these tests exercise the primitive in isolation, not
through any of the trio's own `apply.py` (untouched in this chunk)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.ceremony_common.cli_dispatch import (
    invoke_cli_main,
    load_cli_module,
    resolve_cli_script_root,
)
from coordinator_core.ceremony_common.cli_rejection import CliExitClass


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    script_path = tmp_path / name
    script_path.write_text(body, encoding="utf-8")
    return script_path


def test_resolve_cli_script_root_joins_from_explicit_repo_root(tmp_path: Path):
    root = resolve_cli_script_root(tmp_path)
    assert root == tmp_path / "coordinator" / "bin"


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
    # Review: coordinator:code-reviewer (Finding 1) — the cache is keyed by
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
